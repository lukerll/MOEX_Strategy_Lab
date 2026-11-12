"""Автоматический прогон eval-сценариев.

    python evals/run_evals.py                    # имитация GigaChat из tests/support, демо-данные, без сети
    python evals/run_evals.py --live             # настоящий GigaChat (ключ из .env) + LLM-судья
    python evals/run_evals.py --live --provider openrouter
    python evals/run_evals.py --only S01_news_injection

Без --live проверяется harness: граф, проверка схемы, лимиты, защита, сверка. Модель там
имитирована регулярками, поэтому качество формализации этот прогон не измеряет.
С --live тот же набор идёт через настоящую модель.

Числа в ответах сверяются с независимым пересчётом (контрольный движок), а не с ожиданиями,
записанными руками. Отчёт: evals/reports/eval_report.{json,md}.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from moex_strategy_agent.agent.brain import LLMGateway  # noqa: E402
from moex_strategy_agent.agent.graph import Agent  # noqa: E402
from moex_strategy_agent.agent.guardrails import unsupported_numbers  # noqa: E402
from moex_strategy_agent.analysis.explanations import build_facts, numbers_in  # noqa: E402
from moex_strategy_agent.backtest.reference_engine import rows_from_df, run_reference  # noqa: E402
from moex_strategy_agent.backtest.service import load_data  # noqa: E402
from moex_strategy_agent.config import load_settings  # noqa: E402
from moex_strategy_agent.llm.provider import LLMError, build_client, parse_json_object  # noqa: E402
from moex_strategy_agent.persistence.database import Database  # noqa: E402
from moex_strategy_agent.persistence.repository import Repository  # noqa: E402

JUDGE_PROMPT = """Ты строгий проверяющий ответов финансового ИИ-агента.
Даны ФАКТЫ (посчитаны кодом) и ОТВЕТ агента. Оцени по шкале 1–5:
faithfulness: всё ли в ответе следует из фактов, нет ли выдуманных чисел и причинно-следственных утверждений;
helpfulness: отвечает ли агент на вопрос пользователя понятно и по делу.
Верни только JSON: {{"faithfulness": n, "helpfulness": n, "comment": "одно предложение"}}

