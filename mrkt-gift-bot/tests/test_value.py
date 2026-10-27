"""Тесты оценки выгоды: ориентиры, надёжность, малое число аналогов, комиссии."""

from __future__ import annotations

from datetime import timedelta

from app.domain import Confidence, Listing, utcnow
from app.value import assess_value, describe, is_interesting, select_peers
from tests.conftest import make_listing

TON = 1_000_000_000


def peers(count: int, price_ton: float, **kwargs) -> list[Listing]:
    return [
        make_listing(
            id=f"peer-{index}",
            price_nano=int((price_ton + index * 0.5) * TON),
            **kwargs,
        )
        for index in range(count)
    ]


# ----------------------------------------------------------------------------
#  Выборка аналогов
# ----------------------------------------------------------------------------

def test_exact_peers_require_same_attributes() -> None:
    listing = make_listing(id="target")
    same = peers(3, 50.0)
    other_model = peers(3, 50.0, model_name="Ember")
    other_collection = peers(3, 50.0, collection_name="Iron Whale")

    result = select_peers(listing, same + other_model + other_collection)
    assert len(result.exact) == 3
    assert len(result.collection) == 3          # другая модель той же коллекции
    assert result.total == 6


def test_peers_exclude_the_listing_itself() -> None:
    listing = make_listing(id="target", price_nano=10 * TON)
    twin = make_listing(id="target", price_nano=10 * TON)
    result = select_peers(listing, [twin])
    assert result.total == 0


def test_peers_exclude_inactive_and_stale() -> None:
    listing = make_listing(id="target")
    stale = make_listing(id="stale", fetched_at=utcnow() - timedelta(days=30))
    inactive = make_listing(id="inactive", is_active=False)
    fresh = make_listing(id="fresh")

    result = select_peers(listing, [stale, inactive, fresh], max_peer_age_days=3)
    ids = {item.id for item in result.exact}
    assert ids == {"fresh"}


def test_peers_never_mix_sources_or_test_data() -> None:
    listing = make_listing(id="target", source="demo", is_test_data=True)
    real = make_listing(id="real", source="mrkt", is_test_data=False)
    other_source = make_listing(id="other", source="portals", is_test_data=False)
    same = make_listing(id="same", source="demo", is_test_data=True)

    result = select_peers(listing, [real, other_source, same])
    assert [item.id for item in result.exact] == ["same"]
    assert result.mixed_test_data is True


def test_peers_respect_lookback_window() -> None:
    listing = make_listing(id="target")
    yesterday = make_listing(id="yesterday", fetched_at=utcnow() - timedelta(hours=20))
    last_week = make_listing(id="lastweek", fetched_at=utcnow() - timedelta(days=7))
    result = select_peers(listing, [yesterday, last_week], max_peer_age_days=2)
    assert [item.id for item in result.exact] == ["yesterday"]


# ----------------------------------------------------------------------------
#  Ориентиры и приоритет источников
# ----------------------------------------------------------------------------

def test_exact_floor_from_source_wins() -> None:
    listing = make_listing(
        id="target", price_nano=40 * TON, floor_by_attributes_nano=50 * TON,
        floor_by_collection_nano=30 * TON,
    )
    result = assess_value(listing, peers=select_peers(listing, peers(5, 55.0)))
    assert result.benchmark_source == "exact_floor"
    assert result.benchmark_nano == 50 * TON
    assert result.diff_percent == 20.0
    assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)


def test_median_of_exact_peers_when_no_floor() -> None:
    listing = make_listing(id="target", price_nano=40 * TON)
    pool = peers(7, 50.0)          # 50.0, 50.5, ... 53.0 -> медиана 51.5
    result = assess_value(listing, peers=select_peers(listing, pool), min_comparables=3)
    assert result.benchmark_source == "peers_exact"
    assert result.benchmark_nano == int(51.5 * TON)
    assert result.comparables == 7
    assert result.confidence == Confidence.HIGH
    assert 0 < result.diff_percent < 30


def test_collection_floor_used_when_exact_peers_are_few() -> None:
    listing = make_listing(
        id="target", price_nano=45 * TON, floor_by_collection_nano=42 * TON,
        model_name="Unique Model",
    )
    result = assess_value(listing, peers=select_peers(listing, peers(2, 50.0, model_name="Unique Model")))
    assert result.benchmark_source == "collection_floor"
    assert result.diff_percent is not None and result.diff_percent < 0   # дороже флора
    assert "не учитывает редкость" in result.note


