"""Проверка подключения к GigaChat на вашей машине.

    python scripts/check_gigachat.py
    python scripts/check_gigachat.py --model GigaChat-2      # проверить другую модель

Ключ берётся из .env (GIGACHAT_AUTH_KEY) и на экран не выводится. Вывод скрипта можно
спокойно пересылать: в нём нет ни ключа, ни токена.

Шаги: ключ на месте → токен получен → модель доступна → три стратегии формализованы.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from moex_strategy_agent.agent.brain import LLMBrain, LLMGateway, ModelUnavailable  # noqa: E402
from moex_strategy_agent.agent.guardrails import Budget  # noqa: E402
from moex_strategy_agent.config import load_settings  # noqa: E402
from moex_strategy_agent.domain.rules import RuleSet, describe_rules  # noqa: E402
from moex_strategy_agent.llm.provider import LLMError, build_client  # noqa: E402
from moex_strategy_agent.persistence.database import Database  # noqa: E402
from moex_strategy_agent.persistence.repository import Repository  # noqa: E402
from moex_strategy_agent.telemetry.tracing import Tracer  # noqa: E402

PHRASES = [
    "Сбер за 2021–2025: покупаю, когда цена выше средней за 200 дней и RSI ниже 40, продаю при RSI выше 70, стоп 7%",
    "Газпром, MACD пересекает сигнальную линию снизу вверх, выхожу при обратном пересечении или через 30 дней",
    "Лукойл, покупаю на просадках",
]

SSL_HELP = """
  Python не доверяет сертификату GigaChat: он выпущен Национальным удостоверяющим центром
  Минцифры, которого нет в стандартном наборе. Как исправить:
    1. Скачайте «Корневой сертификат» с https://www.gosuslugi.ru/crt
       (файл russian_trusted_root_ca.cer, формат PEM).
    2. Положите его, например, в папку проекта: certs/russian_trusted_root_ca.cer
    3. Добавьте в .env строку:  GIGACHAT_CA_BUNDLE=certs/russian_trusted_root_ca.cer
  Только для разовой проверки можно отключить проверку сертификата: MSA_INSECURE_SSL=1
  (в работе так не оставляйте)."""


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    settings = load_settings({"app": {"logs_dir": str(ROOT / "logs")}}).with_overrides(provider="gigachat")
    conf = dict(settings.provider_conf("gigachat"))
    if args.model:
        conf["model"] = args.model
    settings.providers["providers"]["gigachat"] = conf
    settings.providers["fallback"] = []               # проверяем именно GigaChat, без запасных
    import os
    ca = os.environ.get("GIGACHAT_CA_BUNDLE", "")
    if ca and not Path(ca).is_absolute():
        os.environ["GIGACHAT_CA_BUNDLE"] = str(ROOT / ca)

    print(f"Проверка GigaChat, модель {conf['model']}\n")

    # 1. ключ
    print("1. Ключ авторизации")
    key = os.environ.get("GIGACHAT_AUTH_KEY", "")
    if not key:
        fail("GIGACHAT_AUTH_KEY не найден ни в .env, ни в окружении")
        print("  Создайте файл .env в корне проекта (можно из .env.example) и впишите строку")
        print("  GIGACHAT_AUTH_KEY=<ключ авторизации из личного кабинета developers.sber.ru>")
        return 1
    ok(f"ключ найден, длина {len(key)} символов" + (" (похоже на client_id:secret, будет закодирован)" if ":" in key else ""))
    if os.environ.get("GIGACHAT_CA_BUNDLE"):
        p = Path(os.environ["GIGACHAT_CA_BUNDLE"])
        (ok if p.exists() else fail)(f"сертификат: {p.name} {'найден' if p.exists() else 'не найден по указанному пути'}")

    # 2. токен
    print("\n2. Токен доступа (OAuth)")
    try:
        client = build_client("gigachat", conf)
        t0 = time.monotonic()
        client._refresh_gigachat_token()
        ok(f"токен получен за {int((time.monotonic() - t0) * 1000)} мс, действует ~30 минут")
    except LLMError as e:
        fail(str(e))
        if "401" in str(e) or "400" in str(e):
            print("  Ключ не принят. Проверьте, что скопирован именно «Ключ авторизации» целиком,")
            print("  без пробелов и кавычек, и что scope в configs/providers.yaml подходит (физлицо: GIGACHAT_API_PERS).")
        return 1
    except (httpx.ConnectError, ssl.SSLError) as e:
        fail(f"соединение не установлено: {e.__class__.__name__}")
        if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
            print(SSL_HELP)
        else:
            print("  Проверьте интернет и что ngw.devices.sberbank.ru открывается (VPN иногда мешает).")
        return 1

    # 3. модели
    print("\n3. Доступные модели")
    try:
        models = client.list_models()
        ok(", ".join(models) or "список пуст")
        if conf["model"] not in models:
            fail(f"{conf['model']} нет в списке: поменяйте model в configs/providers.yaml или запустите с --model")
            return 1
    except (LLMError, httpx.HTTPError) as e:
        fail(f"список моделей не получен: {e}")

    # остаток токенов: метод отвечает не на всех тарифах, это не ошибка
    try:
        bal = client.balance()
    except (LLMError, httpx.HTTPError, ValueError):
        bal = None
    if bal:
        ok("остаток: " + ", ".join(f"{b['model']} {b['tokens']:,}".replace(",", " ") for b in bal))
    else:
        print("  остаток токенов API не сообщает, смотрите его в личном кабинете")

    # 4. формализация
    print("\n4. Формализация стратегий")
    repo = Repository(Database(":memory:"))
    tracer = Tracer(repo, None, None)
    budget = Budget(max_steps=100, max_tool_calls=0, max_llm_calls=30, max_tokens=200000, max_cost_rub=200)
    LLMGateway._clients = {"gigachat": client}
    brain = LLMBrain(LLMGateway(settings, tracer, budget), settings)
    good = 0
    for i, text in enumerate(PHRASES, 1):
        print(f"\n  [{i}] «{text}»")
        t0 = time.monotonic()
        try:
            u = brain.understand(text, today=date.today(), last_spec=None, summary="")
        except ModelUnavailable as e:
            fail(f"модель не ответила: {e}")
            continue
        ms = int((time.monotonic() - t0) * 1000)
        patch = u.spec_patch
        print(f"      понял за {ms} мс, намерение {u.intent}, бумага {patch.get('ticker', '?')}, "
              f"стратегия {patch.get('strategy_type', '?')}")
        if u.question:
            print(f"      спрашивает: {u.question}")
        if patch.get("rules"):
            d = describe_rules(RuleSet.model_validate(patch["rules"]))
            print(f"      {d['entry']}")
            if d["exit"]:
                print(f"      {d['exit']}")
        elif patch.get("parameters"):
            print(f"      параметры: {json.dumps(patch['parameters'], ensure_ascii=False)}")
        for a in u.assumptions:
            print(f"      допущение: {a}")
        expected_question = i == 3
        if expected_question == bool(u.question) and (u.question or patch.get("rules") or patch.get("strategy_type")):
            good += 1
            ok("как и ожидалось" if not expected_question else "переспросил, как и должен")
        else:
            fail("ожидал " + ("уточняющий вопрос" if expected_question else "готовые правила"))
    repairs = sum(1 for e in repo.get_trace(tracer.trace_id) if e["name"] == "formalize_rejected")
    snap = budget.snapshot()
    print(f"\nИтого: {good} из {len(PHRASES)}. Исправлений по ошибкам схемы: {repairs}. "
          f"Токенов: {snap.get('tokens', 0)}, примерно {snap.get('cost_rub', 0):.2f} ₽ по тарифу из конфига.")
    if good == len(PHRASES):
        print("GigaChat подключён и формализует стратегии. Запускайте: python -m moex_strategy_agent")
    return 0 if good == len(PHRASES) else 2


if __name__ == "__main__":
    raise SystemExit(main())
