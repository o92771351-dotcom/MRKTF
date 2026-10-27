"""Тесты исключения дубликатов и изоляции пользователей."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import Settings
from app.db.repo import Repository
from app.domain import (
    Confidence,
    MatchResult,
    MonochromeAssessment,
    RankingResult,
    ScoredListing,
    ValueAssessment,
    utcnow,
)
from app.pipeline import DecisionReason, decide_send, record_sent_batch
from tests.conftest import make_listing

TON = 1_000_000_000


def scored(price_ton: float, listing_id: str = "lot-1", **kwargs) -> ScoredListing:
    listing = make_listing(id=listing_id, price_nano=int(price_ton * TON), **kwargs)
    return ScoredListing(
        listing=listing,
        match=MatchResult(passed=True, match_score=1.0, total_criteria=1),
        monochrome=MonochromeAssessment(score=90.0, method="backdrop_colors",
                                        confidence=Confidence.HIGH),
        value=ValueAssessment(price_nano=listing.price_nano, benchmark_nano=50 * TON,
                              diff_nano=50 * TON - listing.price_nano, diff_percent=20.0,
                              comparables=8, benchmark_source="peers_exact",
                              confidence=Confidence.HIGH),
        ranking=RankingResult(score=88.0),
    )


async def mark_sent(repo: Repository, user_id: int, filter_id: int, item: ScoredListing,
                    reason: str = "new") -> None:
    await record_sent_batch(
        repo, user_id=user_id, filter_id=filter_id, items=[item]
    )
    item.listing.extra["send_reason"] = reason


# ----------------------------------------------------------------------------
#  Базовые правила
# ----------------------------------------------------------------------------

async def test_new_listing_is_sent(repo: Repository, settings: Settings) -> None:
    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(40.0), settings=settings
    )
    assert decision.should_send is True
    assert decision.reason == DecisionReason.NEW


async def test_same_unchanged_listing_is_not_sent_again(
    repo: Repository, settings: Settings
) -> None:
    item = scored(40.0)
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[item])

    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(40.0), settings=settings
    )
    assert decision.should_send is False
    assert decision.reason == DecisionReason.DUPLICATE


async def test_small_price_change_does_not_trigger_resend(
    repo: Repository, settings: Settings
) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])

    # 2 % снижения при пороге 5 % — молчим.
    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(39.2), settings=settings
    )
    assert decision.should_send is False


async def test_significant_price_drop_triggers_resend(
    repo: Repository, settings: Settings
) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])

    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(36.0), settings=settings
    )
    assert decision.should_send is True
    assert decision.reason == DecisionReason.PRICE_DROP
    assert decision.drop_percent == pytest.approx(10.0, abs=0.01)
    assert "10.0" in decision.detail


async def test_price_increase_does_not_trigger_resend(
    repo: Repository, settings: Settings
) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])
    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(48.0), settings=settings
    )
    assert decision.should_send is False


async def test_price_drop_threshold_is_configurable(settings: Settings) -> None:
    strict = Settings(**{**settings.model_dump(), "resend_price_drop_percent": 20.0})
    db_url = strict.database_url
    from app.db.db import Database

    database = Database(db_url)
    await database.create_all()
    try:
        async with database.session_factory() as session:
            repo = Repository(session)
            await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])
            decision = await decide_send(
                repo, user_id=1, filter_id=1, scored=scored(36.0), settings=strict
            )
            assert decision.should_send is False     # 10 % < порога 20 %
    finally:
        await database.close()


async def test_returned_listing_is_sent_again(repo: Repository, settings: Settings) -> None:
    item = scored(40.0)
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[item])

    # Имитируем: лот пропал и вернулся через 30 часов.
    row = await repo.get_sent(1, 1, "lot-1")
    assert row is not None
    row.last_seen_at = utcnow() - timedelta(hours=30)
    await repo.s.commit()

    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(40.0), settings=settings
    )
    assert decision.should_send is True
    assert decision.reason == DecisionReason.RETURNED


async def test_attribute_change_triggers_resend(repo: Repository, settings: Settings) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])
    changed = scored(40.0, backdrop_name="Snow White", symbol_name="Anchor")
    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=changed, settings=settings
    )
    assert decision.should_send is True
    assert decision.reason == DecisionReason.CHANGED


async def test_resend_counter_and_min_price_are_tracked(
    repo: Repository, settings: Settings
) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])
    row = await repo.get_sent(1, 1, "lot-1")
    assert row is not None and row.resend_count == 0

    existing = await repo.get_sent(1, 1, "lot-1")
    await repo.record_sent(
        user_id=1, filter_id=1, listing=scored(36.0).listing, relevance=90.0,
        monochrome_score=90.0, monochrome_method="backdrop_colors",
        monochrome_confidence="high", diff_percent=25.0, reason="price_drop",
        existing=existing,
    )
    row = await repo.get_sent(1, 1, "lot-1")
    assert row is not None
    assert row.resend_count == 1
    assert row.price_nano == 36 * TON
    assert row.min_price_nano == 36 * TON


# ----------------------------------------------------------------------------
#  Изоляция пользователей и фильтров
# ----------------------------------------------------------------------------

async def test_users_do_not_share_send_history(repo: Repository, settings: Settings) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])

    # Другой пользователь видит тот же лот впервые.
    other = await decide_send(
        repo, user_id=2, filter_id=1, scored=scored(40.0), settings=settings
    )
    assert other.should_send is True
    assert other.reason == DecisionReason.NEW


async def test_same_user_different_filter_sends_again(
    repo: Repository, settings: Settings
) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])
    decision = await decide_send(
        repo, user_id=1, filter_id=2, scored=scored(40.0), settings=settings
    )
    assert decision.should_send is True


async def test_history_is_scoped_to_user(repo: Repository) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0, listing_id="a")])
    await record_sent_batch(repo, user_id=2, filter_id=1, items=[scored(41.0, listing_id="b")])

    first = await repo.history(1)
    second = await repo.history(2)
    assert [row.listing_id for row in first] == ["a"]
    assert [row.listing_id for row in second] == ["b"]


async def test_clear_history_allows_resend(repo: Repository, settings: Settings) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[scored(40.0)])
    assert (await decide_send(repo, user_id=1, filter_id=1, scored=scored(40.0),
                              settings=settings)).should_send is False

    removed = await repo.clear_history(1, 1)
    assert removed == 1
    decision = await decide_send(
        repo, user_id=1, filter_id=1, scored=scored(40.0), settings=settings
    )
    assert decision.should_send is True


async def test_forget_single_listing(repo: Repository, settings: Settings) -> None:
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[
        scored(40.0, listing_id="a"), scored(41.0, listing_id="b"),
    ])
    assert await repo.forget_listing(1, "a") is True
    assert await repo.forget_listing(1, "a") is False

    assert (await decide_send(repo, user_id=1, filter_id=1, scored=scored(40.0, listing_id="a"),
                              settings=settings)).should_send is True
    assert (await decide_send(repo, user_id=1, filter_id=1, scored=scored(41.0, listing_id="b"),
                              settings=settings)).should_send is False


async def test_deleting_filter_removes_its_history(repo: Repository, settings: Settings) -> None:
    row = await repo.create_filter(1, name="Тест", interval_minutes=15, max_results=5)
    await record_sent_batch(repo, user_id=1, filter_id=row.id, items=[scored(40.0)])
    assert await repo.history(1)

    assert await repo.delete_filter(1, row.id) is True
    assert await repo.history(1) == []


async def test_touch_sent_does_not_enable_resend(repo: Repository, settings: Settings) -> None:
    item = scored(40.0)
    await record_sent_batch(repo, user_id=1, filter_id=1, items=[item])

    # Первое решение — дубль, при этом наблюдение обновляется.
    first = await decide_send(repo, user_id=1, filter_id=1, scored=scored(40.0), settings=settings)
    assert first.should_send is False
    row = await repo.get_sent(1, 1, "lot-1")
    assert row is not None
    assert (utcnow() - row.last_seen_at).total_seconds() < 60

    second = await decide_send(repo, user_id=1, filter_id=1, scored=scored(40.0), settings=settings)
    assert second.should_send is False
