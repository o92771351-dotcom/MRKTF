"""Модели SQLAlchemy 2.0 (async).

MVP работает на SQLite через aiosqlite; переход на PostgreSQL — это только
смена `DATABASE_URL` (например `postgresql+asyncpg://...`) и установка
драйвера asyncpg. Специфичных для SQLite вещей в схемах нет: JSON хранится
текстом в `Text`, timestamps — `DateTime(timezone=True)`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain import utcnow


class Base(DeclarativeBase):
    pass


def dumps_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def loads_json(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default if default is not None else {}


class User(Base):
    """Пользователь бота. Настройки хранятся отдельно от фильтров."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weights_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    def weights(self) -> dict[str, float]:
        return loads_json(self.weights_json, {})

    def set_weights(self, weights: dict[str, float]) -> None:
        self.weights_json = dumps_json(weights)


class Filter(Base):
    """Пользовательский фильтр. Все поля — «жёсткие» условия, кроме весов."""

    __tablename__ = "filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), default="Фильтр", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Что ищем
    query_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    collection_names: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_names: Mapped[str] = mapped_column(Text, default="", nullable=False)
    backdrop_names: Mapped[str] = mapped_column(Text, default="", nullable=False)
    symbol_names: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Цена
    currency: Mapped[str] = mapped_column(String(8), default="TON", nullable=False)
    price_min_ton: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max_ton: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Монохромность
    mono_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mono_mode: Mapped[str] = mapped_column(String(16), default="gray", nullable=False)
    mono_max_saturation: Mapped[float] = mapped_column(Float, default=0.12, nullable=False)
    mono_min_score: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    mono_custom_hue: Mapped[float | None] = mapped_column(Float, nullable=True)
    mono_custom_hue_tolerance: Mapped[float] = mapped_column(Float, default=25.0, nullable=False)
    mono_require_confidence: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    mono_allow_image: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mono_allow_name_heuristic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Пороги отбора
    min_relevance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    min_value_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_rarity_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    number_equals: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Мониторинг
    interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    max_results: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_filters_user_enabled", "user_id", "is_enabled"),)

    # Список значений храним строкой через запятую: просто, переносимо,
    # удобно править из интерфейса бота.
    def list_field(self, name: str) -> list[str]:
        raw = getattr(self, name) or ""
        return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]

    def set_list_field(self, name: str, values: list[str]) -> None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = str(value).strip().strip(",")
            if not item or item.lower() in seen:
                continue
            seen.add(item.lower())
            cleaned.append(item)
        setattr(self, name, ",".join(cleaned))


class ListingRow(Base):
    """Кэш увиденных лотов: основа для аналогов (ориентир цены) и дедупликации."""

    __tablename__ = "listings_cache"
    __table_args__ = (
        UniqueConstraint("source", "listing_id", name="uq_listings_source_id"),
        Index("ix_listings_lookup", "source", "collection_name", "model_name", "backdrop_name", "symbol_name"),
        Index("ix_listings_seen", "source", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    listing_id: Mapped[str] = mapped_column(String(120), nullable=False)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    collection_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    backdrop_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    symbol_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    model_rarity_permille: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backdrop_rarity_permille: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol_rarity_permille: Mapped[int | None] = mapped_column(Integer, nullable=True)

    price_nano: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    price_without_fee_nano: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="TON", nullable=False)
    floor_by_collection_nano: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    floor_by_attributes_nano: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    backdrop_center_color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backdrop_edge_color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backdrop_symbol_color: Mapped[int | None] = mapped_column(Integer, nullable=True)
    backdrop_text_color: Mapped[int | None] = mapped_column(Integer, nullable=True)

    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extra_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_price_nano: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    price_change_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SentListing(Base):
    """История отправленных уведомлений — защита от повторов."""

    __tablename__ = "sent_listings"
    __table_args__ = (
        UniqueConstraint("user_id", "filter_id", "listing_id", name="uq_sent_user_filter_listing"),
        Index("ix_sent_user_listing", "user_id", "listing_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    listing_id: Mapped[str] = mapped_column(String(120), nullable=False)
    signature: Mapped[str] = mapped_column(Text, default="", nullable=False)
    price_nano: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    min_price_nano: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    relevance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    monochrome_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    monochrome_method: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    monochrome_confidence: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    diff_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(String(24), default="new", nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resend_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class MonitorRun(Base):
    """Журнал циклов мониторинга — для /status и отладки."""

    __tablename__ = "monitor_runs"
    __table_args__ = (Index("ix_runs_user_started", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    filter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed_filters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_duplicates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="", nullable=False)
