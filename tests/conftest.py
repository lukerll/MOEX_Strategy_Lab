from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from moex_strategy_agent.agent.brain import LLMGateway
from moex_strategy_agent.agent.graph import Agent
from moex_strategy_agent.config import load_settings
from moex_strategy_agent.domain.schemas import StrategySpec
from moex_strategy_agent.domain.validators import validate_spec
from moex_strategy_agent.llm import provider as llm_provider
from moex_strategy_agent.llm.provider import OpenAICompatibleClient
from tests.support.fake_gigachat import FakeGigaChat
from moex_strategy_agent.persistence.database import Database
from moex_strategy_agent.persistence.repository import Repository


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings({
        "data": {"mode": "demo", "cache_dir": str(tmp_path / "cache")},
        "app": {"logs_dir": str(tmp_path / "logs"), "db_path": str(tmp_path / "t.sqlite3"),
                "reports_dir": str(tmp_path / "reports")},
    }).with_overrides(provider="gigachat")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch, settings):
    """Вместо сети имитация GigaChat: тот же OAuth и тот же клиент, что в бою."""
    fake = FakeGigaChat()
    monkeypatch.setenv("GIGACHAT_AUTH_KEY", "dGVzdDp0ZXN0")
    monkeypatch.setenv("MSA_NO_DOTENV", "1")
    monkeypatch.delenv("MSA_PROVIDER", raising=False)
    monkeypatch.setattr(llm_provider, "TEST_TRANSPORT", httpx.MockTransport(fake.handler))
    settings.providers["fallback"] = []
    return fake


@pytest.fixture
def repo():
    return Repository(Database(":memory:"))


@pytest.fixture
def agent(settings, repo):
    return Agent(settings, repo)


@pytest.fixture(autouse=True)
def _reset_llm_clients():
    from moex_strategy_agent.data.moex_client import reset_breaker
    from moex_strategy_agent.data import news_client
    news_client._google_down_until[0] = 0.0
    news_client._keyrate_cache.clear()
    LLMGateway._clients = {}
    reset_breaker()
    yield
    LLMGateway._clients = {}
    reset_breaker()


def make_spec(**kw: Any) -> StrategySpec:
    base = dict(ticker="SBER", start_date=date(2022, 1, 1), end_date=date(2025, 12, 31),
                strategy_type="SMA_CROSSOVER", parameters={"fast_period": 20, "slow_period": 50})
    base.update(kw)
    return validate_spec(StrategySpec(**base)).spec


class ScriptedLLM:
    """Мок OpenAI-совместимого API: отвечает по сценарию и записывает все запросы."""

    def __init__(self, responder: Callable[[dict], dict]):
        self.responder = responder
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        return httpx.Response(200, json=self.responder(body))


def chat_response(content: str = "", tool_calls: list | None = None, tokens: int = 100) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                             for i, (n, a) in enumerate(tool_calls)]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": tokens, "completion_tokens": tokens // 4}}


@pytest.fixture
def llm_settings(settings):
    """Настройки с отдельным мок-провайдером 'mock', ответы которого пишет сам тест."""
    s = settings.with_overrides(provider="mock")
    s.providers["providers"]["mock"] = {"kind": "openai_compatible", "base_url": "http://mock/v1",
                                        "model": "mock-1", "api_key_env": "", "tools_style": "tools",
                                        "price_rub_per_1k_tokens": 1.0}
    s.providers["fallback"] = []
    return s


def install_mock(settings, scripted: ScriptedLLM, name: str = "mock") -> None:
    conf = settings.provider_conf(name)
    LLMGateway._clients[name] = OpenAICompatibleClient(name, conf, transport=httpx.MockTransport(scripted.handler))
