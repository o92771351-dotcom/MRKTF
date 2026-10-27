"""Источник «реальный MRKT».

ЧТО ЗДЕСЬ РЕАЛИЗОВАНО
---------------------
Клиент приватного API MRKT (`https://api.tgmrkt.io/api/v1`) по протоколу,
восстановленному из открытых клиентов (amrkt, mrkts/tgmrkt) и неофициальной
документации boostNT/MRKT-API, а также из JS-бандла фронтенда cdn.tgmrkt.io.

Использованные эндпоинты:
    POST /auth                 — обмен Telegram initData на токен
    POST /gifts/saling         — активные предложения (пагинация курсором)
    GET  /gifts/collections    — коллекции: флор, объём, ключи картинок
    POST /gifts/models         — модели коллекций
    POST /gifts/backdrops      — фоны коллекций
    GET  /me                   — профиль (используется для проверки токена)

ЧТО ПРОВЕРЕНО ФАКТИЧЕСКИ (27.10.2026, запросы из песочницы)
-----------------------------------------------------------
* api.tgmrkt.io отвечает; `POST /api/v1/auth` без валидных initData → HTTP 403,
  тело `Forbid 1`.
* `POST /api/v1/gifts/saling` и `GET /api/v1/gifts/collections` БЕЗ валидного
  токена → HTTP 404 с пустым телом (в т.ч. с подложным `Authorization`).
  То есть маршруты закрыты авторизацией и скрывают своё существование.
* Фронтенд (cdn.tgmrkt.io/index.html, bundle 51961) подтверждает поля лота:
  `salePrice`, `salePriceWithoutFee`, `floorPriceNanoTONsByCollection`,
  `floorPriceNanoTONsByBackdropModel`, `backdropColors*Color`,
  `modelStickerThumbnailKey` / `modelStickerKey` (относительные ключи CDN —
  см. `_absolute_media_url`), `floorPriceNanoTONsBy*`, `backdropColors*Color`.

  Особенности живого API, подтверждённые 27.10.2026 рабочим токеном:
  * все четыре массива фильтра ОБЯЗАТЕЛЬНЫ в теле запроса (иначе HTTP 400);
  * пустой `collectionNames` -> `{"gifts": [], "total": 0}`: «весь рынок»
    одним запросом не отдаётся, нужен перебор коллекций;
  * `GET /gifts/collections` работает и возвращает 134 коллекции;
  * курсорная пагинация работает;
  * цены: `salePrice` (с комиссией) и `salePriceWithoutFee`, nanoTON.
* CSP фронтенда разрешает `img-src https:` и `connect-src https://api.tgmrkt.io`.

ЧТО НЕ ПОДТВЕРЖДЕНО
-------------------
* Фактическое содержимое ответа `/gifts/saling` — нужен валидный токен.
  Маппинг полей написан по моделям открытых клиентов; неизвестные поля
  игнорируются, отсутствующие — не ломают нормализацию.
* Публичный deep-link на конкретный лот не найден: фронтенд шарит события
  через `inGameLink` (приходит из авторизованного `/me`), поэтому шаблон
  ссылки задаётся в `MRKT_LINK_TEMPLATE` и по умолчанию ведёт на приложение.
* Единица измерения `salesFee` из конфигов MRKT неизвестна → комиссия
  выводится из пары salePrice/salePriceWithoutFee конкретного лота.

ТРЕБОВАНИЯ К ДОСТУПУ
--------------------
Токен выдаётся только реальному Telegram-аккаунту, открывшему мини-приложение
@mrkt. Два способа: (1) вписать готовый `MRKT_TOKEN` вручную; (2) включить
авто-обновление через пользовательскую сессию (см. mrkt_auth.py, нужен
telethon). Площадка ограничивает частоту запросов (HTTP 429) — лимиты
вежливости настраиваются в .env.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import aiohttp

from app.config import Settings
from app.domain import (
    FetchRequest,
    FetchResult,
    Listing,
    SourceKind,
    SourceStatus,
    parse_iso_datetime,
    utcnow,
)
from app.sources.base import (
    ImageResult,
    ListingSource,
    SourceAuthError,
    SourceError,
    SourceNotFoundError,
    SourceRateLimitError,
    SourceUnavailableError,
)

logger = logging.getLogger(__name__)

TokenProvider = Callable[[], Awaitable[str | None]]

# Порядок проверки схем заголовка авторизации.
# amrkt и неофициальная документация шлют токен «как есть», mrkts — с Bearer.
AUTH_SCHEMES = ("raw", "bearer")


class MrktSource(ListingSource):
    """Клиент приватного API MRKT."""

    name = "mrkt"
    is_test_data = False

    def __init__(
        self,
        settings: Settings,
        *,
        session: aiohttp.ClientSession | None = None,
        token_provider: TokenProvider | None = None,
        image_fetcher: Callable[[str], Awaitable[ImageResult | None]] | None = None,
    ) -> None:
        self._settings = settings
        self._own_session = session is None
        self._session = session
        self._token: str = settings.mrkt_token.strip()
        self._token_provider = token_provider
        self._image_fetcher = image_fetcher

        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._auth_scheme = "raw"
        self._requests_total = 0
        self._requests_failed = 0
        self._last_error: str | None = None
        self._last_success_at: datetime | None = None
        self._rate_limited_until: datetime | None = None
        self._collections_cache: list[dict[str, Any]] | None = None
        self._collections_cached_at: float = 0.0
        self._status_message = ""

    # ------------------------------------------------------------------ сессия
    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._settings.mrkt_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._own_session = True
        return self._session

    def _headers(self, scheme: str | None = None) -> dict[str, str]:
        scheme = scheme or self._auth_scheme
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://cdn.tgmrkt.io",
            "Referer": self._settings.mrkt_referer,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if self._token:
            value = f"Bearer {self._token}" if scheme == "bearer" else self._token
            headers["Authorization"] = value
            headers["Cookie"] = f"access_token={self._token}"
        return headers

    # -------------------------------------------------------------- rate limit
    async def _throttle(self) -> None:
        delay = max(0.0, float(self._settings.mrkt_request_delay))
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------ запрос
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Один HTTP-запрос с троттлингом, повторами и понятными ошибками."""
        import json as _json

        session = self._ensure_session()
        url = f"{self._settings.mrkt_base_url.rstrip('/')}/{path.lstrip('/')}"
        retries = max(1, int(self._settings.mrkt_max_retries))
        last_error: SourceError | None = None
        scheme_flipped = False
        refresh_attempted = False
        just_rate_limited = False
        transient = 0          # повторы на 429/5xx/сеть
        auth_recovery = 0      # попытки восстановления авторизации (401/403/404)

        # Бюджет попыток разделён намеренно: иначе `mrkt_max_retries` незаметно
        # превращался в max_retries + 3, и ожидание после 429 умножалось.
        while True:
            can_retry_transient = transient < retries
            can_recover_auth = auth_recovery < (len(AUTH_SCHEMES) + 1)
            if not can_retry_transient and not can_recover_auth:
                break

            async with self._lock:
                # После 429 пауза Retry-After уже выжидана в обработчике ниже —
                # повторно спать перед запросом не нужно.
                if (
                    not just_rate_limited
                    and self._rate_limited_until
                    and utcnow() < self._rate_limited_until
                ):
                    wait = (self._rate_limited_until - utcnow()).total_seconds()
                    logger.info("MRKT: ждём окончания ограничения %.1f c", wait)
                    await asyncio.sleep(min(wait, 60.0))
                just_rate_limited = False
                await self._throttle()
                self._requests_total += 1
                try:
                    async with session.request(
                        method,
                        url,
                        json=json,
                        params=params,
                        headers=self._headers(),
                    ) as response:
                        status = response.status
                        body = await response.text()
                        headers = dict(response.headers)
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    self._requests_failed += 1
                    transient += 1
                    last_error = SourceUnavailableError(f"сетевая ошибка: {exc!r}")
                    logger.warning("MRKT %s %s: %r (повтор %d/%d)", method, path, exc, transient, retries)
                    if transient >= retries:
                        break
                    await asyncio.sleep(self._backoff(transient))
                    continue

            self._last_request_at = time.monotonic()

            if 200 <= status < 300:
                self._last_success_at = utcnow()
                self._last_error = None
                if not body:
                    return None
                try:
                    return _json.loads(body)
                except ValueError:
                    return body

            # --- разбор ошибок -------------------------------------------------
            if status in (401, 403, 404):
                # Без валидного токена MRKT скрывает закрытые маршруты за 404,
                # а невалидный токен даёт 401/403. Обрабатываем одинаково.
                auth_recovery += 1
                if not self._token:
                    self._requests_failed += 1
                    last_error = SourceAuthError(
                        f"HTTP {status} и токен не задан: маршруты MRKT закрыты "
                        "авторизацией. Укажите MRKT_TOKEN (README, раздел 5)."
                    )
                    break
                if not scheme_flipped:
                    scheme_flipped = True
                    other = [s for s in AUTH_SCHEMES if s != self._auth_scheme]
                    if other:
                        self._auth_scheme = other[0]
                        logger.info(
                            "MRKT: HTTP %s на %s — пробую схему авторизации %r",
                            status, path, self._auth_scheme,
                        )
                        continue
                if not refresh_attempted and self._token_provider is not None:
                    refresh_attempted = True
                    if await self._safe_refresh_token():
                        continue
                self._requests_failed += 1
                last_error = SourceAuthError(
                    f"HTTP {status} на {path}: токен MRKT не принят, истёк "
                    f"или маршрут изменён площадкой. Ответ: {body[:160] or 'пусто'}"
                )
                break

            if status == 429:
                retry_after = _retry_after(headers)
                self._rate_limited_until = utcnow() + timedelta(seconds=retry_after)
                self._requests_failed += 1
                transient += 1
                last_error = SourceRateLimitError(
                    f"HTTP 429: MRKT ограничил частоту запросов (пауза {retry_after:.0f} c)",
                    retry_after=retry_after,
                )
                logger.warning("MRKT rate limit на %s, retry_after=%.0f", path, retry_after)
                if transient >= retries:
                    break
                just_rate_limited = True
                await asyncio.sleep(min(retry_after, 60))
                continue

            if status >= 500:
                self._requests_failed += 1
                transient += 1
                last_error = SourceUnavailableError(f"HTTP {status} на стороне MRKT ({path})")
                if transient >= retries:
                    break
                await asyncio.sleep(self._backoff(transient))
                continue

            self._requests_failed += 1
            last_error = SourceError(f"HTTP {status} на {path}: {body[:200]}")
            break

        if last_error is None:  # pragma: no cover - защита от логики повторов
            last_error = SourceUnavailableError(f"запрос {path} не выполнен")
        self._last_error = str(last_error)
        raise last_error

    def _backoff(self, attempt: int) -> float:
        base = max(0.0, float(self._settings.mrkt_retry_backoff_base))
        cap = max(base, float(self._settings.mrkt_retry_backoff_max))
        if base == 0:
            return 0.0
        return min(base ** attempt, cap)

    async def _safe_refresh_token(self) -> bool:
        """Попросить у провайдера свежий токен. True — токен обновлён."""
        if self._token_provider is None:
            return False
        try:
            fresh = await self._token_provider()
        except Exception as exc:  # noqa: BLE001 - провайдер может быть любым
            logger.warning("MRKT: не удалось обновить токен: %r", exc)
            return False
        if fresh and fresh.strip() and fresh.strip() != self._token:
            self._token = fresh.strip()
            self._auth_scheme = "raw"
            logger.info("MRKT: токен обновлён через провайдера")
            return True
        return False

    # ------------------------------------------------------------------ статус
    async def status(self) -> SourceStatus:
        """Проверить источник реальным запросом.

        Используется минимальный `POST /gifts/saling` с count=1: так же, как это
        делают открытые клиенты для проверки живости токена.
        """
        if not self._token:
            message = (
                "Токен MRKT не задан. Реальный источник выключен: маршруты "
                "api.tgmrkt.io закрыты авторизацией (без токена — HTTP 404)."
            )
            self._status_message = message
            return SourceStatus(
                name=self.name,
                kind=SourceKind.MRKT,
                available=False,
                is_test_data=False,
                checked_at=utcnow(),
                message=message,
                detail="Заполните MRKT_TOKEN или включите авто-вход (TG_API_ID/TG_API_HASH).",
                requires_token=True,
                last_error=self._last_error,
                last_success_at=self._last_success_at,
                requests_total=self._requests_total,
                requests_failed=self._requests_failed,
            )

        try:
            data = await self._request(
                "POST",
                "gifts/saling",
                json={
                    "collectionNames": [],
                    "modelNames": [],
                    "backdropNames": [],
                    "symbolNames": [],
                    "ordering": "Price",
                    "lowToHigh": True,
                    "count": 1,
                    "cursor": "",
                },
            )
        except SourceAuthError as exc:
            self._status_message = str(exc)
            return SourceStatus(
                name=self.name,
                kind=SourceKind.MRKT,
                available=False,
                is_test_data=False,
                checked_at=utcnow(),
                message="Авторизация не прошла",
                detail=str(exc),
                requires_token=True,
                last_error=str(exc),
                requests_total=self._requests_total,
                requests_failed=self._requests_failed,
                rate_limited_until=self._rate_limited_until,
            )
        except SourceError as exc:
            self._status_message = str(exc)
            return SourceStatus(
                name=self.name,
                kind=SourceKind.MRKT,
                available=False,
                is_test_data=False,
                checked_at=utcnow(),
                message="Источник недоступен",
                detail=str(exc),
                requires_token=True,
                last_error=str(exc),
                requests_total=self._requests_total,
                requests_failed=self._requests_failed,
                rate_limited_until=self._rate_limited_until,
            )

        gifts = _extract_gifts(data)
        total = _extract_total(data)
        message = f"MRKT отвечает: получено {len(gifts)} лот(ов) в пробном запросе."
        if total:
            message += f" Всего в выдаче: {total}."
        self._status_message = message
        return SourceStatus(
            name=self.name,
            kind=SourceKind.MRKT,
            available=True,
            is_test_data=False,
            checked_at=utcnow(),
            message=message,
            detail=f"Схема авторизации: {self._auth_scheme}",
            requires_token=True,
            last_success_at=self._last_success_at,
            requests_total=self._requests_total,
            requests_failed=self._requests_failed,
            rate_limited_until=self._rate_limited_until,
        )

    # ------------------------------------------------------------- справочники
    async def collections(self) -> list[str]:
        rows = await self._collections_raw()
        return [str(r.get("name") or r.get("title") or "") for r in rows if r.get("name") or r.get("title")]

    async def collections_meta(self) -> list[dict[str, Any]]:
        return await self._collections_raw()

    async def _collections_raw(self) -> list[dict[str, Any]]:
        ttl = float(self._settings.changes_cache_ttl or 3600)
        if self._collections_cache is not None and (time.monotonic() - self._collections_cached_at) < ttl:
            return self._collections_cache
        data = await self._request("GET", "gifts/collections")
        rows: list[dict[str, Any]] = []
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            inner = data.get("collections") or data.get("gifts") or []
            rows = [r for r in inner if isinstance(r, dict)]
        self._collections_cache = rows
        self._collections_cached_at = time.monotonic()
        return rows

    async def models(self, collection: str) -> list[str]:
        return await self._attribute_names("gifts/models", collection)

    async def backdrops(self, collection: str) -> list[str]:
        return await self._attribute_names("gifts/backdrops", collection)

    async def _attribute_names(self, path: str, collection: str) -> list[str]:
        if not collection:
            return []
        try:
            data = await self._request("POST", path, json={"collections": [collection]})
        except SourceError as exc:
            logger.info("MRKT %s недоступен: %s", path, exc)
            return []
        rows: list[Any] = data if isinstance(data, list) else (data or {}).get("items", [])
        names: list[str] = []
        for row in rows:
            if isinstance(row, str):
                names.append(row)
            elif isinstance(row, dict):
                value = row.get("name") or row.get("title")
                if value:
                    names.append(str(value))
        return names

    # ------------------------------------------------------------------ выдача
    def _saling_payload(
        self, request: FetchRequest, *, cursor: str, count: int, collections: list[str]
    ) -> dict[str, Any]:
        """Тело запроса /gifts/saling.

        Проверено на живом API (27.10.2026): все четыре массива
        (collectionNames, modelNames, backdropNames, symbolNames) ОБЯЗАТЕЛЬНЫ —
        без любого из них сервер отвечает HTTP 400 с валидацией полей.
        """
        payload: dict[str, Any] = {
            "collectionNames": list(collections),
            "modelNames": list(request.models or []),
            "backdropNames": list(request.backdrops or []),
            "symbolNames": list(request.symbols or []),
            "ordering": request.ordering or "Price",
            "lowToHigh": bool(request.low_to_high),
            "count": max(1, min(int(count), 20)),
            "cursor": cursor or "",
        }
        if request.min_price_nano is not None:
            payload["minPrice"] = int(request.min_price_nano)
        if request.max_price_nano is not None:
            payload["maxPrice"] = int(request.max_price_nano)
        if request.query:
            payload["query"] = request.query
        return payload

    async def _resolve_collections(self, request: FetchRequest) -> list[str]:
        """Коллекции для запроса.

        Пустой `collectionNames` даёт `total: 0` — то есть «весь рынок» одним
        запросом MRKT не отдаёт. Поэтому при пустом фильтре берём список
        коллекций из `GET /gifts/collections` (он работает) и ограничиваем его
        `MRKT_MAX_COLLECTIONS`, чтобы не упереться в 429.
        """
        wanted = [name for name in (request.collections or []) if name and name.strip()]
        if wanted:
            return wanted
        try:
            names = await self.collections()
        except SourceError as exc:
            logger.warning("Не удалось получить список коллекций MRKT: %s", exc)
            return []
        limit = max(1, int(self._settings.mrkt_max_collections))
        if len(names) > limit:
            logger.info(
                "Запрос «весь рынок»: беру первые %d коллекций из %d "
                "(MRKT_MAX_COLLECTIONS). Задайте коллекцию в фильтре, "
                "чтобы искать адресно и быстрее.",
                limit, len(names),
            )
        return names[:limit]

    async def fetch(self, request: FetchRequest) -> FetchResult:
        page_size = max(1, min(int(self._settings.mrkt_page_size), 20))
        max_pages = max(1, int(self._settings.mrkt_max_pages))
        need = max(1, request.limit)

        collections = await self._resolve_collections(request)

        collected: list[Listing] = []
        errors: list[str] = []
        cursor = ""
        truncated = False
        exhausted = False

        for page in range(max_pages):
            payload = self._saling_payload(
                request, cursor=cursor, count=page_size, collections=collections
            )

            try:
                data = await self._request("POST", "gifts/saling", json=payload)
            except (SourceAuthError, SourceRateLimitError):
                # Фатальные ошибки пробрасываем наверх: иначе каждый цикл
                # мониторинга молча возвращал бы пустой результат, а причина
                # (истёкший токен, ограничение частоты) терялась.
                raise
            except SourceError as exc:
                # Сетевой сбой или 5xx на одной из страниц: отдаём то, что
                # уже получили, и помечаем выдачу неполной.
                errors.append(f"страница {page + 1}: {exc}")
                exhausted = False
                break

            rows = _extract_gifts(data)
            for row in rows:
                listing = normalize_gift(row, settings=self._settings)
                if listing is not None:
                    collected.append(listing)

            cursor = _extract_cursor(data)
            if not cursor:
                # Курсор отсутствует/None — страниц больше нет.
                # (Сам по себе короткий ответ не признак конца: MRKT может
                # вернуть меньше `count` лотов и всё равно дать курсор.)
                exhausted = True
                break
            if len(collected) >= need:
                truncated = True
                break
        else:
            # Вышли за max_pages: выдача прочитана не целиком.
            truncated = truncated or bool(cursor)

        if len(collected) > need:
            truncated = True
            collected = collected[:need]

        return FetchResult(
            listings=collected,
            truncated=truncated,
            is_test_data=False,
            errors=errors,
            complete=exhausted and not truncated and not errors,
        )

    async def fetch_page(self, request: FetchRequest, cursor: str = "") -> Any:
        """Одна страница выдачи — для диагностики и тестов (ошибки пробрасываются)."""
        payload = self._saling_payload(
            request,
            cursor=cursor,
            count=self._settings.mrkt_page_size,
            collections=list(request.collections or []),
        )
        return await self._request("POST", "gifts/saling", json=payload)

    # --------------------------------------------------------------- картинка
    async def get_image(self, listing: Listing) -> ImageResult | None:
        """Скачать картинку лота.

        Приоритет: `modelStickerThumbnailKey`/`modelStickerKey` из ответа MRKT
        (фронтенд использует это значение напрямую как `src`), затем —
        fallback через публичный API @GiftChanges.
        """
        url = listing.image_url
        if url and url.startswith("http"):
            result = await self._download(url)
            if result and result.ok:
                return result
            logger.info("MRKT: картинка %s недоступна (%s)", url, result.error if result else "нет ответа")
        if self._image_fetcher is not None:
            return await self._image_fetcher(listing.image_url or "")
        return None

    async def _download(self, url: str) -> ImageResult | None:
        session = self._ensure_session()
        try:
            async with self._lock:
                await self._throttle()
                self._requests_total += 1
                async with session.get(url, headers={"Referer": self._settings.mrkt_referer}) as response:
                    if response.status != 200:
                        self._requests_failed += 1
                        return ImageResult(data=b"", url=url, origin="mrkt_cdn",
                                           error=f"HTTP {response.status}")
                    data = await response.read()
                    content_type = response.headers.get("Content-Type", "image/jpeg")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._requests_failed += 1
            return ImageResult(data=b"", url=url, origin="mrkt_cdn", error=repr(exc))
        limit = int(self._settings.image_max_bytes)
        if limit and len(data) > limit:
            return ImageResult(data=b"", url=url, origin="mrkt_cdn",
                               error=f"файл больше лимита ({len(data)} > {limit})")
        return ImageResult(data=data, content_type=content_type.split(";")[0].strip(),
                           url=url, origin="mrkt_cdn")

    # ---------------------------------------------------------------- ресурс
    async def close(self) -> None:
        if self._own_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def token(self) -> str:
        return self._token

    def set_token(self, token: str) -> None:
        self._token = (token or "").strip()
        self._auth_scheme = "raw"


