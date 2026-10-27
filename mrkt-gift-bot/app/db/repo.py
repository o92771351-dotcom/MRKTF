"""Репозиторий: все обращения к БД в одном месте.

Каждый метод принимает `user_id` там, где данные принадлежат пользователю —
настройки разных пользователей не смешиваются (требование задачи).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Filter,
    ListingRow,
    MonitorRun,
    SentListing,
    User,
    dumps_json,
    loads_json,
)
from app.domain import Listing, SourceStatus, ensure_aware, utcnow

# Поля с датами, которые нужно приводить к UTC после чтения из БД.
_DATETIME_FIELDS: dict[type, tuple[str, ...]] = {
    User: ("created_at", "last_seen_at"),
    Filter: ("last_run_at", "created_at", "updated_at"),
    ListingRow: ("listed_at", "first_seen_at", "last_seen_at"),
    SentListing: ("sent_at", "last_seen_at"),
    MonitorRun: ("started_at", "finished_at"),
}


def _aware(row: Any) -> Any:
    """Нормализовать временные метки ORM-строки (SQLite отдаёт наивные)."""
    if row is None:
        return None
    fields = _DATETIME_FIELDS.get(type(row))
    if not fields:
        return row
    for name in fields:
        value = getattr(row, name, None)
        if isinstance(value, datetime):
            setattr(row, name, ensure_aware(value))
    return row

logger = logging.getLogger(__name__)


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ------------------------------------------------------------------ users
    async def touch_user(
        self, user_id: int, *, username: str | None = None, first_name: str | None = None
    ) -> User:
        row = await self.s.get(User, user_id)
        now = utcnow()
        if row is None:
            row = User(
                id=user_id,
                username=username,
                first_name=first_name,
                created_at=now,
                last_seen_at=now,
                weights_json="{}",
                is_paused=False,
            )
            self.s.add(row)
        else:
            row.last_seen_at = now
            if username is not None:
                row.username = username
            if first_name is not None:
                row.first_name = first_name
        await self.s.commit()
        await self.s.refresh(row)
        return _aware(row)

    async def get_user(self, user_id: int) -> User | None:
        return _aware(await self.s.get(User, user_id))

    async def set_paused(self, user_id: int, paused: bool) -> None:
        await self.s.execute(
            update(User).where(User.id == user_id).values(is_paused=paused, last_seen_at=utcnow())
        )
        await self.s.commit()

    async def set_weights(self, user_id: int, weights: dict[str, float]) -> None:
        await self.s.execute(
            update(User).where(User.id == user_id).values(weights_json=dumps_json(weights))
        )
        await self.s.commit()

    async def active_users(self) -> list[User]:
        rows = await self.s.execute(select(User).where(User.is_paused.is_(False)))
        return [_aware(row) for row in rows.scalars().all()]

    # ---------------------------------------------------------------- filters
    async def create_filter(self, user_id: int, **values: Any) -> Filter:
        row = Filter(user_id=user_id, **values)
        self.s.add(row)
        await self.s.commit()
        await self.s.refresh(row)
        return _aware(row)

    async def list_filters(self, user_id: int, *, enabled_only: bool = False) -> list[Filter]:
        stmt = select(Filter).where(Filter.user_id == user_id).order_by(Filter.id)
        if enabled_only:
            stmt = stmt.where(Filter.is_enabled.is_(True))
        rows = await self.s.execute(stmt)
        return [_aware(row) for row in rows.scalars().all()]

    async def get_filter(self, user_id: int, filter_id: int) -> Filter | None:
        row = await self.s.get(Filter, filter_id)
        if row is None or row.user_id != user_id:
            return None
        return _aware(row)

    async def update_filter(self, user_id: int, filter_id: int, **values: Any) -> Filter | None:
        row = await self.get_filter(user_id, filter_id)
        if row is None:
            return None
        for key, value in values.items():
            if hasattr(row, key):
                setattr(row, key, value)
        row.updated_at = utcnow()
        await self.s.commit()
        await self.s.refresh(row)
        return _aware(row)

    async def delete_filter(self, user_id: int, filter_id: int) -> bool:
        row = await self.get_filter(user_id, filter_id)
        if row is None:
            return False
        await self.s.execute(delete(SentListing).where(SentListing.filter_id == filter_id))
        await self.s.delete(row)
        await self.s.commit()
        return True

    async def mark_filter_run(
        self, user_id: int, filter_id: int, *, status: str, message: str = ""
    ) -> None:
        await self.s.execute(
            update(Filter)
            .where(Filter.id == filter_id, Filter.user_id == user_id)
            .values(last_run_at=utcnow(), last_status=(message or status)[:900])
        )
        await self.s.commit()

    # --------------------------------------------------------- listings cache
    async def upsert_listings(self, listings: Sequence[Listing]) -> int:
        """Сохранить/обновить увиденные лоты. Возвращает число обработанных."""
        if not listings:
            return 0
        now = utcnow()
        # Группируем по источнику: id лотов уникальны только в пределах источника.
        by_source: dict[str, list[Listing]] = {}
        for item in listings:
            by_source.setdefault(item.source, []).append(item)

        existing: dict[tuple[str, str], ListingRow] = {}
        for source, items in by_source.items():
            rows = await self.s.execute(
                select(ListingRow).where(
                    ListingRow.source == source,
                    ListingRow.listing_id.in_([item.id for item in items]),
                )
            )
            for row in rows.scalars().all():
                existing[(row.source, row.listing_id)] = row

        for item in listings:
            row = existing.get((item.source, item.id))
            if row is None:
                row = ListingRow(
                    source=item.source,
                    listing_id=item.id,
                    first_seen_at=now,
                    price_change_count=0,
                )
                self.s.add(row)
            elif row.price_nano != item.price_nano:
                row.price_change_count = (row.price_change_count or 0) + 1

            row.is_test_data = bool(item.is_test_data)
            row.collection_name = (item.collection_name or "")[:120]
            row.title = item.title or ""
            row.number = item.number
            row.model_name = item.model_name[:120] if item.model_name else None
            row.backdrop_name = item.backdrop_name[:120] if item.backdrop_name else None
            row.symbol_name = item.symbol_name[:120] if item.symbol_name else None
            row.model_rarity_permille = item.model_rarity_permille
            row.backdrop_rarity_permille = item.backdrop_rarity_permille
            row.symbol_rarity_permille = item.symbol_rarity_permille
            row.price_nano = int(item.price_nano)
            row.price_without_fee_nano = item.price_without_fee_nano
            row.currency = item.currency or "TON"
            row.floor_by_collection_nano = item.floor_by_collection_nano
            row.floor_by_attributes_nano = item.floor_by_attributes_nano
            row.backdrop_center_color = item.backdrop_center_color
            row.backdrop_edge_color = item.backdrop_edge_color
            row.backdrop_symbol_color = item.backdrop_symbol_color
            row.backdrop_text_color = item.backdrop_text_color
            row.image_url = item.image_url
            row.url = item.url
            row.listed_at = item.listed_at
            row.is_active = bool(item.is_active)
            row.extra_json = dumps_json(item.extra)
            row.last_seen_at = now
            row.last_price_nano = int(item.price_nano)

        await self.s.commit()
        return len(listings)

    async def mark_missing_inactive(self, source: str, seen_ids: Sequence[str]) -> int:
        """Лоты, которых нет в свежей выдаче, считаем неактивными (проданы/сняты)."""
        if not seen_ids:
            return 0
        stmt = (
            update(ListingRow)
            .where(
                ListingRow.source == source,
                ListingRow.is_active.is_(True),
                ListingRow.listing_id.not_in(list(seen_ids)),
            )
            .values(is_active=False)
        )
        result = await self.s.execute(stmt)
        await self.s.commit()
        return int(result.rowcount or 0)

    async def peers_for(self, source: str, listing: Listing, *, days: int) -> list[Listing]:
        """Аналоги для оценки выгоды: та же коллекция, активные, свежие."""
        cutoff = utcnow() - timedelta(days=max(1, days))
        stmt = (
            select(ListingRow)
            .where(
                ListingRow.source == source,
                ListingRow.collection_name == listing.collection_name,
                ListingRow.is_active.is_(True),
                ListingRow.last_seen_at >= cutoff,
                ListingRow.is_test_data.is_(bool(listing.is_test_data)),
            )
            .limit(1500)
        )
        rows = await self.s.execute(stmt)
        return [row_to_listing(_aware(row)) for row in rows.scalars().all()]

    async def cache_stats(self, source: str | None = None) -> dict[str, int]:
        """Простая и переносимая статистика двумя запросами (SQLite/Postgres)."""
        total_stmt = select(func.count(ListingRow.id))
        active_stmt = select(func.count(ListingRow.id)).where(ListingRow.is_active.is_(True))
        if source:
            total_stmt = total_stmt.where(ListingRow.source == source)
            active_stmt = active_stmt.where(ListingRow.source == source)
        total = (await self.s.execute(total_stmt)).scalar_one()
        active = (await self.s.execute(active_stmt)).scalar_one()
        return {"total": int(total or 0), "active": int(active or 0)}

    # ---------------------------------------------------------- sent listings
    async def get_sent(self, user_id: int, filter_id: int | None, listing_id: str) -> SentListing | None:
        stmt = select(SentListing).where(
            SentListing.user_id == user_id, SentListing.listing_id == listing_id
        )
        if filter_id is None:
            stmt = stmt.where(SentListing.filter_id.is_(None))
        else:
            stmt = stmt.where(SentListing.filter_id == filter_id)
        rows = await self.s.execute(stmt)
        return _aware(rows.scalars().first())

    async def record_sent(
        self,
        *,
        user_id: int,
        filter_id: int | None,
        listing: Listing,
        relevance: float,
        monochrome_score: float,
        monochrome_method: str,
        monochrome_confidence: str,
        diff_percent: float | None,
        reason: str,
        existing: SentListing | None = None,
    ) -> SentListing:
        now = utcnow()
        if existing is not None:
            existing.price_nano = int(listing.price_nano)
            existing.min_price_nano = min(int(existing.min_price_nano or listing.price_nano), int(listing.price_nano))
            existing.signature = listing.signature()
            existing.relevance = float(relevance)
            existing.monochrome_score = float(monochrome_score)
            existing.monochrome_method = monochrome_method[:32]
            existing.monochrome_confidence = monochrome_confidence[:8]
            existing.diff_percent = diff_percent
            existing.reason = reason[:24]
            existing.sent_at = now
            existing.last_seen_at = now
            existing.resend_count = int(existing.resend_count or 0) + 1
            row = existing
        else:
            row = SentListing(
                user_id=user_id,
                filter_id=filter_id,
                source=listing.source,
                listing_id=listing.id,
                signature=listing.signature(),
                price_nano=int(listing.price_nano),
                min_price_nano=int(listing.price_nano),
                relevance=float(relevance),
                monochrome_score=float(monochrome_score),
                monochrome_method=monochrome_method[:32],
                monochrome_confidence=monochrome_confidence[:8],
                diff_percent=diff_percent,
                reason=reason[:24],
                sent_at=now,
                last_seen_at=now,
                resend_count=0,
            )
            self.s.add(row)
        await self.s.commit()
        await self.s.refresh(row)
        return _aware(row)

    async def touch_sent(self, row: SentListing, listing: Listing) -> None:
        """Лот видели снова, но не отправляли: обновляем цену и время наблюдения."""
        row.last_seen_at = utcnow()
        row.price_nano = int(listing.price_nano)
        row.min_price_nano = min(int(row.min_price_nano or listing.price_nano), int(listing.price_nano))
        row.signature = listing.signature()
        await self.s.commit()

    async def history(self, user_id: int, limit: int = 20) -> list[SentListing]:
        stmt = (
            select(SentListing)
            .where(SentListing.user_id == user_id)
            .order_by(SentListing.sent_at.desc())
            .limit(limit)
        )
        rows = await self.s.execute(stmt)
        return [_aware(row) for row in rows.scalars().all()]

    async def clear_history(self, user_id: int, filter_id: int | None = None) -> int:
        stmt = delete(SentListing).where(SentListing.user_id == user_id)
        if filter_id is not None:
            stmt = stmt.where(SentListing.filter_id == filter_id)
        result = await self.s.execute(stmt)
        await self.s.commit()
        return int(result.rowcount or 0)

    async def forget_listing(self, user_id: int, listing_id: str) -> bool:
        result = await self.s.execute(
            delete(SentListing).where(
                SentListing.user_id == user_id, SentListing.listing_id == listing_id
            )
        )
        await self.s.commit()
        return int(result.rowcount or 0) > 0

    # ------------------------------------------------------------ monitor runs
    async def log_run(
        self,
        *,
        user_id: int,
        filter_id: int | None,
        started_at: datetime,
        finished_at: datetime,
        fetched: int,
        passed_filters: int,
        sent: int,
        skipped_duplicates: int,
        status: str,
        error: str = "",
        source: str = "",
    ) -> MonitorRun:
        row = MonitorRun(
            user_id=user_id,
            filter_id=filter_id,
            started_at=started_at,
            finished_at=finished_at,
            fetched=fetched,
            passed_filters=passed_filters,
            sent=sent,
            skipped_duplicates=skipped_duplicates,
            status=status[:16],
            error=(error or "")[:900],
            source=source[:24],
        )
        self.s.add(row)
        await self.s.commit()
        return _aware(row)

    async def last_runs(self, user_id: int, limit: int = 5) -> list[MonitorRun]:
        stmt = (
            select(MonitorRun)
            .where(MonitorRun.user_id == user_id)
            .order_by(MonitorRun.started_at.desc())
            .limit(limit)
        )
        rows = await self.s.execute(stmt)
        return [_aware(row) for row in rows.scalars().all()]

    async def save_source_status(self, status: SourceStatus) -> None:
        """Состояние источника храним как служебную запись пользователя 0."""
        row = await self.s.get(User, 0)
        payload = status.as_dict()
        if row is None:
            row = User(
                id=0,
                username="service:source_status",
                first_name=status.name,
                weights_json=dumps_json(payload),
                created_at=utcnow(),
                last_seen_at=status.checked_at or utcnow(),
                is_paused=False,
            )
            self.s.add(row)
        else:
            row.weights_json = dumps_json(payload)
            row.last_seen_at = status.checked_at or utcnow()
        await self.s.commit()

    async def load_source_status(self) -> dict[str, Any]:
        row = await self.s.get(User, 0)
        if row is None:
            return {}
        return loads_json(row.weights_json, {})


def row_to_listing(row: ListingRow) -> Listing:
    """Обратное преобразование: строка кэша -> доменный Listing."""
    return Listing(
        id=row.listing_id,
        source=row.source,
        collection_name=row.collection_name,
        title=row.title or row.collection_name,
        number=row.number,
        model_name=row.model_name,
        backdrop_name=row.backdrop_name,
        symbol_name=row.symbol_name,
        model_rarity_permille=row.model_rarity_permille,
        backdrop_rarity_permille=row.backdrop_rarity_permille,
        symbol_rarity_permille=row.symbol_rarity_permille,
        price_nano=int(row.price_nano or 0),
        price_without_fee_nano=row.price_without_fee_nano,
        currency=row.currency or "TON",
        floor_by_collection_nano=row.floor_by_collection_nano,
        floor_by_attributes_nano=row.floor_by_attributes_nano,
        backdrop_center_color=row.backdrop_center_color,
        backdrop_edge_color=row.backdrop_edge_color,
        backdrop_symbol_color=row.backdrop_symbol_color,
        backdrop_text_color=row.backdrop_text_color,
        image_url=row.image_url,
        url=row.url,
        listed_at=ensure_aware(row.listed_at),
        fetched_at=ensure_aware(row.last_seen_at) or utcnow(),
        is_active=bool(row.is_active),
        is_test_data=bool(row.is_test_data),
        extra=loads_json(row.extra_json, {}),
    )
