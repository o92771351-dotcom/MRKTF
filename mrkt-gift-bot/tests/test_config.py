"""Конфигурация: `.env.example` обязан быть рабочим, диапазоны — защищёнными."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def test_env_example_is_parseable_as_is() -> None:
    """`cp .env.example .env` не должен ронять бота на старте.

    Пустые значения опциональных полей (MRKT_TOKEN=, TG_API_ID=,
    MONO_CUSTOM_HUE=) обязаны интерпретироваться как «не задано».
    """
    assert ENV_EXAMPLE.exists(), "нет .env.example"
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.bot_token
    assert settings.source == "demo"
    assert settings.mrkt_token == ""
    assert settings.tg_api_id is None
    assert settings.tg_api_hash == ""
    assert settings.mono_custom_hue is None
    assert settings.changes_api_enabled is True


def test_every_env_example_key_maps_to_a_setting() -> None:
    import re

    keys = set(re.findall(r"^([A-Z0-9_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    fields = set(Settings.model_fields)
    unknown = {key for key in keys if key.lower() not in fields}
    assert not unknown, f"переменные в .env.example не используются: {sorted(unknown)}"


def test_page_size_is_capped_at_mrkt_limit() -> None:
    assert Settings(_env_file=None, bot_token="x", mrkt_page_size=500).mrkt_page_size == 20
    assert Settings(_env_file=None, bot_token="x", mrkt_page_size=0).mrkt_page_size == 1


def test_intervals_are_positive() -> None:
    settings = Settings(_env_file=None, bot_token="x", default_interval_minutes=0, min_interval_minutes=-5)
    assert settings.default_interval_minutes == 1
    assert settings.min_interval_minutes == 1


def test_weights_cannot_be_negative() -> None:
    settings = Settings(_env_file=None, bot_token="x", rank_weight_value=-3.0)
    assert settings.rank_weight_value == 0.0


def test_id_lists_are_parsed_and_bad_values_skipped() -> None:
    settings = Settings(_env_file=None, bot_token="x", allowed_user_ids="1, 2;три,4", admin_user_ids="4")
    allowed, admins = settings.id_lists()
    assert allowed == {1, 2, 4}
    assert admins == {4}

    open_settings = Settings(_env_file=None, bot_token="x")
    assert open_settings.id_lists() == (set(), set())


def test_default_weights_match_components() -> None:
    weights = Settings(_env_file=None, bot_token="x").default_weights()
    assert set(weights) == {"match", "monochrome", "value", "confidence", "freshness"}
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_demo_flag_follows_source() -> None:
    assert Settings(_env_file=None, bot_token="x", source="demo").is_demo is True
    assert Settings(_env_file=None, bot_token="x", source="mrkt").is_demo is False


def test_ensure_dirs_creates_data_and_cache(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        bot_token="x",
        data_dir=str(tmp_path / "nested" / "data"),
        cache_dir=str(tmp_path / "nested" / "cache"),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sub' / 'bot.db'}",
    )
    settings.ensure_dirs()
    assert settings.data_dir and Path(settings.data_dir).is_dir()
    assert Path(settings.cache_dir).is_dir()
    assert Path(tmp_path / "sub").is_dir()


def test_unknown_env_key_is_ignored_by_default() -> None:
    """Лишняя переменная в окружении не должна ломать запуск."""
    settings = Settings(_env_file=None, bot_token="x", SOMETHING_UNEXPECTED="1")  # type: ignore[call-arg]
    assert settings.bot_token == "x"


def test_invalid_numeric_env_value_fails_loudly() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bot_token="x", image_size="большой")
