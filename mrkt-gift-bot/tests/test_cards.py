"""Тесты карточки уведомления и подготовки изображения.

Карточка — единственное, что пользователь видит о лоте, поэтому проверяем
обязательный состав: цена, атрибуты, оценка монохрома со способом и
уверенностью, ориентир цены с числом аналогов и процентом, релевантность,
ссылка, дисклеймер и честная маркировка тестовых данных.
"""

from __future__ import annotations

from typing import Any

import io

import pytest
from PIL import Image

from app.bot.cards import (
    build_caption,
    build_card_text,
    explain_score,
    image_to_jpeg_bytes,
    prepare_card,
    prepare_photo,
)
from app.config import Settings
from app.domain import (
    Confidence,
    FetchRequest,
    FetchResult,
    Listing,
    MatchResult,
    MonochromeAssessment,
    RankingResult,
    ScoredListing,
    SourceKind,
    SourceStatus,
    ValueAssessment,
)
from app.sources.base import ImageResult, ListingSource
from app.sources.factory import ImageProvider, convert_for_telegram
from tests.conftest import make_listing, make_png

TON = 1_000_000_000


@pytest.fixture()
def card_settings() -> Settings:
    return Settings(_env_file=None, bot_token="x", source="demo", image_enabled=True)


def make_scored(
    *,
    listing: Listing | None = None,
    mono_score: float = 88.0,
    mono_method: str = "backdrop_colors",
    mono_confidence: Confidence = Confidence.HIGH,
    benchmark_nano: int | None = 60 * TON,
    diff_percent: float | None = 16.7,
    comparables: int = 9,
    benchmark_source: str = "peers_exact",
    value_confidence: Confidence = Confidence.HIGH,
    relevance: float = 84.5,
    **listing_kwargs: Any,
) -> ScoredListing:
    listing_kwargs.setdefault("is_test_data", False)
    item = listing or make_listing(price_nano=50 * TON, **listing_kwargs)
    return ScoredListing(
        listing=item,
        match=MatchResult(passed=True, match_score=1.0, matched_criteria=["коллекция"], total_criteria=1),
        monochrome=MonochromeAssessment(
            score=mono_score, method=mono_method, confidence=mono_confidence
        ),
        value=ValueAssessment(
            price_nano=item.price_nano,
            benchmark_nano=benchmark_nano,
            diff_nano=(benchmark_nano - item.price_nano) if benchmark_nano else None,
            diff_percent=diff_percent,
            comparables=comparables,
            benchmark_source=benchmark_source,
            confidence=value_confidence,
            min_comparables=3,
            fee_permille=50,
        ),
        ranking=RankingResult(
            score=relevance,
            components={"match": 1.0, "monochrome": 0.88, "value": 0.8, "confidence": 1.0, "freshness": 0.6},
            weights={"match": 0.25, "monochrome": 0.25, "value": 0.25, "confidence": 0.15, "freshness": 0.1},
            used_weights={"match": 0.25, "monochrome": 0.25, "value": 0.25, "confidence": 0.15, "freshness": 0.1},
        ),
    )


# ----------------------------------------------------------------------------
#  Обязательный состав карточки
# ----------------------------------------------------------------------------

def test_card_contains_all_mandatory_blocks(card_settings: Settings) -> None:
    text = build_card_text(make_scored(), settings=card_settings)
    assert "Frost Fox" in text                     # коллекция
    assert "50" in text and "TON" in text          # цена
    assert "Noir" in text                          # модель
    assert "Ash Grey" in text                      # фон
    assert "монохром" in text.lower()              # оценка монохрома
    assert "цвета фона из API MRKT" in text        # способ определения
    assert "высокая" in text.lower() or "high" in text.lower()   # уверенность
    assert "60" in text                            # ориентир цены
    assert "16.7" in text or "16,7" in text        # разница в %
    assert "9" in text                             # число аналогов
    assert "84.5" in text or "84,5" in text        # релевантность
    assert "не гарантия прибыли" in text           # дисклеймер


def test_card_marks_test_data_explicitly(card_settings: Settings) -> None:
    text = build_card_text(make_scored(is_test_data=True), settings=card_settings)
    assert "ТЕСТОВЫЕ ДАННЫЕ" in text

    real_text = build_card_text(make_scored(), settings=card_settings)
    assert "ТЕСТОВЫЕ ДАННЫЕ" not in real_text


def test_card_marks_test_data_from_meta_too(card_settings: Settings) -> None:
    text = build_card_text(make_scored(), settings=card_settings, meta={"is_test_data": True})
    assert "ТЕСТОВЫЕ ДАННЫЕ" in text


def test_card_escapes_html_in_listing_data(card_settings: Settings) -> None:
    scored = make_scored(
        collection_name="<script>alert(1)</script>",
        model_name="A & B",
        title='Noir "special"',
    )
    text = build_card_text(scored, settings=card_settings)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "A &amp; B" in text


