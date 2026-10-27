"""Инициализация асинхронного движка БД и сессий.

SQLite (MVP):  sqlite+aiosqlite:///./data/bot.db
PostgreSQL:    postgresql+asyncpg://user:pass@host:5432/db
               (нужен драйвер asyncpg; схемы переносимы без изменений)
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base

logger = logging.getLogger(__name__)


class Database:
    """Обёртка над движком и фабрикой сессий."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        kwargs: dict[str, object] = {"echo": echo, "future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)  # type: ignore[arg-type]
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._sqlite_patched = False

    def _patch_sqlite(self) -> None:
        """Включить foreign keys и WAL для SQLite (для Postgres не нужно)."""
        if self._sqlite_patched or not self.url.startswith("sqlite"):
            self._sqlite_patched = True
            return
        sync_engine = self.engine.sync_engine

        @event.listens_for(sync_engine, "connect")
        def _on_connect(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

        self._sqlite_patched = True

    async def create_all(self) -> None:
        self._patch_sqlite()
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("Схема БД готова (%s)", self.url.split("://", 1)[0])

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    def make_session(self) -> AsyncSession:
        return self.session_factory()

    async def close(self) -> None:
        await self.engine.dispose()
        logger.info("Соединение с БД закрыто")


async def init_database(url: str, *, echo: bool = False) -> Database:
    database = Database(url, echo=echo)
    await database.create_all()
    return database
