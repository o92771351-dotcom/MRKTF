"""Тесты фильтрации: жёсткие условия отсекают лоты, match_score — плотность."""

from __future__ import annotations

from app.domain import Listing
from app.filters.matching import FilterCriteria, apply
from tests.conftest import make_listing


def test_empty_filter_passes_everything() -> None:
    criteria = FilterCriteria()
    result = apply(make_listing(), criteria)
    assert result.passed is True
    assert result.hard_failures == []
    # Критериев нет -> соответствие полное.
    assert result.match_score == 1.0


def test_collection_filter_is_hard() -> None:
    criteria = FilterCriteria(collection_names=["Frost Fox"])
    assert apply(make_listing(collection_name="Frost Fox"), criteria).passed

    rejected = apply(make_listing(collection_name="Velvet Owl"), criteria)
    assert rejected.passed is False
    assert rejected.match_score == 0.0
    assert any("коллекция" in failure for failure in rejected.hard_failures)


def test_collection_names_case_insensitive() -> None:
    criteria = FilterCriteria(collection_names=["frost fox"])
    assert apply(make_listing(collection_name="Frost Fox"), criteria).passed


def test_price_range_is_hard() -> None:
    criteria = FilterCriteria(price_min_ton=10.0, price_max_ton=100.0)

    assert apply(make_listing(price_nano=50_000_000_000), criteria).passed
    too_cheap = apply(make_listing(price_nano=1_000_000_000), criteria)
    too_pricey = apply(make_listing(price_nano=500_000_000_000), criteria)
    assert too_cheap.passed is False and too_pricey.passed is False
    assert any("ниже минимума" in f for f in too_cheap.hard_failures)
    assert any("выше максимума" in f for f in too_pricey.hard_failures)


def test_price_position_prefers_cheap_end() -> None:
    criteria = FilterCriteria(price_min_ton=10.0, price_max_ton=100.0)
    cheap = apply(make_listing(price_nano=11_000_000_000), criteria)
    expensive = apply(make_listing(price_nano=99_000_000_000), criteria)
    assert cheap.match_score > expensive.match_score


def test_attribute_filters_are_hard() -> None:
    criteria = FilterCriteria(
        model_names=["Noir"], backdrop_names=["Ash Grey"], symbol_names=["Snowflake"]
    )
    assert apply(make_listing(), criteria).passed

    wrong_model = apply(make_listing(model_name="Ember"), criteria)
    assert wrong_model.passed is False
    assert any("модель" in f for f in wrong_model.hard_failures)

    wrong_backdrop = apply(make_listing(backdrop_name="Crimson"), criteria)
    assert wrong_backdrop.passed is False
    assert any("фон" in f for f in wrong_backdrop.hard_failures)

    wrong_symbol = apply(make_listing(symbol_name="Anchor"), criteria)
    assert wrong_symbol.passed is False
    assert any("символ" in f for f in wrong_symbol.hard_failures)


def test_currency_mismatch_rejects() -> None:
    criteria = FilterCriteria(currency="USD")
    result = apply(make_listing(currency="TON"), criteria)
    assert result.passed is False
    assert any("валюта" in f for f in result.hard_failures)


def test_query_matches_any_field() -> None:
    criteria = FilterCriteria(query="noir")
    assert apply(make_listing(model_name="Noir"), criteria).passed
    # Ни в одном поле слова нет -> жёсткий отказ.
    other = make_listing(
        title="Frost Fox · Ember", model_name="Ember", backdrop_name="Crimson", symbol_name="Anchor"
    )
    result = apply(other, criteria)
    assert result.passed is False
    assert any("нет совпадения" in f for f in result.hard_failures)


def test_query_partial_match_scores_lower() -> None:
    criteria = FilterCriteria(query="frost noir")
    full = apply(make_listing(), criteria)            # оба слова найдены
    partial = apply(make_listing(title="Velvet Owl · Noir", collection_name="Velvet Owl"), criteria)
    assert full.passed and partial.passed
    assert full.match_score > partial.match_score