def test_card_explains_reason_for_repeat_notification(card_settings: Settings) -> None:
    text = build_card_text(make_scored(), settings=card_settings, meta={"reason": "price_drop"})
    assert "снижен" in text.lower() or "цен" in text.lower()
    assert "🎁" in text


def test_card_shows_filter_name(card_settings: Settings) -> None:
    text = build_card_text(make_scored(), settings=card_settings, meta={"filter_name": "Серые лисы"})
    assert "Серые лисы" in text


def test_card_reports_missing_benchmark_instead_of_inventing_it(card_settings: Settings) -> None:
    scored = make_scored(
        benchmark_nano=None, diff_percent=None, comparables=0,
        benchmark_source="none", value_confidence=Confidence.NONE,
    )
    text = build_card_text(scored, settings=card_settings)
    assert "не рассчитана" in text or "нет ориентира" in text or "ориентир не определён" in text


def test_card_warns_when_comparables_are_scarce(card_settings: Settings) -> None:
    scored = make_scored(comparables=1, value_confidence=Confidence.LOW)
    text = build_card_text(scored, settings=card_settings)
    assert "ненадёж" in text.lower() or "мало аналогов" in text.lower()


def test_card_states_monochrome_method_when_only_name_heuristic(card_settings: Settings) -> None:
    scored = make_scored(mono_score=55.0, mono_method="name", mono_confidence=Confidence.LOW)
    text = build_card_text(scored, settings=card_settings)
    assert "эвристика по названию" in text
    assert "низкая" in text.lower()


def test_card_says_when_no_monochrome_data(card_settings: Settings) -> None:
    scored = make_scored(mono_score=0.0, mono_method="none", mono_confidence=Confidence.NONE)
    text = build_card_text(scored, settings=card_settings)
    assert "данных о цвете нет" in text


def test_card_mentions_unverified_link_when_template_has_no_placeholders(
    card_settings: Settings,
) -> None:
    scored = make_scored(url=None)
    text = build_card_text(scored, settings=card_settings)
    assert "не подтверждена" in text
    assert "lot-1" in text          # ID лота показываем, чтобы его можно было найти


def test_card_uses_listing_url_when_source_provides_it() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="mrkt")
    scored = make_scored(url="https://t.me/mrkt/app?startapp=gift42")
    text = build_card_text(scored, settings=settings)
    assert "gift42" in text
    assert "не подтверждена" not in text


def test_card_is_within_telegram_message_limit(card_settings: Settings) -> None:
    scored = make_scored(
        title="Очень длинное название " * 40,
        collection_name="Коллекция " * 40,
        extra={"note": "x" * 500},
    )
    text = build_card_text(scored, settings=card_settings)
    assert len(text) <= 4096


def test_compact_card_omits_details_but_keeps_price(card_settings: Settings) -> None:
    scored = make_scored()
    compact = build_card_text(scored, settings=card_settings, compact=True)
    full = build_card_text(scored, settings=card_settings)
    assert len(compact) < len(full)
    assert "50" in compact


# ----------------------------------------------------------------------------
#  Подпись под фото
# ----------------------------------------------------------------------------

def test_caption_fits_telegram_limit(card_settings: Settings) -> None:
    scored = make_scored(title="Длинное название " * 60, collection_name="Коллекция " * 60)
    caption = build_caption(scored, settings=card_settings)
    assert len(caption) <= 1024
    # Важные строки сохраняются даже при очень длинном названии.
    assert "🎁" in caption and "💰" in caption
    assert "монохром" in caption and "ориентир" in caption and "релевантность" in caption


def test_caption_shortens_title_not_the_essentials(card_settings: Settings) -> None:
    scored = make_scored(title="A" * 900)
    caption = build_caption(scored, settings=card_settings)
    assert len(caption) <= 1024
    assert caption.count("\n") >= 4          # цена, монохром, выгода, релевантность
    assert "…" in caption


def test_caption_contains_score_and_benchmark(card_settings: Settings) -> None:
    caption = build_caption(make_scored(), settings=card_settings)
    assert "84.5" in caption or "84,5" in caption
    assert "аналогов 9" in caption
    assert "монохром 88/100" in caption


def test_caption_marks_test_data(card_settings: Settings) -> None:
    caption = build_caption(make_scored(is_test_data=True), settings=card_settings)
    assert "ТЕСТОВЫЕ ДАННЫЕ" in caption


def test_prepare_card_uses_caption_with_photo_and_full_text_without(
    card_settings: Settings,
) -> None:
    scored = make_scored()
    with_photo = prepare_card(scored, settings=card_settings, photo=b"jpeg-bytes")
    assert with_photo.photo == b"jpeg-bytes"
    assert len(with_photo.text) <= 1024
    assert with_photo.keyboard is not None

    without_photo = prepare_card(scored, settings=card_settings)
    assert without_photo.photo is None
    assert "не гарантия прибыли" in without_photo.text


