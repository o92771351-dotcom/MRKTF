"""Тесты разбора пользовательского ввода (фильтры и веса ранжирования).

Проверяется то, что пользователь видит напрямую: допустимые значения,
сброс через «-», ограничения диапазонов и понятные сообщения об ошибках.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.handlers.filters_ui import (
    FIELD_SPECS,
    ParseError,
    parse_field,
    validate_consistency,
)
from app.bot.handlers.status import parse_weights_input


# ----------------------------------------------------------------------------
#  Списки
# ----------------------------------------------------------------------------

def test_list_field_splits_on_comma_and_semicolon() -> None:
    assert parse_field("collection_names", "Plush Pepe, Frost Fox") == "Plush Pepe,Frost Fox"
    assert parse_field("model_names", "Donatello; Raphael ;Leo") == "Donatello,Raphael,Leo"


def test_list_field_keeps_names_with_internal_spaces() -> None:
    assert parse_field("backdrop_names", "Ash Grey,Midnight Blue") == "Ash Grey,Midnight Blue"


def test_empty_list_is_rejected() -> None:
    with pytest.raises(ParseError):
        parse_field("collection_names", "   ")


def test_list_reset_token_clears_value() -> None:
    assert parse_field("collection_names", "-") == ""
    assert parse_field("collection_names", "сброс") == ""


# ----------------------------------------------------------------------------
#  Опциональные поля и сброс
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["price_min_ton", "price_max_ton", "model_names"])
def test_optional_fields_accept_reset_tokens(field: str) -> None:
    for token in ("-", "нет", "none"):
        value = parse_field(field, token)
        assert value in (None, ""), f"{field}: {token} -> {value!r}"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("min_relevance", 0.0),
        ("mono_min_score", 60),
        ("mono_max_saturation", 0.12),
        ("interval_minutes", 15),
        ("max_results", 5),
    ],
)
def test_non_optional_fields_reset_to_defaults(field: str, expected: float) -> None:
    """Поле не может быть пустым — «-» возвращает значение по умолчанию."""
    assert parse_field(field, "-") == expected


def test_blank_input_is_not_silently_resetting_value() -> None:
    with pytest.raises(ParseError):
        parse_field("price_min_ton", "   ")
    with pytest.raises(ParseError):
        parse_field("interval_minutes", "")


def test_name_cannot_be_cleared() -> None:
    with pytest.raises(ParseError):
        parse_field("name", "-")


# ----------------------------------------------------------------------------
#  Числа и диапазоны
# ----------------------------------------------------------------------------

def test_price_accepts_comma_decimal_and_rounds() -> None:
    assert parse_field("price_min_ton", "12,5") == 12.5
    assert parse_field("price_max_ton", "0.123456789") == 0.123457


def test_negative_price_is_rejected() -> None:
    with pytest.raises(ParseError) as info:
        parse_field("price_min_ton", "-3")
    assert "отрицательн" in str(info.value)


def test_relevance_is_clamped_to_0_100() -> None:
    assert parse_field("min_relevance", "140") == 100.0
    assert parse_field("min_relevance", "-5") == 0.0


def test_interval_has_positive_minimum() -> None:
    assert parse_field("interval_minutes", "0") == 1
    assert parse_field("interval_minutes", "-10") == 1
    assert parse_field("interval_minutes", "30") == 30


def test_max_results_is_capped() -> None:
    assert parse_field("max_results", "1000") == 25
    assert parse_field("max_results", "0") == 1


def test_mono_score_and_saturation_are_clamped() -> None:
    assert parse_field("mono_min_score", "250") == 100
    assert parse_field("mono_min_score", "-1") == 0
    assert parse_field("mono_max_saturation", "5") == 0.9
    assert parse_field("mono_max_saturation", "0") == 0.005


def test_custom_hue_wraps_into_0_360() -> None:
    assert parse_field("mono_custom_hue", "720") == 360.0
    assert parse_field("mono_custom_hue", "-30") == 0.0
    assert parse_field("mono_custom_hue_tolerance", "0.5") == 1.0


def test_min_value_percent_allows_negative_but_bounded() -> None:
    assert parse_field("min_value_percent", "-500") == -100.0
    assert parse_field("min_value_percent", "15") == 15.0


def test_non_numeric_input_reports_error() -> None:
    with pytest.raises(ParseError) as info:
        parse_field("price_min_ton", "много")
    assert "число" in str(info.value)

    with pytest.raises(ParseError):
        parse_field("interval_minutes", "12,5 минут")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ParseError):
        parse_field("secret_field", "1")


def test_every_declared_field_has_label_and_hint() -> None:
    """UI строится из FIELD_SPECS — без подписи кнопка будет пустой."""
    assert FIELD_SPECS, "FIELD_SPECS пуст"
    for field, spec in FIELD_SPECS.items():
        assert spec.get("label"), f"{field}: нет label"
        assert spec.get("hint"), f"{field}: нет hint"
        assert spec.get("type"), f"{field}: нет type"


@pytest.mark.parametrize(
    "field",
    ["collection_names", "model_names", "backdrop_names", "symbol_names", "query_text",
     "price_min_ton", "price_max_ton", "interval_minutes", "max_results",
     "min_relevance", "mono_min_score", "mono_max_saturation"],
)
def test_key_fields_are_parseable(field: str) -> None:
    """Все поля из спецификации должны уметь принимать корректное значение."""
    kind = FIELD_SPECS[field]["type"]
    sample = {
        "list": "Value One,Value Two",
        "str": "текст",
        "int": "10",
        "float": "10.5",
        "int?": "10",
        "float?": "10.5",
    }.get(kind)
    assert sample is not None, f"{field}: неизвестный тип {kind}"
    parse_field(field, sample)          # не должно бросать исключение


# ----------------------------------------------------------------------------
#  Проверка противоречий
# ----------------------------------------------------------------------------

def _row(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        collection_names="Frost Fox",
        query_text="",
        model_names="",
        price_min_ton=10.0,
        price_max_ton=50.0,
        mono_enabled=True,
        mono_mode="grey",
        mono_custom_hue=None,
        interval_minutes=15,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_consistency_warns_when_min_price_above_max() -> None:
    warnings = validate_consistency(_row(price_min_ton=100.0, price_max_ton=10.0))  # type: ignore[arg-type]
    assert any("минимальная цена больше максимальной" in w for w in warnings)


def test_consistency_warns_about_custom_mode_without_hue() -> None:
    warnings = validate_consistency(_row(mono_mode="custom", mono_custom_hue=None))  # type: ignore[arg-type]
    assert any("custom" in w for w in warnings)


def test_consistency_warns_about_market_wide_search() -> None:
    warnings = validate_consistency(_row(collection_names="", query_text="", model_names=""))  # type: ignore[arg-type]
    assert any("весь рынок" in w for w in warnings)


def test_consistency_warns_about_too_short_interval() -> None:
    warnings = validate_consistency(_row(interval_minutes=1))  # type: ignore[arg-type]
    assert any("интервал" in w.lower() for w in warnings)


def test_consistency_is_silent_for_sane_filter() -> None:
    assert validate_consistency(_row()) == []  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
#  Веса ранжирования
# ----------------------------------------------------------------------------

def test_weights_accept_key_value_pairs() -> None:
    values, error = parse_weights_input("value=0.4 mono=0.3")
    assert error is None
    assert values == {"value": 0.4, "monochrome": 0.3}


def test_weights_accept_positional_input() -> None:
    values, error = parse_weights_input("1 2 3 4 5")
    assert error is None
    assert values["value"] == 3.0

    values, error = parse_weights_input("1,2,3,4,5")
    assert error is None
    assert list(values) == ["match", "monochrome", "value", "confidence", "freshness"]
    assert values["value"] == 3.0


def test_weights_accept_commas_and_european_decimal() -> None:
    values, error = parse_weights_input("value=0,4, mono=0,3")
    assert error is None
    assert values["value"] == 0.4
    assert values["monochrome"] == 0.3


def test_weights_accept_short_aliases() -> None:
    values, error = parse_weights_input("mono=30, conf=10, fresh=5")
    assert error is None
    assert values == {"monochrome": 30.0, "confidence": 10.0, "freshness": 5.0}


def test_weights_clamp_out_of_range_values() -> None:
    values, error = parse_weights_input("value=500 mono=-10")
    assert error is None
    assert values["value"] == 100.0
    assert values["monochrome"] == 0.0


def test_weights_reject_unknown_component() -> None:
    values, error = parse_weights_input("profit=1")
    assert values == {}
    assert error is not None and "profit" in error
    assert "monochrome" in error      # подсказываем доступные имена


def test_weights_reject_garbage() -> None:
    for raw in ("", "   ", "value=много", "1 2 3 4 5 6 7"):
        values, error = parse_weights_input(raw)
        assert values == {}, raw
        assert error, raw


def test_weights_reject_all_zero_full_set() -> None:
    values, error = parse_weights_input("0 0 0 0 0")
    assert values == {}
    assert error is not None and "нулевые" in error


def test_weights_allow_partial_update() -> None:
    values, error = parse_weights_input("value=1")
    assert error is None
    assert values == {"value": 1.0}
