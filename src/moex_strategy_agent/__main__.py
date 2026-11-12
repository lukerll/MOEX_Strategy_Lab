"""Запуск сервиса одной командой:

    python -m moex_strategy_agent            # живые данные MOEX ISS
    python -m moex_strategy_agent --demo     # синтетика, без сети
"""
from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser(prog="moex_strategy_agent")
    ap.add_argument("--host", default=os.environ.get("MSA_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MSA_PORT", "8000")))
    ap.add_argument("--demo", action="store_true", help="синтетические котировки вместо MOEX ISS")
    ap.add_argument("--provider", default=None, help="провайдер из configs/providers.yaml")
    args = ap.parse_args()
    if args.demo:
        os.environ["MSA_DATA_MODE"] = "demo"
    if args.provider:
        os.environ["MSA_PROVIDER"] = args.provider

    import uvicorn
    print(f"MOEX Strategy Agent: http://{args.host}:{args.port}  (API: /docs)")
    uvicorn.run("moex_strategy_agent.api.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
