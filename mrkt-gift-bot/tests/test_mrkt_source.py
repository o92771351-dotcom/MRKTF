"""Тесты клиента MRKT: нормализация ответа, разбор ошибок, пагинация, ссылки.

HTTP подменяется фейковой сессией — тесты не ходят в сеть. Форма payload
соответствует полям, которые используются фронтендом MRKT и открытыми
клиентами (salePrice, salePriceWithoutFee, floorPriceNanoTONsBy*,
backdropColors*Color, modelStickerThumbnailKey).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from app.domain import FetchRequest
from app.sources.base import (
    SourceAuthError,
    SourceError,
    SourceRateLimitError,
    SourceUnavailableError,
)
from app.sources.mrkt import MrktSource, build_link, normalize_gift
from tests.conftest import make_listing

TON = 1_000_000_000


# ----------------------------------------------------------------------------
#  Фейковый HTTP
# ----------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int, payload: Any = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    async def read(self) -> bytes:
        return b"image-bytes"

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Мини-замена aiohttp.ClientSession: отдаёт заранее заданные ответы."""

    closed = False

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            return FakeResponse(500, "no more canned responses")
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    async def close(self) -> None:
        self.closed = True


def make_source(responses: list[FakeResponse], *, token: str = "tok", **settings_kwargs: Any) -> tuple[MrktSource, FakeSession]:
    defaults: dict[str, Any] = {
        "bot_token": "x",
        "source": "mrkt",
        "mrkt_token": token,
        "mrkt_request_delay": 0.0,
        "mrkt_max_retries": 2,
        # Тесты не должны ждать реальные паузы повторов.
        "mrkt_retry_backoff_base": 0.0,
        "mrkt_retry_backoff_max": 0.0,
    }
    defaults.update(settings_kwargs)
    settings = Settings(_env_file=None, **defaults)
    session = FakeSession(responses)
    source = MrktSource(settings, session=session)  # type: ignore[arg-type]
    source._own_session = False
    return source, session


GIFT_ROW = {
    "id": "5873429911",
    "giftId": 5936013938331222567,
    "giftIdString": "5936013938331222567",
    "number": 42,
    "title": "Plush Pepe",
    "collectionName": "Plush Pepe",
    "collectionTitle": "Plush Pepe",
    "modelName": "Donatello",
    "modelRarityPerMille": 10,
    "backdropName": "Aquamarine",
    "backdropRarityPerMille": 10,
    "symbolName": "Apple",
    "symbolRarityPerMille": 2,
    "salePrice": 12_500_000_000,
    "salePriceWithoutFee": 11_904_761_905,
    "floorPriceNanoTONsByCollection": 11_000_000_000,
    "floorPriceNanoTONsByBackdropModel": 12_000_000_000,
    "backdropColorsCenterColor": 6336917,
    "backdropColorsEdgeColor": 4631476,
    "backdropColorsTextColor": 13106686,
    "backdropColorsSymbolColor": 221031,
    "modelStickerThumbnailKey": "https://cdn.example/thumb.webp",
    "modelStickerKey": "https://cdn.example/model.tgs",
    "isOnSale": True,
    "giftType": "Upgraded",
    "minted": False,
    "salesCount": 3,
    "listedAt": "2026-10-20T10:15:00Z",
}


# ----------------------------------------------------------------------------
#  Нормализация
# ----------------------------------------------------------------------------

def test_normalize_gift_maps_all_relevant_fields() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift(GIFT_ROW, settings=settings)
    assert listing is not None
    assert listing.id == "5873429911"
    assert listing.source == "mrkt"
    assert listing.collection_name == "Plush Pepe"
    assert listing.model_name == "Donatello"
    assert listing.backdrop_name == "Aquamarine"
    assert listing.symbol_name == "Apple"
    assert listing.number == 42
    assert listing.price_nano == 12_500_000_000
    assert listing.price_without_fee_nano == 11_904_761_905
    assert listing.currency == "TON"
    assert listing.floor_by_collection_nano == 11_000_000_000
    assert listing.floor_by_attributes_nano == 12_000_000_000
    assert listing.backdrop_center_color == 6336917
    assert listing.backdrop_edge_color == 4631476
    assert listing.backdrop_symbol_color == 221031
    assert listing.image_url == "https://cdn.example/thumb.webp"
    assert listing.is_active is True
    assert listing.is_test_data is False
    assert listing.listed_at is not None and listing.listed_at.year == 2026
    assert listing.model_rarity_permille == 10
    assert listing.extra["gift_type"] == "Upgraded"


def test_normalize_gift_survives_missing_optional_fields() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift({"id": "x1", "salePrice": 5 * TON}, settings=settings)
    assert listing is not None
    assert listing.collection_name == "Unknown"
    assert listing.model_name is None
    assert listing.floor_by_attributes_nano is None
    assert listing.image_url is None
    assert listing.listed_at is None
    assert listing.is_active is True      # по умолчанию считаем лот активным


