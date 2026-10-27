"""Фабрика источников: сборка источника по конфигурации.

`SOURCE=demo`  -> DemoSource  (тестовые данные, работает без ключей)
`SOURCE=mrkt`  -> MrktSource  (реальный приватный API; без токена честно
                               сообщает о недоступности и не выдаёт фейк)

Источник изображений (`ImageProvider`) отделяет «где берём картинку» от
«где берём лот»: для MRKT это CDN-ключ из ответа API, при неудаче —
fallback на публичный API @GiftChanges; для демо — локальная генерация.
"""

from __future__ import annotations

import io
import logging
import collections.abc as cabc

from app.config import Settings
from app.domain import Listing
from app.sources.base import ImageResult, ListingSource
from app.sources.changes_tg import ChangesTGClient
from app.sources.demo import DemoSource
from app.sources.mrkt import MrktSource

logger = logging.getLogger(__name__)


class ImageProvider:
    """Единая точка получения изображений лота с fallback-цепочкой."""

    def __init__(
        self,
        source: ListingSource,
        changes: ChangesTGClient | None = None,
        *,
        settings: Settings,
    ) -> None:
        self._source = source
        self._changes = changes
        self._settings = settings

    async def fetch(self, listing: Listing) -> ImageResult | None:
        if not self._settings.image_enabled:
            return None

        # 1) Собственный механизм источника (CDN MRKT / локальная генерация демо).
        try:
            result = await self._source.get_image(listing)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_image от источника упал: %r", exc)
            result = None
        if result and result.ok:
            return result

        # 2) Fallback: публичный API @GiftChanges (модель -> оригинал).
        if self._changes is not None and listing.collection_name:
            if listing.model_name:
                png = await self._changes.model_png(
                    listing.collection_name, listing.model_name, size=self._png_size()
                )
                if png and png.ok:
                    return png
            original = await self._changes.original_png(listing.collection_name, size=self._png_size())
            if original and original.ok:
                return original
        return None

    def _png_size(self) -> int:
        size = int(self._settings.image_size or 256)
        for allowed in (64, 128, 256, 512, 1024):
            if size <= allowed:
                return allowed
        return 1024


def convert_for_telegram(result: ImageResult | None, size: int = 512) -> bytes | None:
    """Превратить произвольную картинку в JPEG, который принимает Bot API.

    Telegram для sendPhoto принимает JPEG по прямой ссылке; WebP/PNG/TGS
    надёжнее конвертировать на своей стороне. Возвращает None, если конвертация
    невозможна — тогда бот отправит текстовую карточку (см. cards.py).
    """
    if result is None or not result.ok:
        return None
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return None

    try:
        with Image.open(io.BytesIO(result.data)) as image:
            image = image.convert("RGB")
            if max(image.size) > size:
                image.thumbnail((size, size), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось конвертировать изображение в JPEG: %r", exc)
        return None


def build_source(settings: Settings) -> tuple[ListingSource, ChangesTGClient | None, ImageProvider]:
    """Собрать источник, клиент @GiftChanges и провайдер изображений."""
    changes: ChangesTGClient | None = None
    if settings.changes_api_enabled:
        changes = ChangesTGClient(settings)

    if settings.source == "mrkt":
        token_provider: cabc.Callable[[], cabc.Awaitable[str | None]] | None = None
        try:
            from app.sources.mrkt_auth import build_token_provider

            token_provider = build_token_provider(settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось собрать провайдер токена MRKT: %r", exc)

        # Fallback на @GiftChanges делает ImageProvider, поэтому image_fetcher
        # источнику не передаётся.
        source: ListingSource = MrktSource(settings, token_provider=token_provider)
        if not settings.mrkt_token and token_provider is None:
            logger.warning(
                "SOURCE=mrkt, но MRKT_TOKEN пуст и авто-вход не настроен. "
                "Источник будет сообщать о недоступности; реальных данных не будет."
            )
    else:
        source = DemoSource()

    provider = ImageProvider(source, changes, settings=settings)
    return source, changes, provider


async def close_all(*closables: object) -> None:
    for item in closables:
        close = getattr(item, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if isinstance(result, cabc.Awaitable):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.debug("Ошибка при закрытии %r: %r", item, exc)


__all__ = [
    "ImageProvider",
    "build_source",
    "close_all",
    "convert_for_telegram",
    "ChangesTGClient",
    "DemoSource",
    "MrktSource",
]
