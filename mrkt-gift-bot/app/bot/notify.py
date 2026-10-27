"""Доставка уведомлений: sink для планировщика мониторинга.

Классифицирует ошибки Telegram, чтобы:
* «бот заблокирован / чат не найден» — не спамить повторными попытками;
* «Flood control exceeded» — выдержать паузу и продолжить;
* прочее — залогировать и сообщить пользователю в /status.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from app.bot.cards import send_card
from app.config import Settings
from app.domain import ScoredListing, utcnow
from app.sources.factory import ImageProvider

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliveryStats:
    total: int = 0
    delivered: int = 0
    failed: int = 0
    blocked_chats: set[int] = field(default_factory=set)
    flood_until: datetime | None = None
    last_error: str | None = None
    last_delivery_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "delivered": self.delivered,
            "failed": self.failed,
            "blocked_chats": len(self.blocked_chats),
            "flood_until": self.flood_until.isoformat() if self.flood_until else None,
            "last_error": self.last_error,
            "last_delivery_at": self.last_delivery_at.isoformat() if self.last_delivery_at else None,
        }


class Notifier:
    """Отправка карточек лота пользователю."""

    def __init__(self, bot: Bot, *, settings: Settings, images: ImageProvider) -> None:
        self.bot = bot
        self.settings = settings
        self.images = images
        self.stats = DeliveryStats()

    async def __call__(self, user_id: int, scored: ScoredListing, meta: dict[str, Any]) -> bool:
        return await self.notify(user_id, scored, meta)

    async def notify(self, user_id: int, scored: ScoredListing, meta: dict[str, Any]) -> bool:
        if user_id in self.stats.blocked_chats:
            logger.info("Пропускаем доставку в чат %s: пользователь заблокировал бота", user_id)
            return False

        if self.stats.flood_until and utcnow() < self.stats.flood_until:
            wait = (self.stats.flood_until - utcnow()).total_seconds()
            logger.info("Telegram flood-control: ждём %.0f c", wait)
            await asyncio.sleep(min(wait, 60.0))

        self.stats.total += 1
        try:
            delivered = await send_card(
                self.bot,
                user_id,
                scored,
                settings=self.settings,
                images=self.images,
                meta=meta,
            )
        except TelegramRetryAfter as exc:
            self.stats.failed += 1
            self.stats.flood_until = utcnow() + timedelta(seconds=int(exc.retry_after) + 1)
            self.stats.last_error = f"flood control: {exc.retry_after} c"
            logger.warning("Telegram попросил подождать %s c", exc.retry_after)
            await asyncio.sleep(min(int(exc.retry_after) + 1, 60))
            try:
                delivered = await send_card(
                    self.bot, user_id, scored,
                    settings=self.settings, images=self.images, meta=meta,
                )
            except Exception as exc2:  # noqa: BLE001
                self.stats.last_error = repr(exc2)[:300]
                logger.warning("Повторная доставка не удалась: %r", exc2)
                return False
        except TelegramForbiddenError as exc:
            self.stats.failed += 1
            self.stats.blocked_chats.add(user_id)
            self.stats.last_error = f"чат {user_id} недоступен: {exc}"
            logger.warning("Бот заблокирован пользователем %s", user_id)
            return False
        except TelegramUnauthorizedError as exc:
            self.stats.failed += 1
            self.stats.last_error = f"неверный токен бота: {exc}"
            logger.error("BOT_TOKEN не принят Telegram: %s", exc)
            return False
        except TelegramBadRequest as exc:
            self.stats.failed += 1
            self.stats.last_error = f"bad request: {exc}"[:300]
            logger.warning("TelegramBadRequest при доставке: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.stats.failed += 1
            self.stats.last_error = repr(exc)[:300]
            logger.exception("Непредвиденная ошибка доставки пользователю %s", user_id)
            return False

        if delivered:
            self.stats.delivered += 1
            self.stats.last_delivery_at = utcnow()
        else:
            self.stats.failed += 1
        return delivered

    async def send_text(self, user_id: int, text: str, **kwargs: Any) -> bool:
        """Служебное текстовое сообщение (ошибки мониторинга, отчёты)."""
        try:
            await self.bot.send_message(user_id, text[:4000], **kwargs)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("send_text в %s не удался: %r", user_id, exc)
            return False
