"""Конвейер оценки: источник -> фильтры -> монохром -> выгода -> ранжирование -> дедуп.

Порядок шагов принципиален:

1. Запрос к источнику (серверные фильтры: коллекции, модель/фон/символ, цена).
2. Сохранение увиденного в кэш — это база аналогов для оценки выгоды.
3. Жёсткие фильтры БЕЗ монохрома (дешёвые: коллекция, цена, атрибуты).
4. Детектор монохромности — только для прошедших шаг 3 (экономим трафик:
   картинка качается лениво и только если цветов из API не хватило).
5. Полный `matching.apply` уже с результатом монохрома.
6. Оценка выгоды по аналогам из кэша + ориентирам источника.
7. Ранжирование 0..100 и порог `min_relevance`.
8. Дедупликация: не отправляем тот же лот повторно, но разрешаем повтор при
   существенном снижении цены или возвращении лота спустя N часов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from app.config import Settings
from app.db.repo import Repository, row_to_listing
from app.domain import (
    Confidence,
    FetchRequest,
    Listing,
    MatchResult,
    MonochromeAssessment,
    ScoredListing,
    ValueAssessment,
    utcnow,
)
from app.filters.matching import FilterCriteria, apply
from app.filters.monochrome import MonochromeDetector, MonochromeSpec, passes as mono_passes
from app.ranking import RankingWeights, rank
from app.sources.base import ListingSource
from app.sources.factory import ImageProvider
from app.value import PeerSet, assess_value, select_peers

logger = logging.getLogger(__name__)


class DecisionReason:
    NEW = "new"
    PRICE_DROP = "price_drop"
    RETURNED = "returned"
    CHANGED = "changed"
    DUPLICATE = "duplicate"


@dataclass(slots=True)
class DedupDecision:
    should_send: bool
    reason: str
    detail: str = ""
    drop_percent: float | None = None


@dataclass(slots=True)
class PipelineOutcome:
    """Итог одного цикла: что нашли и что решили отправить."""

    fetched: int = 0
    cached: int = 0
    passed_hard_filters: int = 0
    passed_all_filters: int = 0
    above_threshold: int = 0
    duplicates_skipped: int = 0
    results: list[ScoredListing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    is_test_data: bool = False
    truncated: bool = False

    def summary(self) -> str:
        return (
            f"получено {self.fetched}, кэш {self.cached}, прошли фильтры "
            f"{self.passed_all_filters}, выше порога {self.above_threshold}, "
            f"к отправке {len(self.results)}, дублей пропущено {self.duplicates_skipped}"
        )


class PipelineContext:
    """Всё, что нужно конвейеру. Собирается один раз при старте приложения."""

    def __init__(
        self,
        *,
        settings: Settings,
        source: ListingSource,
        images: ImageProvider,
        detector: MonochromeDetector,
    ) -> None:
        self.settings = settings
        self.source = source
        self.images = images
        self.detector = detector

    async def rarities_for(self, collection: str) -> dict[str, dict[str, float]]:
        """Редкости атрибутов из публичного API @GiftChanges (если доступен)."""
        changes = getattr(self.images, "_changes", None)
        if changes is None or not collection:
            return {}
        try:
            return await changes.rarities(collection)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось получить редкости для %s: %r", collection, exc)
            return {}


@dataclass(slots=True)
class Candidate:
    """Промежуточное состояние лота внутри конвейера."""

    listing: Listing
    match: MatchResult | None = None
    monochrome: MonochromeAssessment | None = None
    value: ValueAssessment | None = None


async def run_pipeline(
    ctx: PipelineContext,
    repo: Repository,
    *,
    user_id: int,
    filter_row: Any | None,
    criteria: FilterCriteria,
    weights: RankingWeights,
    manual: bool = False,
) -> PipelineOutcome:
    """Один цикл мониторинга (или ручной /search).

    Решение о повторной отправке принимает `decide_send` по истории отправок.
    Сам конвейер историю НЕ пишет: запись делает вызывающий код через
    `record_sent_batch`. Ручной /search намеренно её не вызывает — иначе
    разовый поиск заблокировал бы будущие уведомления мониторинга.
    """
    outcome = PipelineOutcome(is_test_data=ctx.source.is_test_data)
    settings = ctx.settings

    request = FetchRequest(
        collections=list(criteria.collection_names),
        models=list(criteria.model_names),
        backdrops=list(criteria.backdrop_names),
        symbols=list(criteria.symbol_names),
        query=criteria.query or None,
        min_price_nano=criteria.price_min_nano,
        max_price_nano=criteria.price_max_nano,
        limit=max(20, int(filter_row.max_results if filter_row else 5) * 12),
        ordering="Price",
        low_to_high=True,
    )

    # --- 1. Запрос к источнику ---------------------------------------------
    try:
        fetch = await ctx.source.fetch(request)
    except Exception as exc:  # noqa: BLE001
        message = f"источник недоступен: {exc}"
        logger.warning("run_pipeline: %s", message)
        outcome.errors.append(message)
        outcome.finished_at = utcnow()
        return outcome

    outcome.fetched = len(fetch.listings)
    outcome.truncated = fetch.truncated
    outcome.errors.extend(fetch.errors)
    listings = fetch.listings

    # --- 2. Кэш (база аналогов) --------------------------------------------
    try:
        outcome.cached = await repo.upsert_listings(listings)
        # Помечать отсутствующие лоты неактивными можно ТОЛЬКО если получена
        # вся выдача: иначе лот, выпавший из первых страниц, будет ошибочно
        # считаться проданным и исчезнет из набора аналогов.
        if listings and fetch.complete and not fetch.errors:
            await repo.mark_missing_inactive(
                listings[0].source, [item.id for item in listings]
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось обновить кэш лотов")
        outcome.errors.append(f"ошибка кэша: {exc}")

    if not listings:
        outcome.finished_at = utcnow()
        return outcome

    # --- 3. Аналоги из кэша (один запрос на коллекцию) ----------------------
    peers_by_collection: dict[str, list[Listing]] = {}
    for listing in listings:
        key = listing.collection_key()
        if key in peers_by_collection:
            continue
        try:
            cached = await repo.peers_for(
                listing.source, listing, days=settings.value_peer_lookback_days
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("peers_for: %r", exc)
            cached = []
        merged = {item.id: item for item in cached}
        for fresh in listings:
            if fresh.collection_key() == key:
                merged[fresh.id] = fresh
        peers_by_collection[key] = list(merged.values())

    # Редкости атрибутов — для вклада «редкий атрибут = выше балл соответствия».
    rarity_cache: dict[str, dict[str, dict[str, float]]] = {}
    for listing in listings:
        name = listing.collection_name
        if name and name not in rarity_cache:
            rarity_cache[name] = await ctx.rarities_for(name)

    mono_spec = MonochromeSpec(
        enabled=criteria.mono_enabled,
        mode=criteria.mono_mode,
        max_saturation=criteria.mono_max_saturation,
        min_score=criteria.mono_min_score,
        custom_hue=criteria.mono_custom_hue,
        custom_hue_tolerance=criteria.mono_custom_hue_tolerance,
        require_confidence=criteria.mono_require_confidence,
        allow_image=criteria.mono_allow_image and settings.mono_image_enabled,
        allow_name_heuristic=criteria.mono_allow_name_heuristic and settings.mono_allow_name_heuristic,
    )

    # --- 4-7. Оценка каждого лота ------------------------------------------
    # Копия критериев без требования монохромности: сначала дешёвые жёсткие
    # фильтры, и только затем — дорогой детектор цвета.
    without_mono = dc_replace(criteria, mono_enabled=False)
    scored: list[ScoredListing] = []

    for listing in listings:
        criteria.rarity_maps = rarity_cache.get(listing.collection_name, {})
        without_mono.rarity_maps = criteria.rarity_maps

        pre_match = apply(listing, without_mono)
        if not pre_match.passed:
            continue
        outcome.passed_hard_filters += 1

        monochrome: MonochromeAssessment | None = None
        if mono_spec.enabled:
            monochrome = await ctx.detector.assess(
                listing,
                mono_spec,
                fetch_image=ctx.images.fetch,
            )
            ok, _reason = mono_passes(monochrome, mono_spec)
            full_match = apply(listing, criteria, monochrome_score=monochrome.score, monochrome_ok=ok)
        else:
            full_match = apply(listing, criteria)

        if not full_match.passed:
            continue
        outcome.passed_all_filters += 1

        peers = select_peers(
            listing,
            peers_by_collection.get(listing.collection_key(), []),
            max_peer_age_days=settings.value_peer_lookback_days,
        )
        value = assess_value(listing, peers=peers, min_comparables=settings.value_min_comparables)

        # Порог минимальной выгоды — жёсткое условие пользователя.
        if criteria.min_value_percent is not None:
            if value.diff_percent is None or value.diff_percent < criteria.min_value_percent:
                continue

        ranking = rank(
            listing,
            match=full_match,
            monochrome=monochrome,
            value=value,
            weights=weights,
            freshness_half_life_hours=settings.rank_freshness_half_life_hours,
        )
        if ranking.score < criteria.min_relevance:
            continue
        outcome.above_threshold += 1

        scored.append(
            ScoredListing(
                listing=listing,
                match=full_match,
                monochrome=monochrome
                or MonochromeAssessment(
                    score=0.0, method="none", confidence=Confidence.NONE
                ),
                value=value,
                ranking=ranking,
            )
        )

    scored.sort(key=lambda item: (item.ranking.score, -item.listing.price_nano), reverse=True)

    # --- 8. Дедупликация и лимит результатов --------------------------------
    limit = max(1, int(filter_row.max_results if filter_row else settings.max_results_per_run))
    kept: list[ScoredListing] = []
    for item in scored:
        decision = await decide_send(
            repo,
            user_id=user_id,
            filter_id=getattr(filter_row, "id", None),
            scored=item,
            settings=settings,
            manual=manual,
        )
        if decision.should_send:
            item.listing.extra["send_reason"] = decision.reason
            item.listing.extra["send_detail"] = decision.detail
            kept.append(item)
            if len(kept) >= limit:
                break
        else:
            outcome.duplicates_skipped += 1

    outcome.results = kept
    outcome.finished_at = utcnow()
    return outcome


async def decide_send(
    repo: Repository,
    *,
    user_id: int,
    filter_id: int | None,
    scored: ScoredListing,
    settings: Settings,
    manual: bool = False,
) -> DedupDecision:
    """Решить, отправлять ли лот: новый / снижение цены / возвращение / дубль."""
    listing = scored.listing
    existing = await repo.get_sent(user_id, filter_id, listing.id)

    if existing is None:
        return DedupDecision(True, DecisionReason.NEW, "лот ещё не отправлялся")

    previous_price = int(existing.price_nano or 0)
    current_price = int(listing.price_nano or 0)

    # 1) Существенное снижение цены.
    if previous_price > 0 and current_price > 0 and current_price < previous_price:
        drop_percent = (previous_price - current_price) / previous_price * 100.0
        threshold = float(settings.resend_price_drop_percent)
        if drop_percent >= threshold:
            return DedupDecision(
                True,
                DecisionReason.PRICE_DROP,
                f"цена снизилась на {drop_percent:.1f} % "
                f"(порог повтора {threshold:g} %)",
                drop_percent=round(drop_percent, 2),
            )

    # 2) Лот исчезал и вернулся.
    if existing.last_seen_at:
        gap = utcnow() - existing.last_seen_at
        if gap >= timedelta(hours=max(0, int(settings.resend_after_hours))):
            return DedupDecision(
                True,
                DecisionReason.RETURNED,
                f"лот снова в продаже спустя {gap.total_seconds() / 3600:.1f} ч",
            )

    # 3) Существенно изменились атрибуты (редкий случай: перепродажа с новым номером).
    if existing.signature and existing.signature != listing.signature():
        # Подпись меняется и при изменении цены — это уже обработано выше,
        # поэтому здесь смотрим только на атрибуты.
        if _attributes_changed(existing.signature, listing.signature()):
            return DedupDecision(True, DecisionReason.CHANGED, "изменились атрибуты лота")

    # Ничего не произошло — обновляем наблюдение и молчим.
    try:
        await repo.touch_sent(existing, listing)
    except Exception as exc:  # noqa: BLE001
        logger.debug("touch_sent: %r", exc)
    detail = "уже отправлялся ранее" if manual else "без изменений"
    return DedupDecision(False, DecisionReason.DUPLICATE, detail)


def _attributes_changed(old_signature: str, new_signature: str) -> bool:
    """Сравнить подписи без цены: id, коллекция, модель, фон, символ, номер."""
    def parts(signature: str) -> list[str]:
        chunks = signature.split(":")
        if len(chunks) < 6:
            return chunks
        return [chunks[0], chunks[1], chunks[2], chunks[3], chunks[4], chunks[6] if len(chunks) > 6 else ""]

    return parts(old_signature) != parts(new_signature)


async def record_sent_batch(
    repo: Repository,
    *,
    user_id: int,
    filter_id: int | None,
    items: Sequence[ScoredListing],
) -> int:
    """Записать факт отправки уведомлений (вызывается после успешной доставки)."""
    count = 0
    for item in items:
        existing = await repo.get_sent(user_id, filter_id, item.listing.id)
        reason = str(item.listing.extra.get("send_reason") or DecisionReason.NEW)
        try:
            await repo.record_sent(
                user_id=user_id,
                filter_id=filter_id,
                listing=item.listing,
                relevance=item.ranking.score,
                monochrome_score=item.monochrome.score if item.monochrome else 0.0,
                monochrome_method=item.monochrome.method if item.monochrome else "",
                monochrome_confidence=(
                    item.monochrome.confidence.value if item.monochrome else ""
                ),
                diff_percent=item.value.diff_percent if item.value else None,
                reason=reason,
                existing=existing,
            )
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось записать отправку лота %s: %r", item.listing.id, exc)
    return count