def test_prepare_card_keyboard_has_open_listing_button(card_settings: Settings) -> None:
    card = prepare_card(make_scored(url="https://t.me/mrkt/app?startapp=gift42"), settings=card_settings)
    buttons = [b.text for row in card.keyboard.inline_keyboard for b in row]
    urls = [b.url for row in card.keyboard.inline_keyboard for b in row if getattr(b, "url", None)]
    assert any("открыть" in (text or "").lower() for text in buttons)
    assert urls and "gift42" in urls[0]


def test_explain_score_describes_formula() -> None:
    text = explain_score(make_scored())
    lowered = text.lower()
    for component in ("соответствие фильтрам", "монохромность", "выгода", "надёжность данных", "свежесть"):
        assert component in lowered, component
    assert "вес" in lowered                       # формула объясняется
    assert "84.5" in text or "84,5" in text       # итоговая оценка


# ----------------------------------------------------------------------------
#  Изображения
# ----------------------------------------------------------------------------

def test_convert_for_telegram_turns_png_into_jpeg() -> None:
    result = ImageResult(data=make_png((10, 10, 10), size=600), content_type="image/png", origin="changes_tg")
    jpeg = convert_for_telegram(result, size=512)
    assert jpeg is not None
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 512


def test_convert_for_telegram_returns_none_on_garbage() -> None:
    assert convert_for_telegram(ImageResult(data=b"not-an-image"), size=256) is None
    assert convert_for_telegram(None) is None
    assert convert_for_telegram(ImageResult(data=b"", error="404")) is None


class StubSource(ListingSource):
    """Источник с настраиваемым ответом get_image."""

    def __init__(self, image: ImageResult | None = None, raise_error: bool = False) -> None:
        self._image = image
        self._raise = raise_error

    async def status(self) -> SourceStatus:
        return SourceStatus(name="stub", kind=SourceKind.DEMO, available=True, is_test_data=True)

    async def fetch(self, request: FetchRequest) -> FetchResult:
        return FetchResult(listings=[])

    async def get_image(self, listing: Listing) -> ImageResult | None:
        if self._raise:
            raise RuntimeError("cdn down")
        return self._image


class StubChanges:
    """@GiftChanges-заглушка: фиксирует обращения и отдаёт PNG."""

    def __init__(self, model_png: bytes | None = None, original_png: bytes | None = None) -> None:
        self.model_calls: list[tuple[str, str]] = []
        self.original_calls: list[str] = []
        self._model = model_png
        self._original = original_png

    async def model_png(self, collection: str, model: str, size: int = 128) -> ImageResult | None:
        self.model_calls.append((collection, model))
        if self._model is None:
            return None
        return ImageResult(data=self._model, content_type="image/png", origin="changes_tg")

    async def original_png(self, collection: str, size: int = 128) -> ImageResult | None:
        self.original_calls.append(collection)
        if self._original is None:
            return None
        return ImageResult(data=self._original, content_type="image/png", origin="changes_tg")


async def test_prepare_photo_returns_jpeg_bytes(card_settings: Settings) -> None:
    provider = ImageProvider(
        StubSource(ImageResult(data=make_png((20, 20, 20)), origin="mrkt_cdn")),
        settings=card_settings,
    )
    data = await prepare_photo(make_scored(), provider, card_settings)
    assert data is not None
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"


async def test_prepare_photo_falls_back_to_changes_tg(card_settings: Settings) -> None:
    changes = StubChanges(model_png=make_png((30, 30, 30)))
    provider = ImageProvider(StubSource(None), changes, settings=card_settings)
    listing = make_listing(collection_name="Frost Fox", model_name="Noir")
    result = await provider.fetch(listing)
    assert result is not None and result.ok
    assert changes.model_calls == [("Frost Fox", "Noir")]


async def test_prepare_photo_falls_back_to_original_png(card_settings: Settings) -> None:
    changes = StubChanges(model_png=None, original_png=make_png((40, 40, 40)))
    provider = ImageProvider(StubSource(None), changes, settings=card_settings)
    result = await provider.fetch(make_listing(collection_name="Frost Fox", model_name="Noir"))
    assert result is not None and result.ok
    assert changes.original_calls == ["Frost Fox"]


async def test_prepare_photo_survives_broken_source(card_settings: Settings) -> None:
    provider = ImageProvider(StubSource(raise_error=True), None, settings=card_settings)
    assert await provider.fetch(make_listing()) is None


async def test_prepare_photo_disabled_by_settings() -> None:
    settings = Settings(_env_file=None, bot_token="x", source="demo", image_enabled=False)
    provider = ImageProvider(StubSource(ImageResult(data=make_png((0, 0, 0)))), settings=settings)
    assert await prepare_photo(make_scored(), provider, settings) is None
    assert await provider.fetch(make_listing()) is None


def test_image_result_ok_requires_data_and_no_error() -> None:
    assert ImageResult(data=b"x").ok is True
    assert ImageResult(data=b"").ok is False
    assert ImageResult(data=b"x", error="timeout").ok is False
