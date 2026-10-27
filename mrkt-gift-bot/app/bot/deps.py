"""Middleware и зависимости для обработчиков aiogram 3.

Каждый хендлер получает:
* `repo`  — репозиторий в рамках своей сессии БД (сессия закрывается
            автоматически после обработки события);
* `ctx`   — PipelineContext (источник, провайдер изображений, детектор);
* `notifier`, `settings`, `monitor`, `bot`.

Отдельный middleware проверяет право доступа (ALLOWED_USER_IDS).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, cast

from aiogram import BaseMiddleware, Router
from aiogram.types import Message, TelegramObject, Update

from app.config import Settings
from app.db.db import Database
from app.db.repo import Repository

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]]


class AccessMiddleware(BaseMiddleware):
    """Пускать только разрешённых пользователей, если список задан."""

    def __init__(self, settings: Settings) -> None:
        self.allowed, self.admins = settings.id_lists()

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not self.allowed:
            return await handler(event, data)

        user = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        if user_id is None:
            # Событие без пользователя (например, обновление inline-запроса).
            return await handler(event, data)

        if int(user_id) in self.allowed:
            return await handler(event, data)

        logger.info("Отказ в доступе пользователю %s (не в ALLOWED_USER_IDS)", user_id)
        if isinstance(event, Message):
            await event.answer(
                "⛔ Этот бот работает в закрытом режиме.\n"
                "Ваш Telegram ID не добавлен в ALLOWED_USER_IDS.\n"
                f"Ваш ID: <code>{user_id}</code>"
            )
        return None


class AdminMiddleware(BaseMiddleware):
    """Добавляет флаг `is_admin` в данные обработчика."""

    def __init__(self, settings: Settings) -> None:
        self.allowed, self.admins = settings.id_lists()

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = getattr(user, "id", None)
        data["is_admin"] = bool(user_id is not None and int(user_id) in self.admins)
        return await handler(event, data)


class RepositoryMiddleware(BaseMiddleware):
    """Создаёт сессию БД и репозиторий на время обработки события."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Handler,
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with self.db.session_factory() as session:
            data["db"] = self.db
            data["repo"] = Repository(session)
            try:
                return await handler(event, data)
            finally:
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001
                    pass


def setup_middlewares(router: Router, *, db: Database, settings: Settings) -> None:
    router.update.middleware(AccessMiddleware(settings))
    router.update.middleware(RepositoryMiddleware(db))
    router.update.middleware(AdminMiddleware(settings))


__all__ = [
    "AccessMiddleware",
    "AdminMiddleware",
    "RepositoryMiddleware",
    "setup_middlewares",
    "cast",
    "Update",
]
