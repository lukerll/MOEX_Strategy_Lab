"""Загрузка конфигурации: YAML + переменные окружения.

Код не знает, какая модель и какой источник данных используются: всё приходит отсюда.
"""
from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Корень проекта: MSA_ROOT или три уровня вверх от этого файла (src/moex_strategy_agent/config.py)
ROOT = Path(os.environ.get("MSA_ROOT", Path(__file__).resolve().parents[2]))

# Переменная окружения -> путь в settings
ENV_OVERRIDES = {
    "MSA_DATA_MODE": ("data", "mode"),
    "MSA_DB_PATH": ("app", "db_path"),
    "MSA_API_KEY": ("app", "api_key"),
    "MSA_REPORTS_DIR": ("app", "reports_dir"),
    "MSA_CACHE_DIR": ("data", "cache_dir"),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """Тонкая обёртка над словарём настроек с доступом через get("a.b.c")."""

    def __init__(self, data: dict[str, Any], providers: dict[str, Any]):
        self.data = data
        self.providers = providers

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, dotted: str) -> Path:
        p = Path(self.get(dotted))
        return p if p.is_absolute() else ROOT / p

    @property
    def active_provider(self) -> str:
        return os.environ.get("MSA_PROVIDER") or self.providers.get("active", "gigachat")

    @property
    def fallback_chain(self) -> list[str]:
        chain = [self.active_provider] + list(self.providers.get("fallback", []))
        seen, out = set(), []
        for name in chain:
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def provider_conf(self, name: str) -> dict[str, Any]:
        conf = self.providers.get("providers", {}).get(name)
        if conf is None:
            raise KeyError(f"Провайдер '{name}' не описан в configs/providers.yaml")
        return conf

    def with_overrides(self, overrides: dict[str, Any] | None = None,
                       provider: str | None = None) -> "Settings":
        data = _deep_merge(self.data, overrides or {})
        prov = copy.deepcopy(self.providers)
        if provider:
            prov["active"] = provider
        return Settings(data, prov)


def load_dotenv(path: Path | None = None) -> None:
    """Читает .env из корня проекта. Уже заданные переменные окружения не перезаписывает."""
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def load_settings(overrides: dict[str, Any] | None = None) -> Settings:
    if not os.environ.get("MSA_NO_DOTENV"):
        load_dotenv()
    data = _load_yaml(ROOT / "configs" / "settings.yaml")
    providers = _load_yaml(ROOT / "configs" / "providers.yaml")
    for env, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            data.setdefault(section, {})[key] = os.environ[env]
    return Settings(_deep_merge(data, overrides or {}), providers)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
