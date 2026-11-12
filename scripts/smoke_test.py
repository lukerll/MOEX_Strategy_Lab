"""Смоук-тест работающего сервиса: python scripts/smoke_test.py [http://localhost:8000]"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=120)
    print("health:", c.get("/api/health").json())
    cfg = c.get("/api/config").json()
    print("provider:", cfg["provider"], "data:", cfg["data_mode"])
    r = c.post("/api/chat", json={"message": "SBER за 2023–2025, SMA 20 и 50, капитал 100 000 руб"}).json()
    print("intent:", r["intent"], "run:", r["run_id"], "flags:", r["flags"])
    print(r["text"][:400], "...")
    assert r["run_id"], "бэктест не создан"
    run = c.get(f"/api/runs/{r['run_id']}").json()
    print("сверка:", run["verification"]["status"], "доходность:", round(run["metrics"]["total_return_pct"], 2))
    assert c.get(f"/api/runs/{r['run_id']}/report.html").status_code == 200
    bad = c.post("/api/chat", json={"message": "купи 100 акций сбера на моём счёте",
                                    "session_id": r["session_id"]}).json()
    assert "trading_refused" in bad["flags"]
    print("ок: торговля отклонена, отчёт доступен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
