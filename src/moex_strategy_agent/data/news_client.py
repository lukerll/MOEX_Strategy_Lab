"""Источники новостей. Все тексты отсюда недоверенные данные.

Порядок от самых надёжных к самым хрупким:
  * cbr_keyrate: решения Банка России по ключевой ставке, архив за все годы, без VPN;
  * moex_sitenews: архив новостей сайта биржи (общие новости рынка);
  * cbr_rss: RSS Банка России, только свежие публикации;
  * google_news: заголовки СМИ о компании за дни резких движений. В России сервис закрыт без VPN,
    поэтому ответы кешируются, а scripts/prefetch_news.py заполняет кеш заранее (запуск с VPN);
  * demo: локальная фикстура data/demo/news.json для офлайн-режима.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import pandas as pd

from ..config import Settings
from .market_data import KNOWN_NAMES
from .moex_client import DataSourceError, MoexClient

CBR_FEEDS = ["https://www.cbr.ru/rss/eventrss", "https://www.cbr.ru/rss/RssPress"]
CBR_SOAP = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
CBR_KEYRATE_PAGE = "https://www.cbr.ru/hd_base/KeyRate/"
GOOGLE_NEWS = "https://news.google.com/rss/search"
EMERGENCY_MONDAYS = {date(2022, 2, 28)}
MAX_SITENEWS_PAGES = 40
_google_down_until = [0.0]            # после отказа 10 минут не стучимся в Google: без VPN он закрыт
_keyrate_cache: dict[tuple, list] = {}

# Название компании для заголовков и поиска: самое длинное из известных имён
COMPANY = {}
for _name, _t in KNOWN_NAMES.items():
    if len(_name) > len(COMPANY.get(_t, "")):
        COMPANY[_t] = _name


@dataclass
class RawNews:
    published_at: datetime
    title: str
    url: str | None
    source: str


SOURCE_TITLES = {"cbr_keyrate": "ключевая ставка ЦБ",
                 "moex_sitenews": "новости биржи", "cbr_rss": "RSS Банка России", "google_news": "СМИ (Google Новости)"}


def _pct(x: float) -> str:
    return f"{x:g}".replace(".", ",") + "%"


def company_name(ticker: str) -> str:
    n = COMPANY.get(ticker.upper())
    return n[:1].upper() + n[1:] if n else ticker.upper()


class NewsService:
    def __init__(self, settings: Settings, moex: MoexClient | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.mode = settings.get("data.mode", "moex")
        self._moex = moex
        self._transport = transport
        self.status: list[str] = []
        self.coverage: dict[str, str] = {}      # источник -> что он дал или почему молчит
        self._notes: dict[str, str] = {}        # пояснение к нулю или к данным из кеша

    def _http(self, timeout: float | None = None) -> httpx.Client:
        t = timeout or self.settings.get("data.request_timeout_s", 15)
        return httpx.Client(timeout=httpx.Timeout(t, connect=min(6.0, t)), transport=self._transport,
                            follow_redirects=True, headers={"User-Agent": "moex-strategy-agent/1.0"})

    def _moex_client(self) -> MoexClient:
        return self._moex or MoexClient(timeout_s=self.settings.get("data.request_timeout_s", 15), max_retries=2)

    def fetch(self, start: date, end: date, ticker: str | None = None,
              event_dates: list[date] | None = None) -> list[RawNews]:
        self.status, self.coverage = [], {}
        if self.mode == "demo":
            return self._demo(start, end)
        items: list[RawNews] = []
        for src in self.settings.get("news.sources", []):
            title = SOURCE_TITLES.get(src, src)
            try:
                if src == "cbr_keyrate":
                    got = self._cbr_keyrate(start, end)
                elif src == "moex_sitenews":
                    got = self._moex_sitenews(start, end)
                elif src == "cbr_rss":
                    got = self._cbr(start, end)
                elif src == "google_news":
                    got = self._google_news(ticker, event_dates or []) if ticker else []
                else:
                    continue
                items += got
                note = self._notes.pop(src, "")
                self.coverage[title] = f"{len(got)} за период" + (f" ({note})" if note else "")
            except (DataSourceError, httpx.HTTPError, OSError, ET.ParseError, ValueError) as e:
                why = "в России открывается только через VPN" if src == "google_news" else e.__class__.__name__
                self.coverage[title] = f"недоступен ({why})"
                self.status.append(f"{title}: недоступен ({why})")
        return items

    # ---------- ключевая ставка ----------
    def _cbr_keyrate(self, start: date, end: date) -> list[RawNews]:
        """Ставка по дням с ЦБ; решение = смена значения. Сначала веб-сервис, затем страница с таблицей."""
        lo = start - timedelta(days=60)
        key = (lo, end, date.today())
        if key in _keyrate_cache and self._transport is None:
            series = _keyrate_cache[key]
        else:
            try:
                series = self._keyrate_soap(lo, end)
            except (httpx.HTTPError, ET.ParseError, ValueError):
                series = self._keyrate_page(lo, end)
            _keyrate_cache[key] = series
        out: list[RawNews] = []
        prev = None
        for d, rate in sorted(series):
            if prev is not None and abs(rate - prev) > 1e-9:
                # плановое решение принимают в пятницу, ставка действует с понедельника;
                # внеочередное (15.08.2023, 28.02.2022) действует в день решения
                planned = d.weekday() == 0 and d not in EMERGENCY_MONDAYS
                decided = (pd.Timestamp(d) - pd.offsets.BDay(1)).date() if planned else d
                if start <= decided <= end:
                    word = "повысил" if rate > prev else "снизил"
                    out.append(RawNews(datetime.combine(decided, datetime.min.time()),
                                       f"Банк России {word} ключевую ставку: {_pct(prev)} → {_pct(rate)} "
                                       f"(действует с {d:%d.%m.%Y})",
                                       "https://www.cbr.ru/hd_base/KeyRate/", "Банк России"))
            prev = rate
        return out

    def _keyrate_soap(self, lo: date, hi: date) -> list[tuple[date, float]]:
        body = ('<?xml version="1.0" encoding="utf-8"?><soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
                '<soap12:Body><KeyRateXML xmlns="http://web.cbr.ru/">'
                f'<fromDate>{lo.isoformat()}T00:00:00</fromDate><ToDate>{hi.isoformat()}T00:00:00</ToDate>'
                '</KeyRateXML></soap12:Body></soap12:Envelope>')
        with self._http() as c:
            r = c.post(CBR_SOAP, content=body.encode(),
                       headers={"Content-Type": "application/soap+xml; charset=utf-8"})
            r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for el in root.iter():
            if el.tag.split("}")[-1] != "KR":
                continue
            kids = {k.tag.split("}")[-1]: (k.text or "") for k in el}
            if "DT" in kids and "Rate" in kids:
                out.append((date.fromisoformat(kids["DT"][:10]), float(kids["Rate"].replace(",", "."))))
        if not out:
            raise ValueError("веб-сервис ЦБ вернул пустой ответ")
        return out

    def _keyrate_page(self, lo: date, hi: date) -> list[tuple[date, float]]:
        params = {"UniDbQuery.Posted": "True", "UniDbQuery.From": f"{lo:%d.%m.%Y}", "UniDbQuery.To": f"{hi:%d.%m.%Y}"}
        with self._http() as c:
            r = c.get(CBR_KEYRATE_PAGE, params=params)
            r.raise_for_status()
        out = []
        for d, v in re.findall(r"<td>\s*(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td>\s*([\d,\.]+)\s*</td>", r.text):
            out.append((datetime.strptime(d, "%d.%m.%Y").date(), float(v.replace(",", "."))))
        if not out:
            raise ValueError("страница ЦБ без таблицы ставок")
        return out

    # ---------- заголовки СМИ ----------
    def _google_news(self, ticker: str, event_dates: list[date]) -> list[RawNews]:
        """По запросу на каждый аномальный день: «Сбербанк» с окном в несколько дней. Результат кешируется."""
        import feedparser

        cache_path = self.settings.path("data.cache_dir") / "google_news.json"
        cache: dict[str, list[dict]] = {}
        if cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        name = company_name(ticker)
        out: list[RawNews] = []
        fetched, blocked = 0, None
        try:
            with self._http(timeout=8) as c:
                for d in sorted(set(event_dates))[: int(self.settings.get("news.max_events", 8))]:
                    key = f"{ticker}:{d.isoformat()}"
                    if key not in cache:
                        if blocked is None and time.monotonic() < _google_down_until[0]:
                            blocked = httpx.ConnectError("Google Новости недавно не ответили")
                        if blocked:                   # сеть уже не пустила: берём только кешированное
                            continue
                        q = f'"{name}" after:{(d - timedelta(days=3)).isoformat()} before:{(d + timedelta(days=2)).isoformat()}'
                        try:
                            r = c.get(f"{GOOGLE_NEWS}?q={quote(q)}&hl=ru&gl=RU&ceid=RU:ru")
                            r.raise_for_status()
                        except (httpx.HTTPError, OSError) as e:
                            blocked = e
                            _google_down_until[0] = time.monotonic() + 600
                            continue
                        fetched += 1
                        feed = feedparser.parse(r.text)
                        rows = []
                        for e in feed.entries[:20]:
                            if not getattr(e, "published_parsed", None):
                                continue
                            src = getattr(getattr(e, "source", None), "title", None) or "СМИ"
                            title = re.sub(r"\s+-\s+[^-]+$", "", e.title).strip()     # «Заголовок - Издание»
                            rows.append({"ts": datetime(*e.published_parsed[:6]).isoformat(), "title": title,
                                         "url": e.link, "source": src})
                        cache[key] = rows
                    for row in cache[key]:
                        out.append(RawNews(datetime.fromisoformat(row["ts"]), row["title"], row["url"], row["source"]))
        finally:                                  # что успели скачать, сохраняем даже при обрыве
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        if blocked and not out:
            raise blocked
        if blocked:
            self._notes["google_news"] = "из кеша; остальные дни без VPN недоступны"
        elif event_dates and not fetched:
            self._notes["google_news"] = "из кеша"
        return out

    def _demo(self, start: date, end: date) -> list[RawNews]:
        path = self.settings.path("data.demo_dir") / "news.json"
        if not path.exists():
            self.status.append("демо-фикстура новостей не найдена")
            return []
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        out = []
        for r in rows:
            ts = datetime.fromisoformat(r["published_at"])
            if start <= ts.date() <= end:
                out.append(RawNews(ts, r["title"], r.get("url"), r.get("source", "demo")))
        self.status.append("новости из демо-фикстуры (синтетика)")
        return out

    def _moex_sitenews(self, start: date, end: date) -> list[RawNews]:
        """Листаем архив от свежих к старым. Прочитанное кладём в кеш, чтобы не листать снова."""
        cache_path = self.settings.path("data.cache_dir") / "moex_sitenews.json"
        cache: dict[str, dict] = {}
        if cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        oldest = min((v["published_at"] for v in cache.values()), default=None)
        moex = self._moex or MoexClient(timeout_s=self.settings.get("data.request_timeout_s", 15),
                                        max_retries=2)
        offset, reached = 0, bool(oldest and oldest[:10] <= start.isoformat())
        had_cache = bool(cache)
        for _ in range(MAX_SITENEWS_PAGES):
            df = moex.site_news(offset)
            if df.empty:
                reached = True
                break
            new = 0
            for r in df.to_dict("records"):
                key = str(r["id"])
                if key not in cache:
                    new += 1
                    cache[key] = {"published_at": str(r["published_at"]).replace(" ", "T"),
                                  "title": str(r["title"])}
            page_oldest = min(str(x) for x in df["published_at"])[:10]
            if page_oldest < start.isoformat():
                reached = True
            if new == 0 and (reached or had_cache):
                break          # догнали уже прочитанное: глубже архив листали в прошлый раз
            if reached and page_oldest < start.isoformat():
                break
            offset += len(df)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        if not reached:
            self.status.append(f"архив новостей MOEX просмотрен на {MAX_SITENEWS_PAGES} страниц, "
                               "начало периода не достигнуто")
            oldest_seen = min((v["published_at"] for v in cache.values()), default=None)
            if oldest_seen:
                self._notes["moex_sitenews"] = f"архив сайта биржи доступен с {oldest_seen[8:10]}.{oldest_seen[5:7]}.{oldest_seen[:4]}"
        out = []
        for key, v in cache.items():
            ts = datetime.fromisoformat(v["published_at"])
            if start <= ts.date() <= end:
                out.append(RawNews(ts, v["title"], f"https://www.moex.com/n{key}", "Московская биржа"))
        return out

    def _cbr(self, start: date, end: date) -> list[RawNews]:
        import feedparser

        out = []
        oldest = None
        with httpx.Client(timeout=self.settings.get("data.request_timeout_s", 15)) as c:
            for url in CBR_FEEDS:
                feed = feedparser.parse(c.get(url).text)
                for e in feed.entries:
                    if not getattr(e, "published_parsed", None):
                        continue
                    ts = datetime(*e.published_parsed[:6])
                    oldest = min(oldest or ts, ts)
                    if start <= ts.date() <= end + timedelta(days=1):
                        out.append(RawNews(ts, e.title, e.link, "Банк России"))
        if oldest and oldest.date() > start:
            self.status.append(f"RSS Банка России хранит публикации только с {oldest.date()}")
        return out