def test_normalize_gift_requires_id_or_price() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    assert normalize_gift({"salePrice": 1}, settings=settings) is None       # нет id
    assert normalize_gift({"id": "a"}, settings=settings) is None            # нет цены
    assert normalize_gift({}, settings=settings) is None


def test_normalize_gift_builds_fallback_id() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift({"giftId": 999, "number": 4, "salePrice": TON}, settings=settings)
    assert listing is not None and listing.id == "999:4"


def test_implied_fee_from_sale_prices() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift(GIFT_ROW, settings=settings)
    assert listing is not None
    fee = listing.implied_fee_permille()
    assert fee is not None and 40 <= fee <= 60       # около 5 %


def test_normalize_accepts_alternative_field_names() -> None:
    """MRKT может менять названия полей — нормализация должна это переживать."""
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    row = {
        "saleId": "s-1",
        "collectionTitle": "Frost Fox",
        "modelTitle": "Noir",
        "price": 7 * TON,
        "imageUrl": "https://cdn.example/a.png",
        "createdAt": "2026-10-21T08:00:00",
    }
    listing = normalize_gift(row, settings=settings)
    assert listing is not None
    assert listing.id == "s-1"
    assert listing.collection_name == "Frost Fox"
    assert listing.model_name == "Noir"
    assert listing.price_nano == 7 * TON
    assert listing.image_url == "https://cdn.example/a.png"
    assert listing.listed_at is not None


# ----------------------------------------------------------------------------
#  Запросы и ошибки
# ----------------------------------------------------------------------------

async def test_fetch_returns_normalized_listings() -> None:
    source, session = make_source([FakeResponse(200, {"gifts": [GIFT_ROW], "total": 1, "cursor": None})])
    result = await source.fetch(FetchRequest(collections=["Plush Pepe"], limit=10))
    assert len(result.listings) == 1
    assert result.listings[0].id == "5873429911"
    assert result.is_test_data is False
    assert result.complete is True
    assert session.calls[0]["url"].endswith("/gifts/saling")


async def test_fetch_paginates_by_cursor() -> None:
    page1 = {"gifts": [GIFT_ROW], "cursor": "next-page"}
    page2 = {"gifts": [{**GIFT_ROW, "id": "second"}], "cursor": None}
    source, session = make_source([FakeResponse(200, page1), FakeResponse(200, page2)])
    result = await source.fetch(FetchRequest(collections=["Plush Pepe"], limit=50))
    assert [item.id for item in result.listings] == ["5873429911", "second"]
    assert len(session.calls) == 2
    assert session.calls[1]["json"]["cursor"] == "next-page"


async def test_fetch_keeps_partial_result_on_network_error() -> None:
    page1 = {"gifts": [GIFT_ROW], "cursor": "next"}
    source, session = make_source([FakeResponse(200, page1), FakeResponse(503, "boom")])
    result = await source.fetch(FetchRequest(collections=["Plush Pepe"], limit=50))
    assert [item.id for item in result.listings] == ["5873429911"]
    assert result.complete is False
    assert result.errors and "страница 2" in result.errors[0]


async def test_fetch_stops_at_max_pages_and_marks_incomplete() -> None:
    page = {"gifts": [GIFT_ROW], "cursor": "always"}
    source, session = make_source([FakeResponse(200, page)], mrkt_max_pages=2, mrkt_page_size=1)
    result = await source.fetch(FetchRequest(collections=["Plush Pepe"], limit=50))
    assert len(session.calls) == 2
    assert result.complete is False


async def test_request_payload_uses_mrkt_field_names() -> None:
    source, session = make_source([FakeResponse(200, {"gifts": []})])
    await source.fetch(
        FetchRequest(
            collections=["Plush Pepe"], models=["Donatello"], backdrops=["Aquamarine"],
            symbols=["Apple"], min_price_nano=TON, max_price_nano=100 * TON,
            query="pepe", limit=5,
        )
    )
    payload = session.calls[0]["json"]
    assert payload["collectionNames"] == ["Plush Pepe"]
    assert payload["modelNames"] == ["Donatello"]
    assert payload["backdropNames"] == ["Aquamarine"]
    assert payload["symbolNames"] == ["Apple"]
    assert payload["minPrice"] == TON
    assert payload["maxPrice"] == 100 * TON
    assert payload["query"] == "pepe"
    assert payload["count"] <= 20          # лимит страницы MRKT
    assert payload["ordering"] == "Price" and payload["lowToHigh"] is True


