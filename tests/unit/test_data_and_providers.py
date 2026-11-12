"""Клиент ISS и LLM-провайдеры на моках HTTP: формат, пагинация, повторы, стили function calling."""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from moex_strategy_agent.data import moex_client
from moex_strategy_agent.data.cache import CandleCache
from moex_strategy_agent.data.market_data import MarketData
from moex_strategy_agent.data.moex_client import DataSourceError, MoexClient, NoDataError
from moex_strategy_agent.data.quality import check_ohlcv
from moex_strategy_agent.llm.provider import LLMError, OpenAICompatibleClient

COLS = ["open", "close", "high", "low", "value", "volume", "begin", "end"]


def _candles(n: int, start: int = 0) -> list[list]:
    out = []
    for i in range(start, start + n):
        d = date.fromordinal(date(2024, 1, 1).toordinal() + i)
        out.append([100 + i, 101 + i, 102 + i, 99 + i, 1e6, 1000, f"{d} 00:00:00", f"{d} 23:59:59"])
    return out


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(moex_client.time, "sleep", lambda *_: None)


def test_candles_pagination_and_format():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        start = int(req.url.params["start"])
        calls.append(start)
        data = _candles(500, 0) if start == 0 else _candles(120, 500) if start == 500 else []
        assert req.url.params["interval"] == "24" and req.url.params["iss.meta"] == "off"
        return httpx.Response(200, json={"candles": {"columns": COLS, "data": data}})

    c = MoexClient(transport=httpx.MockTransport(handler))
    df = c.get_candles("SBER", "TQBR", date(2024, 1, 1), date(2025, 12, 31))
    assert calls == [0, 500] and len(df) == 620
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index[0].date() == date(2024, 1, 1)


def test_retries_on_5xx_then_success():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"candles": {"columns": COLS, "data": _candles(10)}})

    df = MoexClient(transport=httpx.MockTransport(handler), max_retries=3).get_candles(
        "SBER", "TQBR", date(2024, 1, 1), date(2024, 1, 10))
    assert n["i"] == 3 and len(df) == 10


def test_no_retry_on_4xx_and_empty_is_nodata():
    c = MoexClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(DataSourceError) as e:
        c.get_candles("SBER", "TQBR", date(2024, 1, 1), date(2024, 2, 1))
    assert not e.value.retryable
    c2 = MoexClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"candles": {"columns": COLS, "data": []}})))
    with pytest.raises(NoDataError):
        c2.get_candles("NOPE", "TQBR", date(2024, 1, 1), date(2024, 2, 1))


def test_market_data_falls_back_to_cache_when_iss_down(settings, tmp_path):
    s = settings.with_overrides({"data": {"mode": "moex"}})
    ok = MoexClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"candles": {"columns": COLS, "data": _candles(200)}})))
    md = MarketData(s, client=ok)
    df, info = md.history("SBER", "TQBR", date(2024, 1, 1), date(2024, 7, 1))
    assert info.source == "moex" and not info.is_synthetic

    down = MoexClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
                      max_retries=2)
    md2 = MarketData(s, client=down)
    df2, info2 = md2.history("SBER", "TQBR", date(2024, 1, 1), date(2024, 9, 1))   # шире кеша
    assert info2.source == "cache" and "кеш" in info2.warnings[0]


def test_quality_checks_flag_gaps_and_fix_ohlc():
    from moex_strategy_agent.data.demo_source import demo_ohlcv
    df = demo_ohlcv("GAZP")
    df = df[(df.index > "2021-06-01") & (df.index < "2022-12-31")].copy()
    df.iloc[5, df.columns.get_loc("High")] = df.iloc[5]["Low"] * 0.5
    clean, warns = check_ohlcv(df)
    assert any("разрыв" in w for w in warns) and any("High/Low" in w for w in warns)
    assert (clean["High"] >= clean[["Open", "Close"]].max(axis=1) - 1e-9).all()


def test_cache_merge(tmp_path):
    from moex_strategy_agent.data.moex_client import candles_to_ohlcv
    import pandas as pd
    c = CandleCache(tmp_path)
    a = candles_to_ohlcv(pd.DataFrame(_candles(30), columns=COLS))
    b = candles_to_ohlcv(pd.DataFrame(_candles(30, 20), columns=COLS))
    c.save("SBER", "TQBR", a, date(2024, 1, 1), date(2024, 1, 30))
    c.save("SBER", "TQBR", b, date(2024, 1, 21), date(2024, 2, 19))
    assert c.coverage("SBER", "TQBR") == (date(2024, 1, 1), date(2024, 2, 19))
    assert len(c.load("SBER", "TQBR")) == 50


