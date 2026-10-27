"""Интерфейс источника данных.

Любой источник (реальный MRKT, демонстрационный генератор, будущий Portals /
Tonnel / официальный маркет Telegram) реализует `ListingSource`. Остальной код
проекта работает только с этим интерфейсом и с нормализованным `Listing`,
поэтому замена источника не затрагивает фильтры, оценки и уведомления.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from app.domain import FetchRequest, FetchResult, Listing, SourceStatus


@dataclass(slots=True)
class ImageResult:
    """Картинка лота, полученная от источника/провайдера изображений."""

    data: bytes
    content_type: str = "image/jpeg"
    url: str | None = None
    origin: str = "unknown"      # mrkt_cdn | changes_tg | demo | ...
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.data) and self.error is None


class SourceError(Exception):
    """Базовая ошибка источника."""


class SourceAuthError(SourceError):
    """Нет токена, токен не принят или истёк."""


class SourceRateLimitError(SourceError):
    """Источник ответил ограничением частоты запросов (HTTP 429)."""

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SourceUnavailableError(SourceError):
    """Источник недоступен (сеть, 5xx, неизвестный ответ)."""


class SourceNotFoundError(SourceError):
    """Эндпоинт или объект не найдены."""


class ListingSource(abc.ABC):
    """Контракт источника предложений."""

    #: Идентификатор источника, попадает в Listing.source и в логи.
    name: str = "base"
    #: True, если источник отдаёт НЕ реальные данные (демо/фикстуры).
    is_test_data: bool = False

    @abc.abstractmethod
    async def status(self) -> SourceStatus:
        """Проверить доступность источника, не меняя состояние."""

    @abc.abstractmethod
    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Получить список предложений по запросу."""

    async def collections(self) -> list[str]:
        """Известные названия коллекций (для подсказок в UI). Пусто = неизвестно."""
        return []

    async def models(self, collection: str) -> list[str]:
        return []

    async def backdrops(self, collection: str) -> list[str]:
        return []

    async def symbols(self, collection: str) -> list[str]:
        return []

    async def get_image(self, listing: Listing) -> ImageResult | None:
        """Вернуть изображение лота, если источник/провайдер его отдаёт."""
        return None

    async def close(self) -> None:
        """Освободить ресурсы (HTTP-сессии и т. п.)."""

    async def __aenter__(self) -> "ListingSource":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