def test_number_equals_is_hard() -> None:
    criteria = FilterCriteria(number_equals=7)
    assert apply(make_listing(number=7), criteria).passed
    assert apply(make_listing(number=8), criteria).passed is False


def test_rarity_filter_uses_rarity_maps() -> None:
    criteria = FilterCriteria(
        min_rarity_percent=2.0,
        rarity_maps={"models": {"noir": 0.5}, "backdrops": {"ash grey": 20.0}},
    )
    # Наименьшая известная редкость = 0.5 % (модель) -> проходит порог ≤ 2 %.
    assert apply(make_listing(), criteria).passed

    common_only = FilterCriteria(
        min_rarity_percent=2.0,
        rarity_maps={"models": {"ember": 25.0}, "backdrops": {"crimson": 30.0}},
    )
    result = apply(make_listing(model_name="Ember", backdrop_name="Crimson"), common_only)
    assert result.passed is False
    assert any("редкость" in f for f in result.hard_failures)


def test_rarity_filter_without_data_does_not_reject() -> None:
    criteria = FilterCriteria(min_rarity_percent=1.0)
    listing = make_listing(
        model_rarity_permille=None, backdrop_rarity_permille=None, symbol_rarity_permille=None
    )
    result = apply(listing, criteria)
    assert result.passed is True
    assert result.match_score < 1.0   # неизвестная редкость не даёт максимума


def test_rare_attribute_scores_higher_than_common() -> None:
    rare = FilterCriteria(rarity_maps={"models": {"noir": 0.1}}, model_names=["Noir"])
    common = FilterCriteria(rarity_maps={"models": {"ember": 40.0}}, model_names=["Ember"])
    rare_score = apply(make_listing(model_name="Noir"), rare).match_score
    common_score = apply(make_listing(model_name="Ember"), common).match_score
    assert rare_score > common_score


def test_monochrome_requirement_is_hard() -> None:
    criteria = FilterCriteria(mono_enabled=True, mono_min_score=60)

    ok = apply(make_listing(), criteria, monochrome_score=82.0, monochrome_ok=True)
    # Единственный критерий — монохром: match_score равен его баллу (82/100).
    assert ok.passed and abs(ok.match_score - 0.82) < 1e-6

    bad = apply(make_listing(), criteria, monochrome_score=20.0, monochrome_ok=False)
    assert bad.passed is False
    assert any("монохромность" in f for f in bad.hard_failures)

    unknown = apply(make_listing(), criteria)   # детектор не отработал
    assert unknown.passed is False
    assert any("не определена" in f for f in unknown.hard_failures)


def test_multiple_failures_are_reported_together() -> None:
    criteria = FilterCriteria(
        collection_names=["Iron Whale"],
        price_max_ton=1.0,
        model_names=["Steel"],
    )
    result = apply(
        make_listing(collection_name="Frost Fox", model_name="Noir", price_nano=50_000_000_000),
        criteria,
    )
    assert result.passed is False
    assert len(result.hard_failures) == 3


def test_filter_criteria_from_row_uses_defaults() -> None:
    class Row:
        id = 1
        name = "Тест"
        query_text = ""
        collection_names = "Frost Fox, Velvet Owl"
        model_names = ""
        backdrop_names = ""
        symbol_names = ""
        currency = "ton"
        price_min_ton = 5
        price_max_ton = None
        mono_enabled = True
        mono_mode = None
        mono_max_saturation = None
        mono_min_score = None
        mono_custom_hue = None
        mono_custom_hue_tolerance = None
        mono_require_confidence = ""
        mono_allow_image = True
        mono_allow_name_heuristic = None
        min_relevance = None
        min_value_percent = None
        min_rarity_percent = None
        number_equals = None

    criteria = FilterCriteria.from_row(
        Row(),
        defaults={"mono_mode": "dark", "mono_min_score": 75, "min_relevance": 55},
    )
    assert criteria.collection_names == ["Frost Fox", "Velvet Owl"]
    assert criteria.currency == "TON"
    assert criteria.mono_mode == "dark"
    assert criteria.mono_min_score == 75
    assert criteria.min_relevance == 55
    assert criteria.price_min_nano == 5_000_000_000
    assert criteria.price_max_nano is None