# ----------------------------------------------------------------------------
#  Нормализация ответа MRKT в доменный Listing
# ----------------------------------------------------------------------------

def _extract_gifts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("gifts", "items", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _extract_total(data: Any) -> int | None:
    if isinstance(data, dict):
        for key in ("total", "totalCount", "count"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    return None


def _extract_cursor(data: Any) -> str:
    if isinstance(data, dict):
        value = data.get("cursor") or data.get("nextCursor")
        if isinstance(value, str):
            return value
    return ""


def _first_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _absolute_media_url(value: str | None, settings: Settings) -> str | None:
    """Ключи картинок MRKT относительные — сделать из них URL.

    Живой API (27.10.2026) отдаёт `modelStickerThumbnailKey` вида
    `gifts/stickers/thumbnails/<hex>.webp`, а не абсолютный URL. Файл доступен
    по `https://cdn.tgmrkt.io/<ключ>` (проверено: HTTP 200, WEBP 128×128 RGBA —
    конвертацию в JPEG для Telegram делает app.sources.factory).
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://", "//")):
        return raw
    base = (settings.mrkt_cdn_url or "https://cdn.tgmrkt.io/").rstrip("/")
    return f"{base}/{raw.lstrip('/')}"


def _first_str(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def normalize_gift(row: dict[str, Any], *, settings: Settings) -> Listing | None:
    """Превратить сырой объект подарка MRKT в доменный Listing.

    Терпим к отсутствующим и переименованным полям: обязательны только
    идентификатор и цена.
    """
    listing_id = _first_str(row, "id", "giftSaleId", "saleId", "uniqueId")
    if not listing_id:
        gift_id = _first_int(row, "giftId")
        number = _first_int(row, "number")
        if gift_id is None and number is None:
            return None
        listing_id = f"{gift_id or 'gift'}:{number or '0'}"

    price = _first_int(row, "salePrice", "price", "amount", "salePriceNanoTONs")
    if price is None:
        return None

    collection = _first_str(row, "collectionName", "collectionTitle", "collection") or "Unknown"
    image_url = _absolute_media_url(
        _first_str(
            row,
            "modelStickerThumbnailKey",
            "modelStickerKey",
            "thumbnailImageKey",
            "originalImageKey",
            "imageUrl",
            "image",
        ),
        settings,
    )

    listed_at = parse_iso_datetime(
        row.get("listedAt")
        or row.get("createdAt")
        or row.get("saleCreatedAt")
        or row.get("receivedDate")
        or row.get("promoteEndAt")
    )

    is_on_sale = row.get("isOnSale")
    listing = Listing(
        id=str(listing_id),
        source="mrkt",
        collection_name=collection,
        title=_first_str(row, "title", "name", "collectionTitle") or collection,
        number=_first_int(row, "number", "giftNumber"),
        model_name=_first_str(row, "modelName", "modelTitle"),
        backdrop_name=_first_str(row, "backdropName", "backdropTitle"),
        symbol_name=_first_str(row, "symbolName", "symbolTitle", "patternName"),
        model_rarity_permille=_first_int(row, "modelRarityPerMille", "modelRarityPermille"),
        backdrop_rarity_permille=_first_int(row, "backdropRarityPerMille", "backdropRarityPermille"),
        symbol_rarity_permille=_first_int(row, "symbolRarityPerMille", "symbolRarityPermille"),
        price_nano=int(price),
        price_without_fee_nano=_first_int(row, "salePriceWithoutFee", "priceWithoutFee"),
        currency="TON",
        floor_by_collection_nano=_first_int(
            row, "floorPriceNanoTONsByCollection", "floorPriceNanoTonsByCollection", "floorPriceByCollection"
        ),
        floor_by_attributes_nano=_first_int(
            row,
            "floorPriceNanoTONsByBackdropModel",
            "floorPriceNanoTonsByBackdropModel",
            "floorPriceByBackdropModel",
        ),
        backdrop_center_color=_first_int(row, "backdropColorsCenterColor", "backdropCenterColor"),
        backdrop_edge_color=_first_int(row, "backdropColorsEdgeColor", "backdropEdgeColor"),
        backdrop_symbol_color=_first_int(row, "backdropColorsSymbolColor", "backdropSymbolColor"),
        backdrop_text_color=_first_int(row, "backdropColorsTextColor", "backdropTextColor"),
        image_url=image_url,
        url=_first_str(row, "url", "link", "shareUrl"),
        listed_at=listed_at,
        is_active=bool(is_on_sale) if is_on_sale is not None else True,
        is_test_data=False,
        extra={
            "gift_id": _first_str(row, "giftIdString") or _first_int(row, "giftId"),
            "gift_type": _first_str(row, "giftType"),
            "is_premarket": row.get("isPremarket"),
            "minted": row.get("minted"),
            "sales_count": _first_int(row, "salesCount"),
        },
    )
    return listing


def _retry_after(headers: Any) -> float:
    if headers is None:
        return 10.0
    try:
        raw = headers.get("Retry-After")
        if raw:
            return max(1.0, float(raw))
    except (AttributeError, TypeError, ValueError):
        pass
    return 10.0


def build_link(listing: Listing, template: str) -> str:
    """Собрать ссылку для кнопки «Открыть предложение».

    ВАЖНО: формат публичного deep-link на конкретный лот MRKT не подтверждён.
    Шаблон задаётся в `MRKT_LINK_TEMPLATE`; если плейсхолдеров нет — ссылка
    ведёт на приложение MRKT целиком, и бот честно пишет об этом в карточке.
    """
    if listing.url:
        return listing.url
    template = (template or "https://t.me/mrkt/app").strip()
    if not template:
        return "https://t.me/mrkt/app"
    try:
        return template.format(
            id=listing.id,
            collection=listing.collection_name or "",
            model=listing.model_name or "",
            backdrop=listing.backdrop_name or "",
            symbol=listing.symbol_name or "",
            number=listing.number or "",
        )
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Некорректный MRKT_LINK_TEMPLATE (%s): %r", template, exc)
        return "https://t.me/mrkt/app"