async def test_404_without_token_is_reported_as_auth_problem() -> None:
    source, _ = make_source([FakeResponse(404, "")], token="", mrkt_max_retries=1)
    with pytest.raises(SourceAuthError) as info:
        await source.fetch_page(FetchRequest(limit=5))
    assert "MRKT_TOKEN" in str(info.value)


async def test_404_with_token_is_reported_as_expired_token() -> None:
    source, _ = make_source([FakeResponse(404, "")], token="dead-token", mrkt_max_retries=1)
    with pytest.raises(SourceAuthError) as info:
        await source.fetch_page(FetchRequest(limit=5))
    assert "истёк" in str(info.value) or "не принят" in str(info.value)


async def test_auth_error_propagates_through_fetch() -> None:
    """Мониторинг должен видеть причину, а не пустой результат."""
    source, _ = make_source([FakeResponse(404, "")], token="dead", mrkt_max_retries=1)
    with pytest.raises(SourceAuthError):
        await source.fetch(FetchRequest(collections=["Plush Pepe"], limit=5))


# ----------------------------------------------------------------------------
#  Обязательные поля запроса и «весь рынок»
# ----------------------------------------------------------------------------

async def test_payload_always_contains_four_required_arrays() -> None:
    """Живой API отвечает 400, если отсутствует любой из четырёх массивов."""
    source, session = make_source([FakeResponse(200, {"gifts": []})])
    await source.fetch(FetchRequest(collections=["Plush Pepe"], limit=5))
    payload = session.calls[0]["json"]
    for key in ("collectionNames", "modelNames", "backdropNames", "symbolNames"):
        assert key in payload and isinstance(payload[key], list), key


async def test_market_wide_request_falls_back_to_collections_list() -> None:
    """Пустой collectionNames даёт total=0 — бот обязан перебрать коллекции."""
    source, session = make_source(
        [
            FakeResponse(200, [{"name": "Plush Pepe"}, {"name": "Frost Fox"}, {"name": "Third"}]),
            FakeResponse(200, {"gifts": [GIFT_ROW], "total": 1}),
        ],
        mrkt_max_collections=2,
    )
    result = await source.fetch(FetchRequest(limit=5))
    assert [item.id for item in result.listings] == ["5873429911"]
    assert session.calls[0]["url"].endswith("/gifts/collections")
    assert session.calls[1]["json"]["collectionNames"] == ["Plush Pepe", "Frost Fox"]


async def test_market_wide_request_survives_collections_failure() -> None:
    source, session = make_source([FakeResponse(503, "boom"), FakeResponse(200, {"gifts": []})])
    result = await source.fetch(FetchRequest(limit=5))
    assert result.listings == []
    assert result.complete is True


async def test_403_from_auth_endpoint_surfaces_as_unavailable_status() -> None:
    """status() не должен падать: он возвращает диагноз (это видно в /status)."""
    source, _ = make_source([FakeResponse(403, "Forbid 1")], token="x")
    status = await source.status()
    assert status.available is False
    assert status.requires_token is True
    assert "403" in (status.detail or "") or "403" in (status.last_error or "")


async def test_scheme_switch_is_tried_once() -> None:
    source, session = make_source([FakeResponse(401, "")], token="x", mrkt_max_retries=1)
    with pytest.raises(SourceAuthError):
        await source.fetch_page(FetchRequest(limit=5))
    # Первая попытка — raw, вторая — bearer, дальше обновление токена недоступно.
    assert len(session.calls) == 2
    assert session.calls[0]["headers"]["Authorization"] == "x"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer x"


async def test_token_provider_is_used_on_auth_failure() -> None:
    calls: list[str] = []

    async def provider() -> str:
        calls.append("refresh")
        return "fresh-token"

    settings = Settings(
        _env_file=None, bot_token="x", source="mrkt", mrkt_token="old",
        mrkt_request_delay=0.0, mrkt_max_retries=1,
    )
    session = FakeSession(
        [
            FakeResponse(401, ""),   # raw
            FakeResponse(401, ""),   # bearer
            FakeResponse(200, {"gifts": [GIFT_ROW]}),   # после обновления токена
        ]
    )
    source = MrktSource(settings, session=session, token_provider=provider)  # type: ignore[arg-type]
    source._own_session = False
    result = await source.fetch(FetchRequest(limit=5))
    assert calls == ["refresh"]
    assert len(result.listings) == 1
    assert source.token == "fresh-token"


async def test_429_is_classified_as_rate_limit() -> None:
    source, _ = make_source(
        [FakeResponse(429, "slow down", {"Retry-After": "1"})], token="x", mrkt_max_retries=1
    )
    with pytest.raises(SourceRateLimitError) as info:
        await source.fetch_page(FetchRequest(limit=5))
    assert info.value.retry_after == 1.0


