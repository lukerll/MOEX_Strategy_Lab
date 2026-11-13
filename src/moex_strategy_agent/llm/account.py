"""Аккаунт GigaChat: тип доступа, модели, остаток токенов и выбор модели для всего сервиса.

Название тарифа API не сообщает. Известно три вещи: версия API из scope, список моделей
из /models и остаток по группам моделей из /balance. Квоты Freemium для физлиц взяты
из тарифов Сбера (365 млн токенов на 12 месяцев) и нужны только как шкала для полосы.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .provider import LLMError, build_client

# группа моделей: шаблон названия, квота Freemium физлица, цена за 1 млн токенов, ₽
GROUPS: list[tuple[str, str, int | None, int | None]] = [
    ("Ultra", r"ultra", 50_000_000, None),
    ("Max", r"max", 25_000_000, 650),
    ("Pro", r"pro", 40_000_000, 500),
    ("Lite", r"", 250_000_000, 65),
]
SCOPES = {"GIGACHAT_API_PERS": "Физическое лицо",
          "GIGACHAT_API_B2B": "ИП или юрлицо, предоплата",
          "GIGACHAT_API_CORP": "ИП или юрлицо, постоплата"}
STUDIO_URL = "https://developers.sber.ru/studio"
KNOWN_MODELS = ["GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max"]
_SKIP = re.compile(r"embed", re.IGNORECASE)
# в выборе по одной модели на группу: название для человека и коротко, чем она хороша для агента
CARDS = {
    "Lite": ("GigaChat Lite", "Отвечает быстрее всех. Хватает для простых стратегий вроде пересечения средних, "
                              "в правилах из нескольких условий ошибается чаще"),
    "Pro": ("GigaChat Pro", "Точнее Lite следует сложным инструкциям. Подходит для стратегий из двух-трёх условий"),
    "Max": ("GigaChat Max", "Лучше всех разбирает сложные стратегии и объясняет результат. Стоит по умолчанию"),
    "Ultra": ("GigaChat Ultra", "Новый флагман Сбера. На нашем агенте пока не проверялась"),
}
PREFER = {"Lite": ["GigaChat-2", "GigaChat"], "Pro": ["GigaChat-2-Pro", "GigaChat-Pro"], "Max": ["GigaChat-2-Max", "GigaChat-Max"]}


def group_of(model: str) -> str:
    low = model.lower()
    return next(g for g, pat, _, _ in GROUPS if re.search(pat, low))


def price_per_1m(model: str) -> int | None:
    g = group_of(model)
    return next(p for name, _, _, p in GROUPS if name == g)


class Account:
    """Сведения об аккаунте с кешем на минуту: окно «Расход» открывают часто, а сеть до Сбера небыстрая."""

    TTL = 60

    def __init__(self, settings, clients: dict):
        self.settings = settings
        self.clients = clients                      # тот же кеш клиентов, что у агента
        self._cache: tuple[float, dict] | None = None
        self.model_file = self._model_file()
        self._apply_saved_model()

    # ---------- выбор модели ----------
    def _model_file(self) -> Path | None:
        db = self.settings.get("app.db_path")
        if not db or db == ":memory:":
            return None
        return self.settings.path("app.db_path").with_name("model.json")

    def _apply_saved_model(self) -> None:
        if not self.model_file or not self.model_file.exists():
            return
        try:
            saved = json.loads(self.model_file.read_text(encoding="utf-8")).get(self.settings.active_provider)
        except (OSError, ValueError):
            return
        if isinstance(saved, str) and saved:
            self._set_conf_model(saved)

    def _set_conf_model(self, model: str) -> None:
        name = self.settings.active_provider
        conf = self.settings.provider_conf(name)
        conf["model"] = model
        p = price_per_1m(model) if conf.get("auth") == "gigachat_oauth" else None
        if p is not None:
            conf["price_rub_per_1k_tokens"] = p / 1000
        self.clients.pop(name, None)                 # клиент пересоберётся уже с новой моделью

    def set_model(self, model: str) -> dict:
        info = self.info(fresh=False)
        allowed = [m["id"] for m in info["models"]] or KNOWN_MODELS
        if model not in allowed:
            raise ValueError(f"модели {model} нет среди доступных: {', '.join(allowed)}")
        self._set_conf_model(model)
        if self.model_file:
            try:
                data = json.loads(self.model_file.read_text(encoding="utf-8")) if self.model_file.exists() else {}
            except (OSError, ValueError):
                data = {}
            data[self.settings.active_provider] = model
            self.model_file.parent.mkdir(parents=True, exist_ok=True)
            self.model_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self._cache = None
        return self.info(fresh=False)

    # ---------- сведения ----------
    def info(self, fresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache and not fresh and now - self._cache[0] < self.TTL:
            return self._decorate(self._cache[1])
        remote = self._fetch()
        self._cache = (now, remote)
        return self._decorate(remote)

    def _fetch(self) -> dict[str, Any]:
        name = self.settings.active_provider
        conf = self.settings.provider_conf(name)
        out: dict[str, Any] = {"ok": False, "models_raw": [], "balance_raw": None, "error": None}
        try:
            client = self.clients.get(name) or build_client(name, conf)
            if client is None:
                out["error"] = "no_key"
                return out
            self.clients[name] = client
            out["models_raw"] = client.list_models()
            out["ok"] = True
            try:
                out["balance_raw"] = client.balance()
            except (LLMError, httpx.HTTPError, ValueError):
                out["balance_raw"] = None
        except LLMError as e:
            out["error"] = e.kind
        except httpx.HTTPError:
            out["error"] = "network"
        return out

    def _decorate(self, remote: dict[str, Any]) -> dict[str, Any]:
        """Добавляет к ответу сети то, что меняется без неё: текущую модель и цены."""
        name = self.settings.active_provider
        conf = self.settings.provider_conf(name)
        model = str(conf.get("model", ""))
        giga = conf.get("auth") == "gigachat_oauth"
        scope = conf.get("oauth_scope", "GIGACHAT_API_PERS") if giga else None
        ids = [m for m in remote["models_raw"] if not _SKIP.search(m)] if remote["ok"] else []
        if model and model not in ids and remote["ok"]:
            ids.append(model)
        models = [{"id": m, "group": group_of(m) if giga else None, "price_rub_per_1m": price_per_1m(m) if giga else None}
                  for m in sorted(ids, key=lambda m: ["Lite", "Pro", "Max", "Ultra"].index(group_of(m)) if giga else 0)]
        balance = None
        if giga and remote["balance_raw"] is not None:
            balance = []
            seen: dict[str, dict] = {}
            for row in remote["balance_raw"]:
                if _SKIP.search(row["model"]):
                    continue
                g = group_of(row["model"])
                quota = next(q for n, _, q, _ in GROUPS if n == g) if scope == "GIGACHAT_API_PERS" else None
                item = seen.setdefault(g, {"group": g, "tokens": 0, "quota": quota, "current": False})
                item["tokens"] = max(item["tokens"], int(row["tokens"]))   # Lite и Lite 2 делят один пакет
            for g in ["Lite", "Pro", "Max", "Ultra"]:
                if g in seen:
                    seen[g]["current"] = g == group_of(model)
                    balance.append(seen[g])
        choices = []
        if giga:
            pool = models or [{"id": m, "group": group_of(m)} for m in KNOWN_MODELS]
            for g in ["Lite", "Pro", "Max", "Ultra"]:
                ids_g = [m["id"] for m in pool if m["group"] == g]
                if not ids_g:
                    continue
                if group_of(model) == g and model in ids_g:
                    pick = model                                     # текущая модель остаётся выбранной под своим именем
                else:
                    pref = [x for x in PREFER.get(g, []) if x in ids_g]
                    plain = sorted((x for x in ids_g if "preview" not in x.lower()), reverse=True)
                    pick = (pref or plain or sorted(ids_g, reverse=True))[0]
                title, about = CARDS[g]
                choices.append({"group": g, "id": pick, "title": title, "about": about, "current": group_of(model) == g})
        return {"ok": remote["ok"], "error": remote["error"], "provider": name, "model": model, "choices": choices,
                "group": group_of(model) if giga else None, "price_rub_per_1m": price_per_1m(model) if giga else None,
                "scope": scope, "scope_label": SCOPES.get(scope or "", None),
                "studio_url": os.environ.get("GIGACHAT_STUDIO_URL") or (STUDIO_URL if giga else None),
                "models": models, "balance": balance}
