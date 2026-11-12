"""HTTP-сервис. Запуск одной командой: uvicorn moex_strategy_agent.api.main:app

Состояние живёт в SQLite, поэтому переживает перезапуск процесса.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..config import ROOT, Settings, get_settings
from ..data.demo_source import DEMO_UNIVERSE
from .deps import build_services
from .routes import backtests, chat, reports

WEB_DIR = ROOT / "ui" / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="MOEX Strategy Agent", version="0.1.0",
                  description="Агент для формализации и бэктестинга стратегий на данных Мосбиржи")
    app.state.services = build_services(settings)
    hits: dict[str, deque] = defaultdict(deque)
    limit = int(settings.get("app.rate_limit_per_minute", 30))
    api_key = settings.get("app.api_key") or ""

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in ("/api/health",):
            if api_key and request.headers.get("X-API-Key") != api_key:
                return JSONResponse({"detail": "нужен заголовок X-API-Key"}, status_code=401)
            if request.method == "POST":
                ip = request.client.host if request.client else "?"
                now = time.monotonic()
                q = hits[ip]
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= limit:
                    return JSONResponse({"detail": "слишком часто, подождите минуту"}, status_code=429)
                q.append(now)
        return await call_next(request)

    app.include_router(chat.router)
    app.include_router(backtests.router)
    app.include_router(reports.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/config")
    def config() -> dict:
        s = app.state.services.settings
        active = s.active_provider
        conf = s.provider_conf(active)
        import os
        # есть ли ключ хоть у одного провайдера цепочки (сам ключ наружу не отдаётся)
        llm_ready = any(not s.provider_conf(n).get("api_key_env") or os.environ.get(s.provider_conf(n)["api_key_env"])
                        for n in s.fallback_chain if n in s.providers.get("providers", {}))
        return {"provider": active, "model": conf.get("model"), "fallback": s.fallback_chain, "llm_ready": bool(llm_ready),
                "data_mode": s.get("data.mode"), "default_cash": s.get("backtest.default_cash", 100000),
                "demo_tickers": list(DEMO_UNIVERSE) if s.get("data.mode") == "demo" else [],
                "limits": s.get("agent"), "stats": app.state.services.repo.trace_stats()}

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/about", include_in_schema=False)
        def about() -> RedirectResponse:
            return RedirectResponse("/")            # описание теперь панель «Как это работает» в приложении

    return app


app = create_app()
