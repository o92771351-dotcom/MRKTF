"""Тесты ранжирования: формула, веса, перераспределение при отсутствии данных."""

from __future__ import annotations

from datetime import timedelta

from app.domain import Confidence, MatchResult, MonochromeAssessment, ValueAssessment, utcnow
from app.ranking import (
    DEFAULT_WEIGHTS,
    RankingWeights,
    confidence_component,
    explain,
    rank,
    value_component,
)
from tests.conftest import make_listing

TON = 1_000_000_000


def match(score: float = 1.0, passed: bool = True) -> MatchResult:
    return MatchResult(passed=passed, hard_failures=[], match_score=score,
                       matched_criteria=["x"], total_criteria=1)


def mono(score: float, confidence: Confidence = Confidence.HIGH) -> MonochromeAssessment:
    return MonochromeAssessment(score=score, method="backdrop_colors", confidence=confidence)


def value(diff_percent: float | None, confidence: Confidence = Confidence.HIGH,
          comparables: int = 10) -> ValueAssessment:
    return ValueAssessment(
        price_nano=40 * TON,
        benchmark_nano=50 * TON if diff_percent is not None else None,
        diff_percent=diff_percent,
        comparables=comparables,
        benchmark_source="peers_exact" if diff_percent is not None else "none",
        confidence=confidence if diff_percent is not None else Confidence.NONE,
    )


# ----------------------------------------------------------------------------
#  Формула
# ----------------------------------------------------------------------------

def test_perfect_listing_scores_100() -> None:
    listing = make_listing(listed_at=utcnow())
    weights = RankingWeights()
    result = rank(listing, match=match(1.0), monochrome=mono(100.0),
                  value=value(100.0), weights=weights)
    assert result.score == 100.0
    assert set(result.components) == {"match", "monochrome", "value", "confidence", "freshness"}


def test_score_is_weighted_average_of_components() -> None:
    listing = make_listing(listed_at=utcnow())
    weights = RankingWeights(match=1.0, monochrome=1.0, value=1.0, confidence=1.0, freshness=0.0)
    result = rank(
        listing,
        match=match(1.0),
        monochrome=mono(50.0),
        value=value(0.0),          # 0 % выгоды -> компонент 0
        weights=weights,
    )
    # (1.0 + 0.5 + 0.0 + 1.0 + -) / 4 = 0.625 -> 62.5
    assert abs(result.score - 62.5) < 0.05


def test_failed_match_zeroes_the_component() -> None:
    listing = make_listing()
    weights = RankingWeights(match=1.0, monochrome=0.0, value=0.0, confidence=0.0, freshness=0.0)
    result = rank(listing, match=match(0.9, passed=False), monochrome=None, value=None, weights=weights)
    assert result.components["match"] == 0.0
    assert result.score == 0.0


def test_value_component_clamped_and_signed() -> None:
    assert value_component(value(20.0)) == 0.2
    assert value_component(value(250.0)) == 1.0     # кламп сверху
    assert value_component(value(-30.0)) == 0.0     # дороже ориентира
    assert value_component(value(None)) is None     # нет ориентира
    assert value_component(None) is None


def test_confidence_component_averages_available_signals() -> None:
    both = confidence_component(value(10.0, Confidence.HIGH), mono(80.0, Confidence.HIGH))
    assert both == 1.0
    mixed = confidence_component(value(10.0, Confidence.LOW), mono(80.0, Confidence.HIGH))
    assert mixed == (0.3 + 1.0) / 2
    only_mono = confidence_component(value(None), mono(80.0, Confidence.MEDIUM))
    assert only_mono == 0.6
    assert confidence_component(value(None), None) is None
    assert confidence_component(value(None), mono(0.0, Confidence.NONE)) is None


def test_freshness_decays_with_age() -> None:
    now = utcnow()
    fresh = make_listing(id="fresh", listed_at=now)
    half_life = make_listing(id="half", listed_at=now - timedelta(hours=12))
    old = make_listing(id="old", listed_at=now - timedelta(hours=120))
    weights = RankingWeights(match=0.0, monochrome=0.0, value=0.0, confidence=0.0, freshness=1.0)

    scores = {
        item.id: rank(item, match=match(0.0), monochrome=None, value=None,
                      weights=weights, freshness_half_life_hours=12.0).score
        for item in (fresh, half_life, old)
    }
    assert scores["fresh"] > scores["half"] > scores["old"]
    assert abs(scores["half"] - 50.0) < 3.0


