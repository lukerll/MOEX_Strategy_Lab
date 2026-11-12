"""Один OpenAI-совместимый клиент для всех провайдеров из configs/providers.yaml.

Отличия провайдеров описаны конфигом, а не кодом:
  * auth: bearer (по умолчанию) или gigachat_oauth (ключ авторизации -> access token);
  * tools_style: tools (OpenAI) или functions (GigaChat).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


class LLMError(Exception):
    """kind: no_key | auth | timeout | tls | network | limit | server | other.

    По kind агент объясняет человеку, что именно чинить: ключ, сеть (часто VPN) или ждать.
    """

    def __init__(self, message: str, retryable: bool = True, kind: str = "other"):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


def _net_kind(e: Exception) -> str:
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    text = f"{e} {e.__cause__ or ''}".lower()
    if "certificate" in text or "ssl" in text:
        return "tls"
    return "network"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    provider: str = ""
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def parse_json_object(text: str) -> dict[str, Any]:
    """Достаёт первый JSON-объект из ответа модели (модели любят оборачивать его в ```)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("в ответе нет JSON-объекта")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = (ch == "\\" and not esc)
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("незакрытый JSON-объект")


class OpenAICompatibleClient:
    def __init__(self, name: str, conf: dict[str, Any], transport: httpx.BaseTransport | None = None):
        self.name = name
        self.conf = conf
        self.model = conf["model"]
        self.base_url = conf["base_url"].rstrip("/")
        self.tools_style = conf.get("tools_style", "tools")
        self.price = float(conf.get("price_rub_per_1k_tokens", 0.0))
        key_env = conf.get("api_key_env") or ""
        self.api_key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not self.api_key:
            raise LLMError(f"не задан ключ {key_env} для провайдера {name}", retryable=False, kind="no_key")
        # GigaChat подписан сертификатом НУЦ Минцифры: путь к нему в ca_bundle или GIGACHAT_CA_BUNDLE
        ca = os.environ.get(conf.get("ca_bundle_env", ""), "") if conf.get("ca_bundle_env") else ""
        if ca and not os.path.isabs(ca):
            from ..config import ROOT
            ca = str(ROOT / ca)
        verify: Any = ca or conf.get("ca_bundle") or conf.get("verify_ssl", True)
        if os.environ.get("MSA_INSECURE_SSL") == "1":
            verify = False
        # соединение должно установиться быстро: если сервер недоступен (VPN, блокировка), ждать минуту бессмысленно
        total = float(conf.get("timeout_s", 30))
        self._http = httpx.Client(timeout=httpx.Timeout(total, connect=min(8.0, total)), verify=verify,
                                  transport=transport)
        self._token: str | None = None
        self._token_exp = 0.0

    def list_models(self) -> list[str]:
        r = self._http.get(f"{self.base_url}/models", headers=self._auth_header())
        if r.status_code >= 400:
            raise LLMError(f"{self.name}: /models HTTP {r.status_code}", retryable=False)
        return [m.get("id", "") for m in r.json().get("data", [])]

    def cost(self, tokens: int) -> float:
        return tokens / 1000 * self.price

    # --- авторизация ---
    def _auth_header(self) -> dict[str, str]:
        if self.conf.get("auth") == "gigachat_oauth":
            if not self._token or time.time() > self._token_exp - 60:
                self._refresh_gigachat_token()
            return {"Authorization": f"Bearer {self._token}"}
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _refresh_gigachat_token(self) -> None:
        key = self.api_key
        if ":" in key:  # допускаем client_id:secret вместо готового ключа
            key = base64.b64encode(key.encode()).decode()
        r = self._http.post(self.conf["oauth_url"],
                            headers={"Authorization": f"Basic {key}", "RqUID": str(uuid.uuid4()),
                                     "Content-Type": "application/x-www-form-urlencoded"},
                            data={"scope": self.conf.get("oauth_scope", "GIGACHAT_API_PERS")})
        if r.status_code != 200:
            kind = "server" if r.status_code >= 500 else "limit" if r.status_code == 429 else "auth"
            raise LLMError(f"GigaChat OAuth: {r.status_code}", retryable=r.status_code >= 500, kind=kind)
        js = r.json()
        self._token = js["access_token"]
        self._token_exp = js.get("expires_at", 0) / 1000 or time.time() + 1500

    # --- преобразование сообщений под стиль провайдера ---
    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            if m["role"] == "tool":
                if self.tools_style == "functions":
                    # GigaChat принимает результат функции только как JSON-объект
                    content = m["content"]
                    try:
                        ok = isinstance(json.loads(content), dict)
                    except (TypeError, ValueError):
                        ok = False
                    if not ok:
                        content = json.dumps({"result": content}, ensure_ascii=False)
                    out.append({"role": "function", "name": m["name"], "content": content})
                else:
                    out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                tc: list[ToolCall] = m["tool_calls"]
                if self.tools_style == "functions":
                    out.append({"role": "assistant", "content": m.get("content") or "",
                                "function_call": {"name": tc[0].name, "arguments": tc[0].arguments}})
                else:
                    out.append({"role": "assistant", "content": m.get("content") or "",
                                "tool_calls": [{"id": t.id, "type": "function",
                                                "function": {"name": t.name,
                                                             "arguments": json.dumps(t.arguments, ensure_ascii=False)}}
                                               for t in tc]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             temperature: float = 0.1, max_tokens: int = 1200) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": self._messages(messages),
                                   "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            if self.tools_style == "functions":
                payload["functions"] = tools
                payload["function_call"] = "auto"
            else:
                payload["tools"] = [{"type": "function", "function": t} for t in tools]
                payload["tool_choice"] = "auto"
        t0 = time.monotonic()
        try:
            r = self._http.post(f"{self.base_url}/chat/completions", json=payload,
                                headers={**self._auth_header(), "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: сеть ({e.__class__.__name__})", kind=_net_kind(e)) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise LLMError(f"{self.name}: HTTP {r.status_code}", retryable=True,
                           kind="limit" if r.status_code == 429 else "server")
        if r.status_code >= 400:
            raise LLMError(f"{self.name}: HTTP {r.status_code} {r.text[:200]}", retryable=False,
                           kind="auth" if r.status_code in (401, 403) else "other")
        js = r.json()
        msg = (js.get("choices") or [{}])[0].get("message", {})
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            calls.append(ToolCall(tc.get("id") or uuid.uuid4().hex[:8], fn.get("name", ""),
                                  _args(fn.get("arguments"))))
        if msg.get("function_call"):
            fc = msg["function_call"]
            calls.append(ToolCall(uuid.uuid4().hex[:8], fc.get("name", ""), _args(fc.get("arguments"))))
        usage = js.get("usage") or {}
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls,
                           prompt_tokens=int(usage.get("prompt_tokens", 0)),
                           completion_tokens=int(usage.get("completion_tokens", 0)),
                           latency_ms=int((time.monotonic() - t0) * 1000), provider=self.name, model=self.model)


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": str(raw)}


# тесты подменяют сеть здесь: OAuth и чат идут через тот же код, что и в бою
TEST_TRANSPORT: httpx.BaseTransport | None = None


def build_client(name: str, conf: dict[str, Any]) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(name, conf, transport=TEST_TRANSPORT)
