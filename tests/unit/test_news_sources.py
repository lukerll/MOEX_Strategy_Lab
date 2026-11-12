"""Источники новостей на моках: ключевая ставка ЦБ, Google Новости и честный охват."""
from __future__ import annotations

from datetime import date

import httpx

from moex_strategy_agent.analysis.news_matching import match_news
from moex_strategy_agent.data.moex_client import MoexClient
from moex_strategy_agent.data.news_client import NewsService
from moex_strategy_agent.domain.schemas import PriceEvent

SOAP = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body>
<KeyRateXMLResponse xmlns="http://web.cbr.ru/"><KeyRateXMLResult><KeyRate xmlns="">
<KR><DT>2024-07-26T00:00:00+03:00</DT><Rate>16.00</Rate></KR>
<KR><DT>2024-07-29T00:00:00+03:00</DT><Rate>18.00</Rate></KR>
<KR><DT>2024-07-30T00:00:00+03:00</DT><Rate>18.00</Rate></KR>
</KeyRate></KeyRateXMLResult></KeyRateXMLResponse></soap:Body></soap:Envelope>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>q</title>
<item><title>Сбербанк отчитался о рекордной прибыли - РБК</title><link>https://example.ru/a</link>
<pubDate>Wed, 10 Jul 2024 09:00:00 GMT</pubDate><source url="https://rbc.ru">РБК</source></item>
</channel></rss>"""


def _settings(settings, sources):
    settings.data.setdefault("news", {})["sources"] = sources
    settings.data["data"]["mode"] = "moex"
    return settings


def _handler(google_ok: bool):
    def h(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "DailyInfo.asmx" in url:
            return httpx.Response(200, text=SOAP)
        if "news.google.com" in url:
            if not google_ok:
                raise httpx.ConnectTimeout("blocked", request=request)
            return httpx.Response(200, text=RSS)
        return httpx.Response(404)
    return h


def _service(settings, google_ok=True, sources=("cbr_keyrate", "google_news")):
    t = httpx.MockTransport(_handler(google_ok))
    return NewsService(_settings(settings, list(sources)), moex=MoexClient(transport=t, max_retries=1), transport=t)


def test_key_rate_change_becomes_decision_news(settings):
    svc = _service(settings, sources=["cbr_keyrate"])
    items = svc.fetch(date(2024, 7, 1), date(2024, 8, 31))
    assert len(items) == 1
    n = items[0]
    assert n.published_at.date() == date(2024, 7, 26) and "повысил" in n.title and "16% → 18%" in n.title


def test_news_attach_to_price_event_with_coverage(settings):
    svc = _service(settings)
    ev = [PriceEvent(date=date(2024, 7, 11), change_pct=-6.1, close=290.0)]
    raw = svc.fetch(date(2024, 7, 5), date(2024, 7, 12), ticker="SBER", event_dates=[ev[0].date])
    out = match_news(ev, raw, "SBER")
    titles = [n.title for n in out[0].news]
    assert any("ключевую ставку" in t for t in titles) is False   # ставка 26.07 вне окна события 11.07
    assert any("рекордной прибыли" in t for t in titles)          # «- РБК» срезан, источник отдельно
    assert any(n.source == "РБК" for n in out[0].news)
    assert svc.coverage["СМИ (Google Новости)"].endswith("за период")


def test_blocked_google_is_reported_honestly(settings):
    svc = _service(settings, google_ok=False)
    raw = svc.fetch(date(2024, 7, 5), date(2024, 7, 12), ticker="SBER", event_dates=[date(2024, 7, 10)])
    assert not any(n.source != "Банк России" for n in raw)            # СМИ нет, остальные источники работают
    assert "VPN" in svc.coverage["СМИ (Google Новости)"]


def test_google_cache_is_labelled(settings):
    svc = _service(settings, sources=["google_news"])
    svc.fetch(date(2024, 7, 5), date(2024, 7, 12), ticker="SBER", event_dates=[date(2024, 7, 11)])
    blocked = _service(settings, google_ok=False, sources=["google_news"])
    raw = blocked.fetch(date(2024, 7, 5), date(2024, 7, 12), ticker="SBER", event_dates=[date(2024, 7, 11)])
    assert raw and blocked.coverage["СМИ (Google Новости)"] == "1 за период (из кеша)"   # без сети, но из кеша