def test_unknown_listing_date_gets_neutral_freshness() -> None:
    listing = make_listing(listed_at=None)
    weights = RankingWeights(match=0.0, monochrome=0.0, value=0.0, confidence=0.0, freshness=1.0)
    result = rank(listing, match=match(0.0), monochrome=None, value=None, weights=weights)
    assert abs(result.score - 50.0) < 0.01


# ----------------------------------------------------------------------------
#  Перераспределение весов при отсутствии данных
# ----------------------------------------------------------------------------

def test_missing_value_redistributes_weight() -> None:
    listing = make_listing(listed_at=utcnow())
    weights = RankingWeights(match=1.0, monochrome=1.0, value=1.0, confidence=0.0, freshness=0.0)

    with_value = rank(listing, match=match(1.0), monochrome=mono(100.0),
                      value=value(0.0), weights=weights)
    without_value = rank(listing, match=match(1.0), monochrome=mono(100.0),
                         value=None, weights=weights)

    # Без ориентира цены лот не должен тонуть: вес value ушёл к match и monochrome.
    assert "value" not in without_value.components
    assert without_value.score == 100.0
    assert with_value.score < without_value.score


def test_all_zero_weights_fall_back_to_plain_average() -> None:
    listing = make_listing(listed_at=utcnow())
    weights = RankingWeights(match=0.0, monochrome=0.0, value=0.0, confidence=0.0, freshness=0.0)
    result = rank(listing, match=match(1.0), monochrome=mono(50.0),
                  value=value(0.0), weights=weights)
    # (1.0 + 0.5 + 0.0 + 1.0 + ~1.0) / 5 = 0.7 -> 70.0
    assert 60.0 < result.score < 80.0


def test_score_never_leaves_0_100() -> None:
    listing = make_listing(listed_at=utcnow() + timedelta(hours=5))   # «будущий» лот
    weights = RankingWeights()
    result = rank(listing, match=match(1.0), monochrome=mono(100.0),
                  value=value(500.0), weights=weights)
    assert 0.0 <= result.score <= 100.0


# ----------------------------------------------------------------------------
#  Веса
# ----------------------------------------------------------------------------

def test_weights_normalization() -> None:
    weights = RankingWeights(match=2.0, monochrome=2.0, value=0.0, confidence=0.0, freshness=0.0)
    norm = weights.normalized()
    assert abs(sum(norm.values()) - 1.0) < 1e-9
    assert norm["match"] == 0.5


def test_weights_from_settings_and_mapping() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, bot_token="x", rank_weight_value=0.5)
    from_settings = RankingWeights.from_settings(settings)
    assert from_settings.value == 0.5

    from_map = RankingWeights.from_mapping({"value": 0.25, "match": "0.4", "bogus": 1})
    assert from_map.value == 0.25
    assert from_map.match == 0.4
    # Неизвестные ключи игнорируются, остальные берут дефолты.
    assert from_map.monochrome == DEFAULT_WEIGHTS["monochrome"]


def test_weights_clamped_to_0_1() -> None:
    weights = RankingWeights.from_mapping({"value": 5, "match": -3})
    assert weights.value == 1.0
    assert weights.match == 0.0


def test_explain_lists_all_components() -> None:
    listing = make_listing(listed_at=utcnow())
    result = rank(listing, match=match(0.8), monochrome=mono(70.0),
                  value=value(15.0), weights=RankingWeights())
    text = explain(result)
    assert "Релевантность" in text
    for label in ("соответствие фильтрам", "монохромность", "выгода",
                  "надёжность данных", "свежесть"):
        assert label in text


def test_explain_marks_missing_components() -> None:
    listing = make_listing(listed_at=utcnow())
    result = rank(listing, match=match(1.0), monochrome=None, value=None, weights=RankingWeights())
    text = explain(result)
    assert "нет данных" in text


def test_higher_value_gives_higher_rank() -> None:
    listing = make_listing(listed_at=utcnow())
    weights = RankingWeights()
    cheap = rank(listing, match=match(1.0), monochrome=mono(90.0), value=value(35.0), weights=weights)
    pricey = rank(listing, match=match(1.0), monochrome=mono(90.0), value=value(2.0), weights=weights)
    assert cheap.score > pricey.score


def test_higher_monochrome_gives_higher_rank() -> None:
    listing = make_listing(listed_at=utcnow())
    weights = RankingWeights()
    mono_high = rank(listing, match=match(1.0), monochrome=mono(95.0), value=value(10.0), weights=weights)
    mono_low = rank(listing, match=match(1.0), monochrome=mono(20.0), value=value(10.0), weights=weights)
    assert mono_high.score > mono_low.score
