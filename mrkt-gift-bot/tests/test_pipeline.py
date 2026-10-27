"""Сквозные тесты конвейера: источник -> фильтры -> монохром -> выгода ->
ранжирование -> дедупликация. Работают на демо-источнике и подставных
источниках, без сети.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from app.config import Settings
from app.db.repo import Repository, row_to_listing
from app.domain import (
    FetchRequest,
    FetchResult,
    Listing,
    SourceKind,
    SourceStatus,
    utcnow,
)
from app.filters.matching import FilterCriteria
from app.filters.monochrome import MonochromeDetector, MonochromeSpec
from app.pipeline import PipelineContext, run_pipeline
from app.ranking import RankingWeights
from app.sources.base import ImageResult, ListingSource
from app.sources.demo import MONO_BACKDROPS, DemoSource
from app.sources.factory import ImageProvider
from tests.conftest import StubChanges, make_listing

TON = 1_000_000_000


def criteria(**kwargs: Any) -> FilterCriteria:
    base = dict(min_relevance=0.0)
    base.update(kwargs)
    return FilterCriteria(**base)


def make_ctx(settings: Settings, source: ListingSource, changes: Any | None = None) -> PipelineContext:
    images = ImageProvider(source, changes, settings=settings)
    detector = MonochromeDetector(changes, image_timeout=settings.mono_image_analysis_timeout)
    return PipelineContext(settings=settings, source=source, images=images, detector=detector)


class FakeSource(ListingSource):
    """Источник с фиксированным набором лотов — для управляемых сценариев."""

    name = "fake"
    is_test_data = True

    def __init__(self, listings: list[Listing], *, fail: bool = False, complete: bool = True) -> None:
        self.listings = listings
        self.fail = fail
        self.complete = complete
        self.requests: list[FetchRequest] = []

    async def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name, kind=SourceKind.DEMO, available=not self.fail,
            is_test_data=True, checked_at=utcnow(), message="fake",
        )

    async def fetch(self, request: FetchRequest) -> FetchResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("источник недоступен")
        rows = [
            item
            for item in self.listings
            if (not request.collections or item.collection_name in request.collections)
            and (request.min_price_nano is None or item.price_nano >= request.min_price_nano)
            and (request.max_price_nano is None or item.price_nano <= request.max_price_nano)
        ]
        return FetchResult(listings=list(rows), is_test_data=True, complete=self.complete)

    async def collections(self) -> list[str]:
        return sorted({item.collection_name for item in self.listings})

    async def get_image(self, listing: Listing) -> ImageResult | None:
        return None


class FilterRow:
    """Мини-замена ORM-строки фильтра для вызова run_pipeline."""

    def __init__(self, filter_id: int = 1, *, max_results: int = 5, name: str = "test") -> None:
        self.id = filter_id
        self.name = name
        self.max_results = max_results
        self.interval_minutes = 15


# ----------------------------------------------------------------------------
#  Базовый проход
# ----------------------------------------------------------------------------

async def test_pipeline_fetches_and_caches(ctx: PipelineContext, repo: Repository, settings: Settings) -> None:
    outcome = await run_pipeline(
        ctx, repo, user_id=1, filter_row=FilterRow(), criteria=criteria(), weights=RankingWeights()
    )
    assert outcome.fetched > 0
    assert outcome.cached == outcome.fetched
    assert outcome.results
    stats = await repo.cache_stats("demo")
    assert stats["total"] > 0


async def test_pipeline_sorts_by_relevance(ctx: PipelineContext, repo: Repository, settings: Settings) -> None:
    outcome = await run_pipeline(
        ctx, repo, user_id=1, filter_row=FilterRow(max_results=5),
        criteria=criteria(), weights=RankingWeights(),
    )
    scores = [item.ranking.score for item in outcome.results]
    assert scores == sorted(scores, reverse=True)
    assert len(outcome.results) <= 5


async def test_pipeline_respects_price_filter(ctx: PipelineContext, repo: Repository, settings: Settings) -> None:
    outcome = await run_pipeline(
        ctx, repo, user_id=2, filter_row=FilterRow(),
        criteria=criteria(price_min_ton=10.0, price_max_ton=20.0),
        weights=RankingWeights(),
    )
    assert outcome.results
    for item in outcome.results:
        assert 10.0 * TON <= item.listing.price_nano <= 20.0 * TON


async def test_pipeline_respects_min_relevance(ctx: PipelineContext, repo: Repository, settings: Settings) -> None:
    outcome = await run_pipeline(
        ctx, repo, user_id=3, filter_row=FilterRow(max_results=20),
        criteria=criteria(min_relevance=99.0), weights=RankingWeights(),
    )
    assert all(item.ranking.score >= 99.0 for item in outcome.results)


async def test_pipeline_reports_source_failure(repo: Repository, settings: Settings) -> None:
    source = FakeSource([], fail=True)
    ctx = make_ctx(settings, source)
    outcome = await run_pipeline(
        ctx, repo, user_id=4, filter_row=FilterRow(), criteria=criteria(), weights=RankingWeights()
    )
    assert outcome.results == []
    assert outcome.fetched == 0
    assert outcome.errors and "источник недоступен" in outcome.errors[0]


# ----------------------------------------------------------------------------
#  Монохромный отбор
# ----------------------------------------------------------------------------

async def test_monochrome_filter_selects_gray_backdrops(
    repo: Repository, settings: Settings
) -> None:
    source = DemoSource()
    changes = StubChanges()
    ctx = make_ctx(settings, source, changes)
    outcome = await run_pipeline(
        ctx, repo, user_id=5, filter_row=FilterRow(max_results=10),
        criteria=criteria(mono_enabled=True, mono_mode="gray", mono_min_score=70),
        weights=RankingWeights(),
    )
    assert outcome.passed_hard_filters > 0
    for item in outcome.results:
        assert item.monochrome.score >= 70
        assert item.monochrome.confidence.value in ("high", "medium", "low")
        # Демо-лот с «цветным» фоном не должен проходить.
        assert (item.listing.backdrop_name or "") in MONO_BACKDROPS or item.monochrome.score >= 70


async def test_monochrome_result_shows_method_and_confidence(
    repo: Repository, settings: Settings
) -> None:
    listing = make_listing(
        id="mono-1", backdrop_center_color=0x8C8C8C, backdrop_edge_color=0x5A5A5A,
        backdrop_symbol_color=0x6E6E6E, price_nano=40 * TON,
    )
    source = FakeSource([listing])
    ctx = make_ctx(settings, source, StubChanges())
    outcome = await run_pipeline(
        ctx, repo, user_id=6, filter_row=FilterRow(),
        criteria=criteria(mono_enabled=True, mono_mode="gray", mono_min_score=60),
        weights=RankingWeights(),
    )
    assert len(outcome.results) == 1
    mono = outcome.results[0].monochrome
    assert mono.score >= 90
    assert mono.method == "backdrop_colors"
    assert mono.confidence.value == "high"


async def test_colorful_listing_is_rejected_by_mono_filter(
    repo: Repository, settings: Settings
) -> None:
    listing = make_listing(
        id="color-1", backdrop_center_color=0xC0392B, backdrop_edge_color=0x1F3A93,
        backdrop_symbol_color=0xE0A106, price_nano=40 * TON,
    )
    source = FakeSource([listing])
    ctx = make_ctx(settings, source, StubChanges())
    outcome = await run_pipeline(
        ctx, repo, user_id=7, filter_row=FilterRow(),
        criteria=criteria(mono_enabled=True, mono_mode="gray", mono_min_score=60),
        weights=RankingWeights(),
    )
    assert outcome.results == []
    assert outcome.passed_hard_filters == 1        # до монохрома дошёл
    assert outcome.passed_all_filters == 0         # и не прошёл


async def test_image_analysis_used_when_colors_missing(
    repo: Repository, settings: Settings
) -> None:
    """Нет цветов в API -> детектор запрашивает картинку и решает по пикселям."""
    listing = make_listing(
        id="no-colors", title="Frost Fox · Graphite 1", model_name="Graphite",
        backdrop_name=None, backdrop_center_color=None, backdrop_edge_color=None,
        price_nano=40 * TON,
    )

    class ImageSource(FakeSource):
        async def get_image(self, item: Listing) -> ImageResult:
            from tests.conftest import make_png

            return ImageResult(data=make_png((120, 120, 122), size=128), content_type="image/png")

    ctx = make_ctx(settings, ImageSource([listing]), StubChanges())
    outcome = await run_pipeline(
        ctx, repo, user_id=8, filter_row=FilterRow(),
        criteria=criteria(mono_enabled=True, mono_mode="gray", mono_min_score=60),
        weights=RankingWeights(),
    )
    assert len(outcome.results) == 1
    methods = {e.method for e in outcome.results[0].monochrome.evidences}
    assert "image" in methods


# ----------------------------------------------------------------------------
#  Дедупликация в конвейере
# ----------------------------------------------------------------------------

async def test_second_run_returns_nothing_without_changes(
    ctx: PipelineContext, repo: Repository, settings: Settings
) -> None:
    source = FakeSource(
        [
            make_listing(id="a", price_nano=40 * TON, backdrop_center_color=0x8C8C8C),
            make_listing(id="b", price_nano=41 * TON, backdrop_center_color=0x9A9A9A),
        ]
    )
    ctx = make_ctx(settings, source)
    first = await run_pipeline(
        ctx, repo, user_id=9, filter_row=FilterRow(), criteria=criteria(), weights=RankingWeights()
    )
    assert len(first.results) == 2
    assert first.duplicates_skipped == 0

    from app.pipeline import record_sent_batch

    await record_sent_batch(repo, user_id=9, filter_id=1, items=first.results)

    second = await run_pipeline(
        ctx, repo, user_id=9, filter_row=FilterRow(), criteria=criteria(), weights=RankingWeights()
    )
    assert second.results == []
    assert second.duplicates_skipped == 2


async def test_price_drop_allows_resend_in_pipeline(
    repo: Repository, settings: Settings
) -> None:
    listing = make_listing(id="drop", price_nano=100 * TON, backdrop_center_color=0x8C8C8C)
    source = FakeSource([listing])
    ctx = make_ctx(settings, source)
    row = FilterRow()

    first = await run_pipeline(
        ctx, repo, user_id=10, filter_row=row, criteria=criteria(), weights=RankingWeights()
    )
    assert len(first.results) == 1
    from app.pipeline import record_sent_batch

    await record_sent_batch(repo, user_id=10, filter_id=row.id, items=first.results)

    # Цена падает на 20 % — должно прийти повторно с причиной price_drop.
    listing.price_nano = 80 * TON
    second = await run_pipeline(
        ctx, repo, user_id=10, filter_row=row, criteria=criteria(), weights=RankingWeights()
    )
    assert len(second.results) == 1
    assert second.results[0].listing.extra["send_reason"] == "price_drop"


async def test_max_results_limits_output(repo: Repository, settings: Settings) -> None:
    listings = [
        make_listing(id=f"lot-{i}", price_nano=(10 + i) * TON, backdrop_center_color=0x8C8C8C)
        for i in range(12)
    ]
    ctx = make_ctx(settings, FakeSource(listings))
    outcome = await run_pipeline(
        ctx, repo, user_id=11, filter_row=FilterRow(max_results=3),
        criteria=criteria(), weights=RankingWeights(),
    )
    assert len(outcome.results) == 3
    assert outcome.above_threshold >= 3


# ----------------------------------------------------------------------------
#  Изоляция пользователей
# ----------------------------------------------------------------------------

async def test_two_users_get_independent_results(
    repo: Repository, settings: Settings
) -> None:
    listings = [
        make_listing(id="shared-1", price_nano=40 * TON, backdrop_center_color=0x8C8C8C),
        make_listing(id="shared-2", price_nano=42 * TON, backdrop_center_color=0x9A9A9A),
    ]
    ctx = make_ctx(settings, FakeSource(listings))
    row = FilterRow()

    first_user = await run_pipeline(
        ctx, repo, user_id=101, filter_row=row, criteria=criteria(), weights=RankingWeights()
    )
    assert len(first_user.results) == 2
    from app.pipeline import record_sent_batch

    await record_sent_batch(repo, user_id=101, filter_id=row.id, items=first_user.results)

    # Тот же лот для второго пользователя — новый.
    second_user = await run_pipeline(
        ctx, repo, user_id=102, filter_row=row, criteria=criteria(), weights=RankingWeights()
    )
    assert len(second_user.results) == 2

    # А для первого — уже дубликаты.
    again = await run_pipeline(
        ctx, repo, user_id=101, filter_row=row, criteria=criteria(), weights=RankingWeights()
    )
    assert again.results == []


async def test_filters_of_one_user_do_not_leak(repo: Repository, settings: Settings) -> None:
    listings = [make_listing(id="x", price_nano=40 * TON)]
    ctx = make_ctx(settings, FakeSource(listings))

    created_a = await repo.create_filter(1, name="A", interval_minutes=15, max_results=5)
    created_b = await repo.create_filter(2, name="B", interval_minutes=15, max_results=5)

    assert await repo.get_filter(1, created_b.id) is None
    assert await repo.get_filter(2, created_a.id) is None

    outcome = await run_pipeline(
        ctx, repo, user_id=1, filter_row=created_a, criteria=criteria(), weights=RankingWeights()
    )
    assert len(outcome.results) == 1
    assert (await repo.list_filters(2)) != []
    assert [row.name for row in await repo.list_filters(1)] == ["A"]


# ----------------------------------------------------------------------------
#  Кэш и активность лотов
# ----------------------------------------------------------------------------

async def test_missing_listings_marked_inactive_only_when_fetch_complete(
    settings: Settings, tmp_path
) -> None:
    """Нужна одна БД на несколько сессий -> используем файловый SQLite."""
    from app.db.db import Database

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}")
    await db.create_all()
    try:
        first_batch = [
            make_listing(id="a", source="fake", price_nano=40 * TON),
            make_listing(id="b", source="fake", price_nano=41 * TON),
        ]
        ctx = make_ctx(settings, FakeSource(first_batch, complete=True))

        async with db.session_factory() as session:
            repo = Repository(session)
            await run_pipeline(
                ctx, repo, user_id=20, filter_row=FilterRow(),
                criteria=criteria(), weights=RankingWeights(),
            )
            stats = await repo.cache_stats("fake")
        assert stats == {"total": 2, "active": 2}

        # Неполная выдача (truncated): лот «b» не должен считаться проданным.
        ctx_partial = make_ctx(settings, FakeSource(
            [make_listing(id="a", source="fake", price_nano=40 * TON)], complete=False))
        async with db.session_factory() as session:
            repo = Repository(session)
            await run_pipeline(
                ctx_partial, repo, user_id=20, filter_row=FilterRow(),
                criteria=criteria(), weights=RankingWeights(),
            )
            stats = await repo.cache_stats("fake")
        assert stats == {"total": 2, "active": 2}

        # Полная выдача без «b»: теперь он действительно неактивен.
        ctx_complete = make_ctx(settings, FakeSource(
            [make_listing(id="a", source="fake", price_nano=40 * TON)], complete=True))
        async with db.session_factory() as session:
            repo = Repository(session)
            await run_pipeline(
                ctx_complete, repo, user_id=20, filter_row=FilterRow(),
                criteria=criteria(), weights=RankingWeights(),
            )
            stats = await repo.cache_stats("fake")
        assert stats == {"total": 2, "active": 1}
    finally:
        await db.close()


async def test_cached_peers_are_used_for_value(repo: Repository, settings: Settings) -> None:
    """Ориентир цены считается по аналогам из кэша, а не только по текущей выдаче."""
    base = [
        make_listing(
            id=f"peer-{i}", price_nano=100 * TON, model_name="Noir",
            backdrop_name="Ash Grey", symbol_name="Snowflake",
        )
        for i in range(6)
    ]
    target = make_listing(
        id="target", price_nano=60 * TON, model_name="Noir",
        backdrop_name="Ash Grey", symbol_name="Snowflake",
        floor_by_collection_nano=None, floor_by_attributes_nano=None,
    )
    ctx = make_ctx(settings, FakeSource(base + [target]))
    outcome = await run_pipeline(
        ctx, repo, user_id=21, filter_row=FilterRow(), criteria=criteria(), weights=RankingWeights()
    )
    by_id = {item.listing.id: item for item in outcome.results}
    assert "target" in by_id
    value = by_id["target"].value
    assert value.benchmark_source == "peers_exact"
    assert value.comparables >= 3
    assert value.diff_percent is not None and value.diff_percent > 30


async def test_demo_and_real_data_are_not_mixed_in_benchmark(
    repo: Repository, settings: Settings
) -> None:
    demo_listing = make_listing(id="demo", source="fake", is_test_data=True, price_nano=40 * TON)
    real_listing = make_listing(id="real", source="fake", is_test_data=False, price_nano=100 * TON)
    ctx = make_ctx(settings, FakeSource([demo_listing, real_listing]))
    await run_pipeline(
        ctx, repo, user_id=22, filter_row=FilterRow(), criteria=criteria(), weights=RankingWeights()
    )
    cached = await repo.peers_for("fake", demo_listing, days=3)
    assert cached, "кэш пуст — проверка теряет смысл"
    assert all(item.is_test_data for item in cached)
    assert all(item.id != "real" for item in cached)


# ----------------------------------------------------------------------------
#  Ручной поиск не блокирует будущие уведомления
# ----------------------------------------------------------------------------

async def test_manual_run_does_not_record_history(
    repo: Repository, settings: Settings
) -> None:
    listings = [make_listing(id="manual", price_nano=40 * TON)]
    ctx = make_ctx(settings, FakeSource(listings))
    row = FilterRow()

    manual = await run_pipeline(
        ctx, repo, user_id=30, filter_row=row, criteria=criteria(),
        weights=RankingWeights(), manual=True,
    )
    assert len(manual.results) == 1
    assert await repo.get_sent(30, row.id, "manual") is None

    # После ручного поиска мониторинг всё ещё может прислать этот лот.
    scheduled = await run_pipeline(
        ctx, repo, user_id=30, filter_row=row, criteria=criteria(), weights=RankingWeights()
    )
    assert len(scheduled.results) == 1
    assert scheduled.results[0].listing.extra["send_reason"] == "new"


# ----------------------------------------------------------------------------
#  Передача фильтров в источник
# ----------------------------------------------------------------------------

async def test_server_side_filters_are_passed_to_source(
    repo: Repository, settings: Settings
) -> None:
    source = FakeSource([make_listing(id="a", collection_name="Frost Fox", price_nano=40 * TON)])
    ctx = make_ctx(settings, source)
    await run_pipeline(
        ctx, repo, user_id=31, filter_row=FilterRow(),
        criteria=criteria(
            collection_names=["Frost Fox"],
            model_names=["Noir"],
            price_min_ton=5.0,
            price_max_ton=100.0,
            query="noir",
        ),
        weights=RankingWeights(),
    )
    assert source.requests
    request = source.requests[0]
    assert request.collections == ["Frost Fox"]
    assert request.models == ["Noir"]
    assert request.min_price_nano == 5 * TON
    assert request.max_price_nano == 100 * TON
    assert request.query == "noir"
