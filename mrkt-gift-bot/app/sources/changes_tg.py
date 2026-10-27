"""Клиент публичного API @GiftChanges (api.changes.tg / cdn.changes.tg).

ЗАЧЕМ
-----
API не требует ключей и отдаёт то, чего нет в приватном MRKT:
* точные цвета фонов (`centerColor`, `edgeColor`, `patternColor`, `textColor`,
  а также готовые `hex`) — основа надёжного определения монохромности;
* PNG моделей и символов — для анализа цвета по картинке и для карточек,
  когда изображение от MRKT недоступно;
* редкости моделей/фонов/символов — для оценки совпадения с фильтрами.

Проверено фактическими запросами 27.10.2026:
    GET https://api.changes.tg/llm                            -> 200 (SKILL.md)
    GET https://api.changes.tg/gift/plushpepe                 -> 200 (модели/фоны/символы + hex-цвета)
    GET https://api.changes.tg/model/plushpepe/donatello.png  -> 200 image/png
    GET https://api.changes.tg/backdrop/plushpepe/aquamarine/info -> 200 JSON с hex

ОБЯЗАТЕЛЬНОЕ УСЛОВИЕ ИСПОЛЬЗОВАНИЯ
----------------------------------
API бесплатен при условии видимой атрибуции. Кредит «Powered by @GiftChanges
(api.changes.tg)» выводится в командах /start, /help и /credits бота
(см. app/bot/i18n.py) и продублирован в README. Не удаляйте его.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from app.config import Settings
from app.domain import int_to_rgb
from app.sources.base import ImageResult

logger = logging.getLogger(__name__)

ATTRIBUTION = "Powered by @GiftChanges (api.changes.tg)"


class ChangesTGError(Exception):
    pass


class ChangesTGClient:
    """Тонкий асинхронный клиент с кэшем и троттлингом."""

    def __init__(self, settings: Settings, *, session: aiohttp.ClientSession | None = None) -> None:
        self._settings = settings
        self._base = (settings.changes_api_url or "https://api.changes.tg").rstrip("/")
        self._session = session
        self._own_session = session is None
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self._negative_cache: dict[str, float] = {}
        self.enabled = bool(settings.changes_api_enabled)

    # ------------------------------------------------------------------ сессия
    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=max(5.0, self._settings.mrkt_timeout))
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._own_session = True
        return self._session

    async def _throttle(self) -> None:
        delay = max(0.0, float(self._settings.changes_request_delay))
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    # ------------------------------------------------------------------ запрос
    async def _get_json(self, path: str, *, ttl: float | None = None) -> Any:
        if not self.enabled:
            raise ChangesTGError("API @GiftChanges отключён (CHANGES_API_ENABLED=false)")
        cache_ttl = ttl if ttl is not None else float(self._settings.changes_cache_ttl)
        now = time.monotonic()
        if path in self._cache:
            cached_at, value = self._cache[path]
            if now - cached_at < cache_ttl:
                return value
        # Не долбим эндпоинт, который недавно ответил ошибкой.
        if now - self._negative_cache.get(path, 0.0) < min(cache_ttl, 300.0):
            raise ChangesTGError(f"{path}: временно недоступен")

        session = self._ensure_session()
        url = f"{self._base}/{path.lstrip('/')}"
        async with self._lock:
            await self._throttle()
            try:
                async with session.get(url, headers={"Accept": "application/json"}) as response:
                    if response.status == 404:
                        self._negative_cache[path] = now
                        raise ChangesTGError(f"{path}: HTTP 404")
                    if response.status != 200:
                        self._negative_cache[path] = now
                        raise ChangesTGError(f"{path}: HTTP {response.status}")
                    data = await response.json(content_type=None)
            except aiohttp.ClientError as exc:
                self._negative_cache[path] = now
                raise ChangesTGError(f"{path}: {exc!r}") from exc
        self._cache[path] = (now, data)
        self._negative_cache.pop(path, None)
        return data

    async def _get_bytes(self, path: str) -> tuple[bytes, str]:
        if not self.enabled:
            raise ChangesTGError("API @GiftChanges отключён")
        session = self._ensure_session()
        url = f"{self._base}/{path.lstrip('/')}"
        async with self._lock:
            await self._throttle()
            try:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise ChangesTGError(f"{path}: HTTP {response.status}")
                    data = await response.read()
                    content_type = response.headers.get("Content-Type", "image/png")
            except aiohttp.ClientError as exc:
                raise ChangesTGError(f"{path}: {exc!r}") from exc
        limit = int(self._settings.image_max_bytes)
        if limit and len(data) > limit:
            raise ChangesTGError(f"{path}: файл больше лимита ({len(data)})")
        return data, content_type.split(";")[0].strip()

    # ------------------------------------------------------------------ данные
    async def gift_details(self, collection: str) -> dict[str, Any] | None:
        """Модели, фоны (с цветами) и символы коллекции."""
        if not collection:
            return None
        try:
            data = await self._get_json(f"gift/{collection}")
        except ChangesTGError as exc:
            logger.debug("GiftChanges gift/%s: %s", collection, exc)
            return None
        return data if isinstance(data, dict) else None

    async def backdrop_info(self, collection: str, backdrop: str) -> dict[str, Any] | None:
        if not collection or not backdrop:
            return None
        try:
            data = await self._get_json(f"backdrop/{collection}/{backdrop}/info")
        except ChangesTGError as exc:
            logger.debug("GiftChanges backdrop/%s/%s: %s", collection, backdrop, exc)
            return None
        return data if isinstance(data, dict) else None

    async def backdrop_colors(
        self, collection: str, backdrop: str
    ) -> dict[str, tuple[int, int, int] | None]:
        """Цвета фона как RGB: center / edge / pattern / text."""
        info = await self.backdrop_info(collection, backdrop)
        if not info:
            details = await self.gift_details(collection)
            if details:
                for row in details.get("backdrops") or []:
                    if isinstance(row, dict) and str(row.get("name", "")).lower() == backdrop.strip().lower():
                        info = row
                        break
        if not info:
            return {}
        return {
            "center": _rgb_from_entry(info, "centerColor"),
            "edge": _rgb_from_entry(info, "edgeColor"),
            "pattern": _rgb_from_entry(info, "patternColor"),
            "text": _rgb_from_entry(info, "textColor"),
        }

    async def rarities(self, collection: str) -> dict[str, dict[str, float]]:
        """Редкости в процентах: {'models': {name: pct}, 'backdrops': ..., 'symbols': ...}."""
        details = await self.gift_details(collection)
        if not details:
            return {}
        out: dict[str, dict[str, float]] = {"models": {}, "backdrops": {}, "symbols": {}}
        for key, section in (("models", "models"), ("backdrops", "backdrops"), ("symbols", "symbols")):
            for row in details.get(key) or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                pct = row.get("rarity")
                if pct is None:
                    permille = row.get("rarityPermille")
                    pct = (float(permille) / 10.0) if permille is not None else None
                if pct is not None:
                    try:
                        out[section][name.lower()] = float(pct)
                    except (TypeError, ValueError):
                        continue
        return out

    # --------------------------------------------------------------- картинки
    async def model_png(self, collection: str, model: str, size: int = 256) -> ImageResult | None:
        """PNG модели подарка (для анализа цвета и для карточки-заглушки)."""
        if not collection or not model:
            return None
        size = size if size in (64, 128, 256, 512, 1024) else 256
        path = f"model/{collection}/{model}.png?size={size}"
        try:
            data, content_type = await self._get_bytes(path)
        except ChangesTGError as exc:
            logger.debug("GiftChanges %s: %s", path, exc)
            return None
        return ImageResult(data=data, content_type=content_type, url=f"{self._base}/{path}",
                           origin="changes_tg")

    async def original_png(self, collection: str, size: int = 256) -> ImageResult | None:
        """PNG «оригинала» (неулучшенного стикера) коллекции."""
        if not collection:
            return None
        size = size if size in (64, 128, 256, 512, 1024) else 256
        path = f"original/{collection}.png?size={size}"
        try:
            data, content_type = await self._get_bytes(path)
        except ChangesTGError as exc:
            logger.debug("GiftChanges %s: %s", path, exc)
            return None
        return ImageResult(data=data, content_type=content_type, url=f"{self._base}/{path}",
                           origin="changes_tg")

    async def health(self) -> dict[str, Any]:
        try:
            data = await self._get_json("health", ttl=60)
        except ChangesTGError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "data": data}

    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def _rgb_from_entry(info: dict[str, Any], key: str) -> tuple[int, int, int] | None:
    """Взять RGB из поля-числа или из вложенного `hex`."""
    hex_map = info.get("hex")
    if isinstance(hex_map, dict):
        raw = hex_map.get(key)
        if isinstance(raw, str) and raw.startswith("#") and len(raw) == 7:
            try:
                value = int(raw[1:], 16)
            except ValueError:
                value = None
            if value is not None:
                return int_to_rgb(value)
    value = info.get(key)
    if isinstance(value, int):
        return int_to_rgb(value)
    if isinstance(value, str):
        try:
            return int_to_rgb(int(value))
        except ValueError:
            return None
    return None