async def test_rate_limit_propagates_through_fetch() -> None:
    source, _ = make_source(
        [FakeResponse(429, "slow down", {"Retry-After": "1"})], token="x", mrkt_max_retries=1
    )
    with pytest.raises(SourceRateLimitError):
        await source.fetch(FetchRequest(limit=5))


async def test_5xx_is_unavailable() -> None:
    source, _ = make_source([FakeResponse(503, "maintenance")], token="x", mrkt_max_retries=1)
    with pytest.raises(SourceUnavailableError):
        await source.fetch_page(FetchRequest(limit=5))


async def test_unknown_status_is_source_error() -> None:
    source, _ = make_source([FakeResponse(418, "teapot")], token="x", mrkt_max_retries=1)
    with pytest.raises(SourceError):
        await source.fetch_page(FetchRequest(limit=5))


async def test_status_reports_unavailable_without_token() -> None:
    source, session = make_source([FakeResponse(200, {})], token="")
    status = await source.status()
    assert status.available is False
    assert status.requires_token is True
    assert status.is_test_data is False
    assert session.calls == []          # запрос даже не отправляли


async def test_status_reports_rate_limit_window() -> None:
    source, _ = make_source(
        [FakeResponse(429, "slow down", {"Retry-After": "30"})], token="x", mrkt_max_retries=1
    )
    status = await source.status()
    assert status.available is False
    assert status.rate_limited_until is not None


async def test_status_ok_on_successful_probe() -> None:
    source, _ = make_source([FakeResponse(200, {"gifts": [GIFT_ROW], "total": 137})], token="x")
    status = await source.status()
    assert status.available is True
    assert status.is_test_data is False
    assert "137" in status.message


async def test_collections_parsed_from_list_and_dict() -> None:
    source, _ = make_source(
        [FakeResponse(200, [{"name": "Plush Pepe", "title": "Plush Pepe"}, {"name": "Lunar Snake"}])]
    )
    assert await source.collections() == ["Plush Pepe", "Lunar Snake"]

    source2, _ = make_source(
        [FakeResponse(200, {"collections": [{"name": "Frost Fox"}]})], token="x"
    )
    assert await source2.collections() == ["Frost Fox"]


async def test_attribute_endpoints_use_collections_payload() -> None:
    source, session = make_source([FakeResponse(200, [{"name": "Donatello"}, {"name": "Raphael"}])])
    models = await source.models("Plush Pepe")
    assert models == ["Donatello", "Raphael"]
    assert session.calls[0]["url"].endswith("/gifts/models")
    assert session.calls[0]["json"] == {"collections": ["Plush Pepe"]}


# ----------------------------------------------------------------------------
#  Ссылки
# ----------------------------------------------------------------------------

def test_build_link_uses_listing_url_when_present() -> None:
    listing = make_listing(url="https://t.me/mrkt/app?startapp=gift123")
    assert build_link(listing, "https://t.me/mrkt/app") == listing.url


def test_build_link_falls_back_to_app_when_template_has_no_placeholders() -> None:
    listing = make_listing(id="abc", url=None)
    assert build_link(listing, "https://t.me/mrkt/app") == "https://t.me/mrkt/app"


def test_build_link_substitutes_placeholders() -> None:
    listing = make_listing(
        id="abc", url=None, collection_name="Frost Fox", model_name="Noir", number=7
    )
    template = "https://t.me/mrkt/app?startapp=gift_{id}_{number}"
    assert build_link(listing, template) == "https://t.me/mrkt/app?startapp=gift_abc_7"


def test_build_link_survives_broken_template() -> None:
    listing = make_listing(id="abc", url=None)
    assert build_link(listing, "https://t.me/{unknown_field}") == "https://t.me/mrkt/app"


def test_image_url_prefers_thumbnail_key() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift(
        {**GIFT_ROW, "modelStickerThumbnailKey": None, "modelStickerKey": "https://cdn/model.tgs"},
        settings=settings,
    )
    assert listing is not None
    assert listing.image_url == "https://cdn/model.tgs"


def test_relative_image_keys_become_cdn_urls() -> None:
    """Живой MRKT отдаёт ключи вида gifts/stickers/thumbnails/<hex>.webp."""
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift(
        {**GIFT_ROW, "modelStickerThumbnailKey": "gifts/stickers/thumbnails/506c757368.webp"},
        settings=settings,
    )
    assert listing is not None
    assert listing.image_url == "https://cdn.tgmrkt.io/gifts/stickers/thumbnails/506c757368.webp"


def test_absolute_image_urls_are_left_alone() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    listing = normalize_gift(
        {**GIFT_ROW, "modelStickerThumbnailKey": "https://cdn.example/a.webp"}, settings=settings
    )
    assert listing is not None and listing.image_url == "https://cdn.example/a.webp"