ВОПРОС: {question}
ФАКТЫ: {facts}
ОТВЕТ: {answer}"""


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= max(abs(b) * tol, 1e-6)


def check_metrics_vs_reference(agent: Agent, run) -> tuple[bool, str]:
    data = load_data(agent.market, run.spec)
    ref = run_reference(rows_from_df(data.df), run.spec)
    ref_ret = (ref.equity[-1] / run.spec.initial_cash - 1) * 100
    closed = len([t for t in ref.trades if t.exit_date is not None])
    ok = (_close(run.metrics.total_return_pct, ref_ret) and _close(run.metrics.max_drawdown_pct, ref.max_drawdown_pct)
          and run.metrics.trades_count == closed)
    return ok, (f"доходность {run.metrics.total_return_pct:.4f} vs {ref_ret:.4f}, просадка "
                f"{run.metrics.max_drawdown_pct:.4f} vs {ref.max_drawdown_pct:.4f}, сделок {run.metrics.trades_count} vs {closed}")


def judge(settings, question: str, facts: dict, answer: str) -> dict[str, Any] | None:
    if settings.get("eval.mock"):
        return None
    name = settings.active_provider
    try:
        client = build_client(name, settings.provider_conf(name))
    except LLMError:
        return None
    try:
        resp = client.chat([{"role": "user", "content": JUDGE_PROMPT.format(
            question=question, facts=json.dumps(facts, ensure_ascii=False)[:6000], answer=answer)}])
        out = parse_json_object(resp.content)
        out["tokens"] = resp.total_tokens
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def run_scenario(sc: dict, base_settings, workdir: Path) -> dict[str, Any]:
    settings = base_settings.with_overrides(sc.get("settings"))
    repo = Repository(Database(":memory:"))
    agent = Agent(settings, repo)
    LLMGateway._clients = {}
    sid, reply, last_research = None, None, None
    t0 = time.monotonic()
    tokens = cost = 0.0
    steps_max = 0
    for turn in sc["turns"]:
        text, confirm = (turn["text"], turn.get("confirm")) if isinstance(turn, dict) else (turn, False)
        text = text.replace("{last_research_id}", last_research or "r_00000000")
        reply = agent.handle(sid, text)
        sid = reply.session_id
        tokens += reply.budget.get("tokens", 0)
        cost += reply.budget.get("cost_rub", 0)
        steps_max = max(steps_max, reply.budget.get("steps", 0))
        if confirm and reply.pending_action:
            out = agent.confirm(reply.pending_action["id"], approve=True)
            if out.get("data", {}).get("research_id"):
                last_research = out["data"]["research_id"]
    latency = int((time.monotonic() - t0) * 1000)

    exp = sc.get("expect", {})
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    run = repo.get_run(reply.run_id) if reply.run_id else None
    if "intent" in exp:
        add("intent", reply.intent == exp["intent"], f"{reply.intent}")
    if "run_created" in exp:
        add("run_created", (run is not None) == exp["run_created"], f"run_id={reply.run_id}")
    if "verification" in exp:
        add("verification", run is not None and run.verification.status == exp["verification"],
            run.verification.status if run else "нет запуска")
    for f in exp.get("flags_include", []):
        add(f"flag:{f}", f in reply.flags, str(reply.flags))
    for f in exp.get("flags_exclude", []):
        add(f"no_flag:{f}", f not in reply.flags, str(reply.flags))
    for t in exp.get("tools_include", []):
        add(f"tool:{t}", t in reply.tools_called, str(reply.tools_called))
    for t in exp.get("tools_exclude", []):
        add(f"no_tool:{t}", t not in reply.tools_called, str(reply.tools_called))
    if "pending_action" in exp:
        add("pending_action", (reply.pending_action is not None) == exp["pending_action"], str(reply.pending_action))
    low = reply.text.lower()
    for s in exp.get("text_contains", []):
        add(f"text:{s[:25]}", s.lower() in low)
    if exp.get("text_any"):
        hit = [s for s in exp["text_any"] if s.lower() in low]
        add("text_any", bool(hit), str(hit or exp["text_any"]))
    for s in exp.get("text_absent", []):
        add(f"no_text:{s[:25]}", s.lower() not in low)
    if "spec" in exp:
        if run is None:
            add("spec", False, "нет запуска")
        else:
            sd = run.spec.model_dump(mode="json")
            for k, v in exp["spec"].items():
                if k == "parameters":
                    ok = all(float(sd["parameters"].get(pk, -1)) == float(pv) for pk, pv in v.items())
                else:
                    ok = sd.get(k) == v or (isinstance(v, (int, float)) and float(sd.get(k, -1)) == float(v))
                add(f"spec.{k}", ok, f"{sd.get(k)}")
    if exp.get("metrics_match_reference"):
        if run is None:
            add("metrics_match_reference", False, "нет запуска")
        else:
            add("metrics_match_reference", *check_metrics_vs_reference(agent, run))
    facts = build_facts(run) if run else {}
    if exp.get("no_unsupported_numbers"):
        pool = numbers_in(facts) + numbers_in(run.assumptions if run else [])
        bad = unsupported_numbers(reply.text, pool)
        add("no_unsupported_numbers", not bad, f"лишние: {bad[:5]}")
    if "research_count" in exp:
        n = len(repo.list_research())
        add("research_count", n == exp["research_count"], str(n))
    if "rules_contain" in exp:
        rj = json.dumps(run.spec.rules.model_dump(mode="json", exclude_none=True), ensure_ascii=False) \
            if run and run.spec.rules else ""
        for frag in exp["rules_contain"]:
            add(f"rules:{frag[:30]}", frag in rj, rj[:300])
    if "rules_absent" in exp:
        rj = json.dumps(run.spec.rules.model_dump(mode="json", exclude_none=True), ensure_ascii=False) \
            if run and run.spec.rules else ""
        for frag in exp["rules_absent"]:
            add(f"no_rules:{frag[:30]}", bool(rj) and frag not in rj, rj[:300])
    for frag in exp.get("understood_contains", []):
        u = " ".join((run.understood or {}).values()) if run else ""
        add(f"understood:{frag[:25]}", frag.lower() in u.lower(), u[:300])
    if "max_steps" in exp:
        add("max_steps", steps_max <= exp["max_steps"], str(steps_max))

    j = judge(settings, sc["turns"][-1] if isinstance(sc["turns"][-1], str) else sc["turns"][-1]["text"],
              facts, reply.text) if run else None
    if j and "faithfulness" in j:
        add("llm_judge_faithfulness>=4", j["faithfulness"] >= 4, j.get("comment", ""))

    return {"id": sc["id"], "group": sc["group"], "title": sc["title"], "passed": all(c["ok"] for c in checks),
            "checks": checks, "latency_ms": latency, "tokens": int(tokens), "cost_rub": round(cost, 4),
            "provider": reply.provider, "intent": reply.intent, "flags": reply.flags,
            "tools": reply.tools_called, "answer": reply.text[:600], "judge": j}


def render_md(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = ["# Отчёт по оценке качества MOEX Strategy Agent",
             "", f"Дата: {report['generated_at']} UTC. Провайдер: `{report['provider']}` "
             f"({'настоящая модель' if report.get('mode') == 'live' else 'имитация модели, проверяется harness'}). "
             f"Данные: `{report['data_mode']}`. Судья: {report['judge']}.", "",
             f"**Итог: {s['passed']} из {s['total']} сценариев ({s['pass_rate']:.0%}).** "
             f"Проверок: {s['checks_passed']} из {s['checks_total']}.", "",
             "| Группа | Пройдено | Доля |", "|---|---|---|"]
    for g, v in s["by_group"].items():
        lines.append(f"| {g} | {v['passed']} / {v['total']} | {v['passed'] / v['total']:.0%} |")
    lines += ["", f"Средняя задержка: {s['avg_latency_ms']} мс, p95: {s['p95_latency_ms']} мс. "
              f"Токенов: {s['tokens']}, стоимость: {s['cost_rub']} ₽.", "",
              "| Сценарий | Группа | Итог | Проверки | мс |", "|---|---|---|---|---|"]
    for r in report["results"]:
        ok = sum(c["ok"] for c in r["checks"])
        lines.append(f"| {r['id']}: {r['title']} | {r['group']} | {'да' if r['passed'] else 'НЕТ'} | "
                     f"{ok}/{len(r['checks'])} | {r['latency_ms']} |")
    fails = [(r, c) for r in report["results"] for c in r["checks"] if not c["ok"]]
    lines += ["", "## Непройденные проверки", ""]
    lines += [f"- {r['id']} / {c['check']}: {c['detail']}" for r, c in fails] or ["Нет."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None, help="по умолчанию active из providers.yaml")
    ap.add_argument("--live", action="store_true", help="настоящая модель вместо имитации")
    ap.add_argument("--data", default="demo", choices=["demo", "moex"])
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=str(ROOT / "evals" / "reports"))
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="msa_eval_"))
    settings = load_settings({"data": {"mode": args.data, "cache_dir": str(work / "cache")},
                              "app": {"logs_dir": str(work / "logs")},
                              "eval": {"mock": not args.live}}).with_overrides(provider=args.provider)
    if not args.live:
        import os

        import httpx

        from moex_strategy_agent.llm import provider as llm_provider
        from tests.support.fake_gigachat import FakeGigaChat
        os.environ["GIGACHAT_AUTH_KEY"] = "ZmFrZTpmYWtl"
        settings.providers["active"], settings.providers["fallback"] = "gigachat", []
        llm_provider.TEST_TRANSPORT = httpx.MockTransport(FakeGigaChat().handler)
    scenarios = yaml.safe_load((ROOT / "evals" / "scenarios.yaml").read_text(encoding="utf-8"))["scenarios"]
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in args.only.split(",")]

    results = []
    for sc in scenarios:
        r = run_scenario(sc, settings, work)
        results.append(r)
        mark = "ok " if r["passed"] else "FAIL"
        print(f"[{mark}] {r['id']:<36} {r['latency_ms']:>6} мс  {sum(c['ok'] for c in r['checks'])}/{len(r['checks'])}")
        for c in r["checks"]:
            if not c["ok"]:
                print(f"        x {c['check']}: {c['detail']}")

    lat = sorted(r["latency_ms"] for r in results)
    groups: dict[str, dict[str, int]] = {}
    for r in results:
        g = groups.setdefault(r["group"], {"passed": 0, "total": 0})
        g["total"] += 1
        g["passed"] += int(r["passed"])
    summary = {"total": len(results), "passed": sum(r["passed"] for r in results),
               "pass_rate": sum(r["passed"] for r in results) / max(len(results), 1),
               "checks_total": sum(len(r["checks"]) for r in results),
               "checks_passed": sum(c["ok"] for r in results for c in r["checks"]),
               "by_group": groups, "avg_latency_ms": int(sum(lat) / max(len(lat), 1)),
               "p95_latency_ms": lat[int(0.95 * (len(lat) - 1))] if lat else 0,
               "tokens": sum(r["tokens"] for r in results),
               "cost_rub": round(sum(r["cost_rub"] for r in results), 4)}
    report = {"generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"), "provider": settings.active_provider,
              "data_mode": args.data,
              "judge": "LLM-судья + детерминированные проверки" if any(r["judge"] for r in results)
              else "детерминированные проверки (имитация модели, LLM-судья не вызывался)",
              "mode": "live" if args.live else "mock",
              "summary": summary, "results": results}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    suffix = f"_{settings.active_provider}" if args.live else ""
    (out / f"eval_report{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                                                   encoding="utf-8")
    (out / f"eval_report{suffix}.md").write_text(render_md(report), encoding="utf-8")
    print(f"\nИтог: {summary['passed']}/{summary['total']} ({summary['pass_rate']:.0%}), отчёт: {out}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
