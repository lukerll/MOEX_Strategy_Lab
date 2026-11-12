"""Контейнер зависимостей сервиса: настройки, база, репозиторий, агент."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..agent.graph import Agent
from ..config import Settings
from ..persistence.database import Database
from ..persistence.repository import Repository


@dataclass
class Services:
    settings: Settings
    db: Database
    repo: Repository
    agent: Agent


def build_services(settings: Settings) -> Services:
    db = Database(settings.get("app.db_path") if settings.get("app.db_path") == ":memory:"
                  else settings.path("app.db_path"))
    repo = Repository(db)
    return Services(settings, db, repo, Agent(settings, repo))


def services(request: Request) -> Services:
    return request.app.state.services


def get_run_or_404(svc: Services, run_id: str):
    res = svc.repo.get_run(run_id)
    if res is None:
        raise HTTPException(404, f"запуск {run_id} не найден")
    return res