def test_collection_median_is_low_confidence() -> None:
    listing = make_listing(id="target", price_nano=40 * TON, model_name="Unobtainium")
    pool = peers(8, 50.0, model_name="Other")
    result = assess_value(listing, peers=select_peers(listing, pool), min_comparables=3)
    assert result.benchmark_source == "peers_collection"
    assert result.confidence == Confidence.LOW
    assert "приблизительный" in result.note


# ----------------------------------------------------------------------------
#  Недостаточно данных
# ----------------------------------------------------------------------------

def test_no_peers_and_no_floor_means_no_assessment() -> None:
    listing = make_listing(id="target", price_nano=40 * TON)
    result = assess_value(listing, peers=select_peers(listing, []), min_comparables=3)
    assert result.benchmark_nano is None
    assert result.confidence == Confidence.NONE
    assert result.diff_percent is None
    assert result.is_reliable is False
    assert "не рассчитана" in result.note


def test_few_peers_below_threshold_is_not_reliable() -> None:
    listing = make_listing(id="target", price_nano=40 * TON)
    result = assess_value(listing, peers=select_peers(listing, peers(2, 50.0)), min_comparables=3)
    assert result.benchmark_nano is None
    assert result.confidence == Confidence.NONE
    assert "мало" in result.note


def test_two_peers_with_floor_is_still_low_confidence() -> None:
    listing = make_listing(
        id="target", price_nano=40 * TON, floor_by_collection_nano=50 * TON
    )
    result = assess_value(listing, peers=select_peers(listing, peers(1, 55.0)), min_comparables=3)
    assert result.benchmark_source == "collection_floor"
    assert result.confidence == Confidence.LOW
    assert "ненадёжна" in result.note


def test_zero_price_is_not_assessed() -> None:
    listing = make_listing(id="target", price_nano=0, floor_by_collection_nano=50 * TON)
    result = assess_value(listing)
    assert result.confidence == Confidence.NONE
    assert "неизвестна" in result.note


# ----------------------------------------------------------------------------
#  Комиссии и сопоставимость цен
# ----------------------------------------------------------------------------

def test_fee_is_derived_from_listing_fields() -> None:
    listing = make_listing(
        id="target", price_nano=105 * TON, price_without_fee_nano=100 * TON
    )
    assert listing.implied_fee_permille() == 50          # 5 %
    result = assess_value(listing)
    assert result.fee_permille == 50


def test_fee_unknown_when_fields_missing() -> None:
    listing = make_listing(id="target", price_nano=100 * TON, price_without_fee_nano=None)
    assert listing.implied_fee_permille() is None
    assert assess_value(listing).fee_permille is None


def test_gross_prices_are_compared_consistently() -> None:
    """Ориентир и цена лота — обе брутто: net-цена не подмешивается в сравнение."""
    listing = make_listing(
        id="target", price_nano=50 * TON, price_without_fee_nano=47 * TON
    )
    pool = peers(5, 50.0)   # брутто-цены аналогов
    result = assess_value(listing, peers=select_peers(listing, pool))
    assert result.benchmark_nano is not None
    # Разница считается от брутто-цены лота, а не от цены без комиссии.
    assert abs(result.diff_percent) < 12.0


# ----------------------------------------------------------------------------
#  Порог выгоды и формат описания
# ----------------------------------------------------------------------------

def test_is_interesting_respects_threshold() -> None:
    listing = make_listing(id="target", price_nano=40 * TON, floor_by_attributes_nano=50 * TON)
    result = assess_value(listing)
    assert result.diff_percent == 20.0
    assert is_interesting(result, 10.0) is True
    assert is_interesting(result, 25.0) is False


def test_is_interesting_false_without_benchmark() -> None:
    result = assess_value(make_listing(id="target"))
    assert is_interesting(result, 0.0) is False


def test_describe_mentions_benchmark_and_comparables() -> None:
    listing = make_listing(id="target", price_nano=40 * TON)
    result = assess_value(listing, peers=select_peers(listing, peers(5, 50.0)))
    text = describe(result)
    assert "ориентир" in text and "аналогов" in text
    assert "50.00" in text or "51.50" in text or "51" in text


def test_disclaimer_is_always_present() -> None:
    listing = make_listing(id="target", price_nano=40 * TON, floor_by_attributes_nano=50 * TON)
    assert "не гарантия прибыли" in assess_value(listing).note
