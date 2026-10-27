"""Загрузка конфигурации из переменных окружения / файла .env.

Все настройки — плоские поля (без вложенных моделей), чтобы pydantic-settings
не требовал `env_nested_delimiter` и конфигурацию было легко переопределять
в docker-compose.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

SourceName = Literal["demo", "mrkt"]
MonoMode = Literal["gray", "dark", "light", "custom"]


class Settings(BaseSettings):
    """Настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ------------------------------------------------------------
    bot_token: str = ""
    allowed_user_ids: str = ""
    admin_user_ids: str = ""

    # --- Источник ------------------------------------------------------------
    source: SourceName = "demo"

    mrkt_base_url: str = "https://api.tgmrkt.io/api/v1"
    mrkt_referer: str = "https://cdn.tgmrkt.io/"
    mrkt_token: str = ""

    tg_api_id: int | None = None
    tg_api_hash: str = ""
    tg_session_name: str = "mrkt_service"
    tg_session_workdir: str = "./sessions"

    mrkt_request_delay: float = 2.0
    mrkt_page_size: int = 20
    mrkt_max_pages: int = 5
    mrkt_timeout: float = 20.0
    mrkt_max_retries: int = 3
    # Экспоненциальная пауза между повторами: base ** попытка, не больше cap.
    mrkt_retry_backoff_base: float = 2.0
    mrkt_retry_backoff_max: float = 15.0
    mrkt_link_template: str = "https://t.me/mrkt/app"
    # Ключи картинок MRKT приходят относительными ("gifts/stickers/...webp") —
    # префикс CDN подставляется в app/sources/mrkt.py.
    mrkt_cdn_url: str = "https://cdn.tgmrkt.io/"
    # Если в запросе не задана коллекция, API отдаёт total=0: приходится
    # перебирать коллекции. Это лимит коллекций на один цикл (защита от 429).
    mrkt_max_collections: int = 12

    # --- @GiftChanges --------------------------------------------------------
    changes_api_enabled: bool = True
    changes_api_url: str = "https://api.changes.tg"
    changes_cache_ttl: int = 86400
    changes_request_delay: float = 0.4

    # --- Мониторинг ----------------------------------------------------------
    poll_tick_seconds: int = 20
    default_interval_minutes: int = 15
    min_interval_minutes: int = 5
    max_results_per_run: int = 5
    send_delay_seconds: float = 1.2

    resend_price_drop_percent: float = 5.0
    resend_after_hours: int = 24

    # --- Оценка выгоды -------------------------------------------------------
    value_min_comparables: int = 3
    value_peer_lookback_days: int = 3

    # --- Ранжирование --------------------------------------------------------
    rank_weight_match: float = 0.25
    rank_weight_monochrome: float = 0.25
    rank_weight_value: float = 0.25
    rank_weight_confidence: float = 0.15
    rank_weight_freshness: float = 0.10
    rank_freshness_half_life_hours: float = 12.0
    default_min_relevance: int = 60

    # --- Монохром ------------------------------------------------------------
    mono_mode_default: MonoMode = "gray"
    mono_max_saturation: float = 0.12
    mono_min_score: int = 60
    mono_image_enabled: bool = True
    mono_image_analysis_timeout: float = 15.0
    mono_allow_name_heuristic: bool = True
    mono_custom_hue: float | None = None
    mono_custom_hue_tolerance: float = 25.0

    # --- Хранилище -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    db_echo: bool = False

    # --- Прочее --------------------------------------------------------------
    log_level: str = "INFO"
    data_dir: str = "./data"
    cache_dir: str = "./data/cache"
    image_enabled: bool = True
    image_max_bytes: int = 4_000_000
    image_size: int = 512

    # ------------------------------------------------------------------ валидация
    @field_validator("mono_custom_hue", "tg_api_id", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v: Any) -> Any:
        """Пустое значение в .env (`MONO_CUSTOM_HUE=`, `TG_API_ID=`) означает
        «не задано», а не ошибку запуска. Без этого cp .env.example .env
        уронил бы бота на старте."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("mrkt_page_size")
    @classmethod
    def _clamp_page_size(cls, v: int) -> int:
        # В неофициальной документации MRKT указан лимит 20 на страницу.
        return max(1, min(v, 20))

    @field_validator("default_interval_minutes", "min_interval_minutes")
    @classmethod
    def _positive_minutes(cls, v: int) -> int:
        return max(1, v)

    @field_validator("rank_weight_match", "rank_weight_monochrome", "rank_weight_value",
                     "rank_weight_confidence", "rank_weight_freshness")
    @classmethod
    def _non_negative_weight(cls, v: float) -> float:
        return max(0.0, v)

    @property
    def is_demo(self) -> bool:
        return self.source == "demo"

    def id_lists(self) -> tuple[set[int], set[int]]:
        """Возвращает (allowed_user_ids, admin_user_ids)."""
        return _parse_ids(self.allowed_user_ids), _parse_ids(self.admin_user_ids)

    def default_weights(self) -> dict[str, float]:
        return {
            "match": self.rank_weight_match,
            "monochrome": self.rank_weight_monochrome,
            "value": self.rank_weight_value,
            "confidence": self.rank_weight_confidence,
            "freshness": self.rank_weight_freshness,
        }

    def ensure_dirs(self) -> None:
        for raw in (self.data_dir, self.cache_dir):
            Path(raw).mkdir(parents=True, exist_ok=True)
        # Каталог для sqlite, если путь указан в DATABASE_URL.
        if self.database_url.startswith("sqlite"):
            tail = self.database_url.split("///", 1)[-1]
            if tail and tail != ":memory:":
                Path(tail).parent.mkdir(parents=True, exist_ok=True)


def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            logger.warning("Некорректный Telegram ID в списке доступа: %r", chunk)
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
