"""HTTP-сервис. Запуск одной командой: uvicorn moex_strategy_agent.api.main:app

Состояние живёт в SQLite, поэтому переживает перезапуск процесса.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..config import ROOT, Settings, get_settings
from ..data.demo_source import DEMO_UNIVERSE
from ..agent.brain import LLMGateway
from ..llm.account import Account
from .deps import build_services
from .routes import backtests, chat, reports

WEB_DIR = ROOT / "ui" / "web"

API_DESCRIPTION = """Всё, что умеет интерфейс, доступно запросами. Интерфейс в браузере сам ничего не считает: он вызывает
эти же адреса.

Чтобы попробовать, раскройте запрос, нажмите **Try it out**, затем **Execute**. Начать проще всего с
«Сообщение агенту»: в поле `message` напишите стратегию, например `Сбербанк за 2024 год, SMA 20/50`.

Сохранение и удаление идут в два шага: запрос создаёт действие, а выполняет его только «Подтвердить действие».
Реальных сделок через API нет, такого запроса не существует."""

API_TAGS = [
    {"name": "Диалог с агентом", "description": "Сообщения агенту, история, подтверждения, трасса и расход"},
    {"name": "Бэктесты и избранное", "description": "Расчёт без агента, запуски и сохранённые исследования"},
    {"name": "Отчёты", "description": "Отчёт HTML и таблицы CSV для Excel"},
    {"name": "Сервис", "description": "Состояние сервиса и настройки"},
]

# Swagger в цветах проекта: шрифт и знак свои, лишнее спрятано
DOCS_CSS = """<style>
@font-face { font-family: "Inter"; font-weight: 400; src: url(/static/fonts/inter-cyrillic-400-normal.woff2) format("woff2"); unicode-range: U+0400-045F; }
@font-face { font-family: "Inter"; font-weight: 400; src: url(/static/fonts/inter-latin-400-normal.woff2) format("woff2"); unicode-range: U+0000-00FF, U+2000-206F; }
@font-face { font-family: "Inter"; font-weight: 500; src: url(/static/fonts/inter-cyrillic-500-normal.woff2) format("woff2"); unicode-range: U+0400-045F; }
@font-face { font-family: "Inter"; font-weight: 500; src: url(/static/fonts/inter-latin-500-normal.woff2) format("woff2"); unicode-range: U+0000-00FF, U+2000-206F; }
body { margin: 0; background: #f6f8f7; }
.swagger-ui, .swagger-ui .info *, .swagger-ui .opblock-tag, .swagger-ui .opblock .opblock-summary-description { font-family: Inter, system-ui, sans-serif !important; color: #0a1a14; }
.swagger-ui .info hgroup.main { display: flex; flex-direction: column; } .swagger-ui .info .title { display: flex; align-items: center; gap: 12px; }
.swagger-ui .info .title::before { content: ""; width: 34px; height: 31px; flex: none; background: url(/static/logo.svg) center / contain no-repeat; }
.swagger-ui .info .title small.version-stamp, .swagger-ui .info hgroup.main > a { display: none; }
.swagger-ui .info .description, .swagger-ui .info .description .markdown { margin: 0 !important; padding: 0 !important; }
.swagger-ui .markdown code, .swagger-ui .renderedMarkdown code { color: #0b7a48 !important; background: #e5ece8 !important; font-weight: 500; }
.swagger-ui .opblock.opblock-get, .swagger-ui .opblock.opblock-post { background: #fff; border-color: rgba(0,10,6,.12); }
.swagger-ui .opblock.is-open { border-color: rgba(11,122,72,.45); }
.swagger-ui .opblock .opblock-section-header { background: #f6f8f7; box-shadow: none; }
.swagger-ui .opblock-body pre.microlight, .swagger-ui .highlight-code > .microlight { background: #0a1a14 !important; border-radius: 10px; }
.swagger-ui .tab li button.tablinks, .swagger-ui .opblock-description-wrapper p { font-family: Inter, system-ui, sans-serif; }
.swagger-ui .info { margin: 26px 0 30px; } .swagger-ui .info .title { font-size: 30px; font-weight: 500; letter-spacing: -0.02em; }
.swagger-ui .info .title small { background: #e5ece8 !important; } .swagger-ui .info .title small pre { color: #24312b !important; }
.swagger-ui .info .title small.version-stamp { background: #0b7a48 !important; } .swagger-ui .info .title small.version-stamp pre { color: #fff !important; }
.swagger-ui .info .description p, .swagger-ui .info .description li { font-size: 14px; line-height: 1.55; max-width: 760px; color: #24312b; margin: 0 0 12px !important; }
.swagger-ui .info a { color: #0b7a48; } .swagger-ui .info code { font-size: 13px; background: #e5ece8; padding: 1px 5px; border-radius: 4px; }
.swagger-ui .opblock-tag { font-size: 20px; font-weight: 500; border-bottom: 1px solid rgba(0,10,6,.09); }
.swagger-ui .opblock-tag small { font-size: 13px; color: #5e6262; }
.swagger-ui .opblock { border-radius: 12px; box-shadow: none; border: 1px solid rgba(0,10,6,.12); background: #fff; margin-bottom: 8px; }
.swagger-ui .opblock .opblock-summary { border: 0; } .swagger-ui .opblock .opblock-summary-method { border-radius: 999px; font-family: Inter, sans-serif; font-weight: 500; min-width: 66px; }
.swagger-ui .opblock.opblock-get .opblock-summary-method { background: #24312b; } .swagger-ui .opblock.opblock-post .opblock-summary-method { background: #0b7a48; }
.swagger-ui .opblock .opblock-summary-path { font-size: 14px; } .swagger-ui .opblock .opblock-summary-description { font-size: 14px; font-weight: 500; }
.swagger-ui .btn.execute { background: #0b7a48; border-color: #0b7a48; border-radius: 999px; }
.swagger-ui .btn { border-radius: 999px; } .swagger-ui .scheme-container { display: none; }
</style>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="MOEX Strategy Agent", version="1.0.0", description=API_DESCRIPTION, openapi_tags=API_TAGS,
                  docs_url=None, redoc_url=None)
    app.state.services = build_services(settings)
    app.state.account = Account(settings, LLMGateway._clients)
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

    @app.get("/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        page = get_swagger_ui_html(openapi_url=app.openapi_url, title="MOEX Strategy Agent: API",
                                   swagger_favicon_url="/static/icon.svg",
                                   swagger_js_url="/static/vendor/swagger/swagger-ui-bundle.js",   # Swagger UI 5.34.1, работает без интернета
                                   swagger_css_url="/static/vendor/swagger/swagger-ui.css",
                                   swagger_ui_parameters={"defaultModelsExpandDepth": -1, "docExpansion": "list",
                                                          "displayRequestDuration": True, "syntaxHighlight": False})
        html = page.body.decode().replace("</head>", DOCS_CSS + "</head>")
        return HTMLResponse(html)

    @app.get("/api/health", tags=["Сервис"], summary="Сервис жив", description="Отвечает `ok`, если сервер запущен.")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/config", tags=["Сервис"], summary="Настройки сервиса",
             description="Модель и провайдеры, готов ли ключ, режим котировок, капитал по умолчанию и лимиты. Сами ключи не отдаются.")
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

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(WEB_DIR / "icon-32.png", media_type="image/png")    # для браузеров, которые просят иконку по старому адресу

        @app.get("/about", include_in_schema=False)
        def about() -> RedirectResponse:
            return RedirectResponse("/")            # описание теперь раздел «Как это работает» в настройках

    return app


app = create_app()
