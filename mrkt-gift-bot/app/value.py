"""Оценка выгоды: сравнение цены лота с ориентиром по сопоставимым предложениям.

КАК СЧИТАЕТСЯ
-------------
Ориентир (benchmark) берётся по приоритету — первый доступный wins:

1. `exact_floor`       — флор по тем же атрибутам из ответа источника
                         (MRKT: `floorPriceNanoTONsByBackdropModel`).
2. `peers_exact`       — медиана цен активных лотов с теми же
                         коллекция+модель+фон+символ из нашего кэша.
3. `collection_floor`  — флор коллекции из ответа источника
                         (MRKT: `floorPriceNanoTONsByCollection`).
4. `peers_collection`  — медиана цен активных лотов той же коллекции.

Выгода:  diff = benchmark − price;  diff_percent = diff / benchmark * 100.
Положительный процент — лот дешевле ориентира.

СОПОСТАВИМОСТЬ И ЧЕСТНОСТЬ
--------------------------
* Сравниваются ОДИНАКОВЫЕ цены: брутто (сколько платит покупатель) с брутто.
  `salePriceWithoutFee` используется только чтобы показать комиссию площадки.
* В выборку аналогов попадают только лоты того же источника и той же природы
  данных (реальные не смешиваются с демонстрационными) — см. `select_peers`.
* Если аналогов меньше `min_comparables` и нет флора от источника, оценка
  помечается как ненадёжная (`confidence=low`) или не считается вовсе
  (`confidence=none`, `benchmark=None`).
* Это НЕ гарантия прибыли: ориентир описывает текущие asking-цены, а не
  фактические сделки и не ликвидность. Формулировка в карточке это подчёркивает.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable, Sequence

from app.domain import (
    Confidence,
    Listing,
    ValueAssessment,
    fmt_ton,
    utcnow,
)


@dataclass(slots=True)
class PeerSet:
    """Набор аналогов, по которым считается ориентир."""

    exact: list[Listing]
    collection: list[Listing]
    same_source: bool = True
    mixed_test_data: bool = False

    @property
    def total(self) -> int:
        return len(self.exact) + len(self.collection)


def select_peers(
    listing: Listing,
    candidates: Iterable[Listing],
    *,
    max_peer_age_days: int = 3,
    include_inactive: bool = False,
    max_exact: int = 200,
    max_collection: int = 400,
) -> PeerSet:
    """Отобрать сопоставимые активные предложения.

    Правила сопоставимости:
    * тот же источник и та же природа данных (test/real);
    * не сам лот (по id);
    * лот активен (если не разрешено обратное);
    * цена положительна;
    * `fetched_at` не старше `max_peer_age_days` (иначе аналог устарел);
    * «точные» аналоги — совпадают коллекция, модель, фон и символ;
    * «коллекционные» — совпадает коллекция, но атрибуты отличаются.
    """
    cutoff = utcnow() - timedelta(days=max(0, max_peer_age_days))
    exact: list[Listing] = []
    collection: list[Listing] = []
    mixed = False

    target_key = listing.attribute_key()
    target_collection = listing.collection_key()

    for other in candidates:
        if other.id == listing.id:
            continue
        if other.source != listing.source:
            mixed = True
            continue
        if bool(other.is_test_data) != bool(listing.is_test_data):
            mixed = True
            continue
        if not include_inactive and not other.is_active:
            continue
        if other.price_nano <= 0:
            continue
        if other.fetched_at and other.fetched_at < cutoff:
            continue
        if other.collection_key() != target_collection:
            continue
        if other.attribute_key() == target_key:
            if len(exact) < max_exact:
                exact.append(other)
        elif len(collection) < max_collection:
            collection.append(other)

    return PeerSet(exact=exact, collection=collection, same_source=not mixed, mixed_test_data=mixed)


def assess_value(
    listing: Listing,
    *,
    peers: PeerSet | None = None,
    min_comparables: int = 3,
) -> ValueAssessment:
    """Рассчитать выгоду лота относительно ориентира."""
    peers = peers or PeerSet(exact=[], collection=[], same_source=True)
    fee_permille = listing.implied_fee_permille()

    assessment = ValueAssessment(
        price_nano=listing.price_nano,
        min_comparables=min_comparables,
        fee_permille=fee_permille,
    )

    if listing.price_nano <= 0:
        assessment.note = "цена лота неизвестна"
        assessment.confidence = Confidence.NONE
        return assessment

    # --- 1. Флор по тем же атрибутам (данные источника) -------------------
    if listing.floor_by_attributes_nano and listing.floor_by_attributes_nano > 0:
        return _finalize(
            assessment,
            benchmark=listing.floor_by_attributes_nano,
            source="exact_floor",
            comparables=len(peers.exact),
            peers=peers,
            note_parts=["ориентир от источника: флор по тем же модели и фону"],
        )

    # --- 2. Медиана точных аналогов из кэша -------------------------------
    if len(peers.exact) >= min_comparables:
        benchmark = int(round(median([p.price_nano for p in peers.exact])))
        confidence = Confidence.HIGH if len(peers.exact) >= min_comparables * 2 else Confidence.MEDIUM
        return _finalize(
            assessment,
            benchmark=benchmark,
            source="peers_exact",
            comparables=len(peers.exact),
            peers=peers,
            confidence_override=confidence,
            note_parts=[f"медиана {len(peers.exact)} активных лотов с теми же атрибутами"],
        )

    # --- 3. Флор коллекции (данные источника) -----------------------------
    if listing.floor_by_collection_nano and listing.floor_by_collection_nano > 0:
        comparables = len(peers.exact) + len(peers.collection)
        # Флор коллекции — ориентир грубый: он не учитывает редкость атрибутов.
        confidence = Confidence.MEDIUM if comparables >= min_comparables else Confidence.LOW
        return _finalize(
            assessment,
            benchmark=listing.floor_by_collection_nano,
            source="collection_floor",
            comparables=comparables,
            peers=peers,
            confidence_override=confidence,
            note_parts=[
                "ориентир от источника: флор коллекции",
                "не учитывает редкость модели/фона/символа",
            ],
        )

    # --- 4. Медиана лотов коллекции ---------------------------------------
    if len(peers.collection) >= max(min_comparables, 5):
        benchmark = int(round(median([p.price_nano for p in peers.collection])))
        return _finalize(
            assessment,
            benchmark=benchmark,
            source="peers_collection",
            comparables=len(peers.collection),
            peers=peers,
            confidence_override=Confidence.LOW,
            note_parts=[
                f"медиана {len(peers.collection)} лотов той же коллекции",
                "атрибуты не совпадают — ориентир приблизительный",
            ],
        )

    # --- Ориентир определить не удалось ------------------------------------
    comparables = len(peers.exact) + len(peers.collection)
    assessment.benchmark_nano = None
    assessment.confidence = Confidence.NONE
    assessment.benchmark_source = "none"
    assessment.comparables = comparables
    if comparables == 0:
        assessment.note = (
            f"аналогов не найдено (нужно ≥ {min_comparables}). Выгода не рассчитана."
        )
    else:
        assessment.note = (
            f"аналогов слишком мало: {comparables} из {min_comparables} нужных. "
            "Оценка выгоды не рассчитана."
        )
    return assessment


def _finalize(
    assessment: ValueAssessment,
    *,
    benchmark: int,
    source: str,
    comparables: int,
    peers: PeerSet,
    confidence_override: Confidence | None = None,
    note_parts: Sequence[str] = (),
) -> ValueAssessment:
    diff = benchmark - assessment.price_nano
    diff_percent = (diff / benchmark * 100.0) if benchmark else None

    assessment.benchmark_nano = benchmark
    assessment.benchmark_source = source
    assessment.diff_nano = diff
    assessment.diff_percent = round(diff_percent, 2) if diff_percent is not None else None
    assessment.comparables = comparables

    if confidence_override is not None:
        assessment.confidence = confidence_override
    else:
        assessment.confidence = Confidence.HIGH if comparables >= assessment.min_comparables else Confidence.LOW

    notes = list(note_parts)
    if comparables < assessment.min_comparables:
        notes.append(
            f"аналогов {comparables} < {assessment.min_comparables} — оценка ненадёжна"
        )
    if peers.mixed_test_data:
        notes.append("часть данных пропущена: смешение источников/тестовых данных")
    if not peers.same_source:
        notes.append("в выборку попали лоты других источников — они исключены")
    notes.append("не гарантия прибыли: сравниваются текущие цены предложения")
    assessment.note = "; ".join(notes)
    return assessment


def describe(assessment: ValueAssessment) -> str:
    """Короткое человекочитаемое описание для карточки."""
    if assessment.benchmark_nano is None:
        return assessment.note or "ориентир цены не определён"
    sign = "−" if (assessment.diff_percent or 0) >= 0 else "+"
    percent = abs(assessment.diff_percent or 0.0)
    return (
        f"{fmt_ton(assessment.price_nano)} против ориентира "
        f"{fmt_ton(assessment.benchmark_nano)} ({assessment.describe_benchmark()}); "
        f"разница {sign}{percent:.1f} %, аналогов: {assessment.comparables}"
    )


def is_interesting(assessment: ValueAssessment, min_percent: float) -> bool:
    """Прошёл ли лот порог минимальной выгоды (жёсткий фильтр пользователя)."""
    if assessment.diff_percent is None:
        return False
    return assessment.diff_percent >= min_percent


__all__ = ["PeerSet", "assess_value", "describe", "is_interesting", "select_peers"]