# ---------------- LLM ----------------

def _client(handler, **conf):
    base = {"base_url": "http://x/v1", "model": "m", "api_key_env": ""}
    return OpenAICompatibleClient("p", {**base, **conf}, transport=httpx.MockTransport(handler))


def test_openai_tools_style_roundtrip():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_trades", "arguments": '{"run_id": "abc"}'}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    r = _client(handler, tools_style="tools").chat([{"role": "user", "content": "hi"}],
                                                    tools=[{"name": "get_trades", "parameters": {}}])
    assert seen["tools"][0]["type"] == "function" and r.tool_calls[0].arguments == {"run_id": "abc"}
    assert r.total_tokens == 15


def test_gigachat_functions_style_and_oauth():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/oauth"):
            assert req.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_at": 9_999_999_999_000})
        assert req.headers["Authorization"] == "Bearer tok"
        body = json.loads(req.content)
        assert "functions" in body and body["function_call"] == "auto"
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "function_call":
                                         {"name": "list_history", "arguments": {"limit": 3}}}}]})

    import os
    os.environ["TEST_GIGA_KEY"] = "id:secret"
    c = _client(handler, auth="gigachat_oauth", oauth_url="http://x/api/v2/oauth", api_key_env="TEST_GIGA_KEY",
                tools_style="functions")
    r = c.chat([{"role": "user", "content": "x"}], tools=[{"name": "list_history", "parameters": {}}])
    r2 = c.chat([{"role": "user", "content": "x"}], tools=[{"name": "list_history", "parameters": {}}])
    assert r.tool_calls[0].name == "list_history" and r2.tool_calls[0].arguments == {"limit": 3}
    assert calls.count("/api/v2/oauth") == 1          # токен кешируется


def test_provider_errors_are_classified():
    with pytest.raises(LLMError) as e:
        _client(lambda r: httpx.Response(429)).chat([{"role": "user", "content": "x"}])
    assert e.value.retryable
    with pytest.raises(LLMError) as e:
        _client(lambda r: httpx.Response(401, text="bad key")).chat([{"role": "user", "content": "x"}])
    assert not e.value.retryable
    with pytest.raises(LLMError):
        OpenAICompatibleClient("p", {"base_url": "http://x", "model": "m", "api_key_env": "NO_SUCH_KEY_ENV"})


def test_breaker_skips_dead_iss_and_serves_cache_instantly(settings):
    """С VPN биржа не отвечает: первый запрос ждёт таймауты, следующие сразу идут в кеш."""
    s = settings.with_overrides({"data": {"mode": "moex"}})
    ok = MoexClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"candles": {"columns": COLS, "data": _candles(200)}})))
    MarketData(s, client=ok).history("SBER", "TQBR", date(2024, 1, 1), date(2024, 7, 1))   # «скачали без VPN»

    calls = {"n": 0}

    def dead(r):
        calls["n"] += 1
        raise httpx.ConnectTimeout("vpn")
    down = MoexClient(transport=httpx.MockTransport(dead), max_retries=3)
    md = MarketData(s, client=down)
    _, i1 = md.history("SBER", "TQBR", date(2024, 1, 1), date(2024, 9, 1))
    assert i1.source == "cache" and calls["n"] == 3                       # честно попробовали три раза
    _, i2 = md.history("SBER", "TQBR", date(2024, 2, 1), date(2024, 9, 1))
    assert i2.source == "cache" and calls["n"] == 3                       # предохранитель: сеть не трогали
    assert "повторю попытку" in i2.warnings[0]
    with pytest.raises(moex_client.DataSourceError):                      # кеша нет: честный отказ, тоже без ожидания
        md.history("GAZP", "TQBR", date(2024, 1, 1), date(2024, 9, 1))
    assert calls["n"] == 3


def test_breaker_does_not_trip_on_http_errors():
    c = MoexClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(moex_client.DataSourceError):
        c.get_candles("SBER", "TQBR", date(2024, 1, 1), date(2024, 2, 1))
    assert not moex_client._down_until                                    # биржа ответила: это не обрыв сети
