"""Доменная модель: сущности, которыми обмениваются источник, фильтры,
оценщики и бот. Никакой зависимости от aiogram и SQLAlchemy.

Цены везде в nanoTON (1 TON = 1_000_000_000 nanoTON) — так их отдаёт MRKT.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

NANO = 1_000_000_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def nano_to_ton(nano: int | float | None) -> float:
    if nano is None:
        return 0.0
    return float(nano) / NANO


def ton_to_nano(ton: float | int | None) -> int | None:
    if ton is None:
        return None
    return int(round(float(ton) * NANO))


def fmt_ton(nano: int | float | None, digits: int = 2) -> str:
    return f"{nano_to_ton(nano):,.{digits}f}".replace(",", " ")


def int_to_rgb(value: int | None) -> tuple[int, int, int] | None:
    """0xRRGGBB -> (r, g, b)."""
    if value is None:
        return None
    try:
        v = int(value) & 0xFFFFFF
    except (TypeError, ValueError):
        return None
    return (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF


def ensure_aware(value: datetime | None) -> datetime | None:
    """Привести datetime к timezone-aware UTC.

    Обязательно для SQLite: драйвер aiosqlite возвращает наивные метки, а
    сравнения с `utcnow()` иначе падают с TypeError.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_iso_datetime(raw: Any) -> datetime | None:
    """Аккуратный парсинг дат из API (ISO-8601, иногда с 'Z' или без tz)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SourceKind(StrEnum):
    DEMO = "demo"
    MRKT = "mrkt"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


CONFIDENCE_SCORE: dict[str, float] = {
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.6,
    Confidence.LOW: 0.3,
    Confidence.NONE: 0.0,
}


@dataclass(slots=True)
class Listing:
    """Нормализованное предложение (лот) маркетплейса."""

    id: str
    source: str
    collection_name: str
    title: str = ""
    number: int | None = None

    model_name: str | None = None
    backdrop_name: str | None = None
    symbol_name: str | None = None

    model_rarity_permille: int | None = None
    backdrop_rarity_permille: int | None = None
    symbol_rarity_permille: int | None = None

    # Цена покупателя (брутто) и цена без комиссии площадки — обе в nanoTON.
    price_nano: int = 0
    price_without_fee_nano: int | None = None
    currency: str = "TON"

    # Ориентиры, которые может отдавать сам источник.
    floor_by_collection_nano: int | None = None
    floor_by_attributes_nano: int | None = None

    # Цвета фона в формате 0xRRGGBB (у MRKT это backdropColors*Color).
    backdrop_center_color: int | None = None
    backdrop_edge_color: int | None = None
    backdrop_symbol_color: int | None = None
    backdrop_text_color: int | None = None

    image_url: str | None = None
    url: str | None = None

    listed_at: datetime | None = None
    fetched_at: datetime = field(default_factory=utcnow)

    is_active: bool = True
    is_test_data: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ свойства
    @property
    def display_title(self) -> str:
        return self.title or self.model_name or self.collection_name

    @property
    def age_hours(self) -> float | None:
        if not self.listed_at:
            return None
        delta = (utcnow() - self.listed_at).total_seconds() / 3600.0
        return max(0.0, delta)

    def attribute_key(self) -> str:
        """Ключ «точной» группы аналогов: коллекция + модель + фон + символ."""
        return "|".join(
            str(x or "").strip().lower()
            for x in (self.collection_name, self.model_name, self.backdrop_name, self.symbol_name)
        )

    def collection_key(self) -> str:
        return str(self.collection_name or "").strip().lower()

    def implied_fee_permille(self) -> int | None:
        """Комиссия площадки в ‰, выведенная из пары salePrice / salePriceWithoutFee.

        Единица измерения полей `salesFee` в конфиге MRKT достоверно не известна,
        поэтому комиссия выводится из фактических данных лота, а не берётся на веру.
        """
        if not self.price_without_fee_nano or self.price_without_fee_nano <= 0:
            return None
        if self.price_nano <= self.price_without_fee_nano:
            return None
        ratio = self.price_nano / self.price_without_fee_nano - 1.0
        return int(round(ratio * 1000))

    def signature(self) -> str:
        """Подпись «существенных» полей: изменилась она — лот можно показать снова."""
        parts = [
            self.id,
            self.collection_name,
            self.model_name or "",
            self.backdrop_name or "",
            self.symbol_name or "",
            str(self.price_nano),
            str(self.number or ""),
        ]
        return ":".join(parts)


@dataclass(slots=True)
class MonochromeEvidence:
    """Одно свидетельство монохромности."""

    method: str                 # backdrop_colors | changes_api_colors | image | name
    score: float                # 0..1 — насколько это свидетельство «монохромное»
    confidence: Confidence
    details: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.method}={self.score:.2f}({self.confidence})"


@dataclass(slots=True)
class MonochromeAssessment:
    """Итоговая оценка монохромности лота."""

    score: float                            # 0..100
    method: str                             # каким способом получено решение
    confidence: Confidence
    evidences: list[MonochromeEvidence] = field(default_factory=list)
    palette: dict[str, Any] = field(default_factory=dict)

    @property
    def is_monochrome(self) -> bool:
        return self.score >= 50.0 and self.confidence != Confidence.NONE

    def describe_method(self) -> str:
        """Человекочитаемое описание способа (для карточки уведомления)."""
        names = {
            "backdrop_colors": "цвета фона из API MRKT",
            "changes_api_colors": "цвета фона из @GiftChanges",
            "image": "анализ изображения",
            "name": "эвристика по названию",
            "combined": "комбинация источников",
            "none": "данных нет",
        }
        return names.get(self.method, self.method)


@dataclass(slots=True)
class ValueAssessment:
    """Оценка выгоды: сравнение цены лота с ориентиром по аналогам."""

    price_nano: int
    benchmark_nano: int | None = None
    diff_nano: int | None = None
    diff_percent: float | None = None       # >0 — лот дешевле ориентира
    comparables: int = 0
    benchmark_source: str = "none"          # exact_floor | collection_floor | peers_exact | peers_collection | none
    confidence: Confidence = Confidence.NONE
    note: str = ""
    min_comparables: int = 3
    fee_permille: int | None = None

    @property
    def is_reliable(self) -> bool:
        return self.confidence in (Confidence.HIGH, Confidence.MEDIUM) and self.benchmark_nano is not None

    def describe_benchmark(self) -> str:
        names = {
            "exact_floor": "флор по тем же атрибутам (из API)",
            "collection_floor": "флор коллекции (из API)",
            "peers_exact": "медиана аналогов с теми же атрибутами",
            "peers_collection": "медиана лотов коллекции",
            "none": "ориентир не определён",
        }
        return names.get(self.benchmark_source, self.benchmark_source)


@dataclass(slots=True)
class MatchResult:
    """Результат проверки лота фильтром."""

    passed: bool
    hard_failures: list[str] = field(default_factory=list)
    match_score: float = 0.0                # 0..1
    matched_criteria: list[str] = field(default_factory=list)
    total_criteria: int = 0

    def describe(self) -> str:
        if not self.total_criteria:
            return "критериев нет"
        return f"{len(self.matched_criteria)}/{self.total_criteria}"


@dataclass(slots=True)
class RankingResult:
    """Итоговая релевантность 0..100 и её разложение."""

    score: float
    components: dict[str, float] = field(default_factory=dict)   # 0..1
    weights: dict[str, float] = field(default_factory=dict)
    used_weights: dict[str, float] = field(default_factory=dict)

    def breakdown(self) -> str:
        order = ["match", "monochrome", "value", "confidence", "freshness"]
        labels = {
            "match": "фильтры",
            "monochrome": "монохром",
            "value": "выгода",
            "confidence": "надёжность",
            "freshness": "свежесть",
        }
        chunks = []
        for key in order:
            if key not in self.components:
                continue
            chunks.append(f"{labels.get(key, key)} {self.components[key] * 100:.0f}")
        return " · ".join(chunks)


@dataclass(slots=True)
class ScoredListing:
    """Лот после полного цикла оценки — то, что уходит в уведомление."""

    listing: Listing
    match: MatchResult
    monochrome: MonochromeAssessment
    value: ValueAssessment
    ranking: RankingResult
    evaluated_at: datetime = field(default_factory=utcnow)

    @property
    def relevance(self) -> float:
        return self.ranking.score


@dataclass(slots=True)
class SourceStatus:
    """Состояние источника данных — показывается в /status."""

    name: str
    kind: SourceKind
    available: bool
    is_test_data: bool
    checked_at: datetime | None = None
    message: str = ""
    detail: str = ""
    requires_token: bool = False
    last_error: str | None = None
    last_success_at: datetime | None = None
    requests_total: int = 0
    requests_failed: int = 0
    rate_limited_until: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "available": self.available,
            "is_test_data": self.is_test_data,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "message": self.message,
            "requires_token": self.requires_token,
            "last_error": self.last_error,
            "requests_total": self.requests_total,
            "requests_failed": self.requests_failed,
        }


@dataclass(slots=True)
class FetchRequest:
    """Запрос к источнику: что тянуть и сколько."""

    collections: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    query: str | None = None
    min_price_nano: int | None = None
    max_price_nano: int | None = None
    limit: int = 60
    ordering: str = "Price"
    low_to_high: bool = True


@dataclass(slots=True)
class FetchResult:
    listings: list[Listing] = field(default_factory=list)
    truncated: bool = False
    is_test_data: bool = False
    errors: list[str] = field(default_factory=list)
    requested_at: datetime = field(default_factory=utcnow)
    #: True, если получена ВСЯ выдача по запросу (нет следующей страницы и
    #: результат не обрезан лимитом). Только в этом случае безопасно считать
    #: отсутствующие в ответе лоты неактивными.
    complete: bool = True

    def __len__(self) -> int:
        return len(self.listings)


def freshness_component(age_hours: float | None, half_life_hours: float) -> float:
    """Экспоненциальное затухание: 1.0 для свежего лота, 0.5 на половине периода.

    Если дата выставления неизвестна, возвращаем нейтральные 0.5 — отсутствие
    данных не должно ни поднимать, ни топить лот.
    """
    if age_hours is None:
        return 0.5
    if half_life_hours <= 0:
        return 1.0
    return math.pow(0.5, max(0.0, age_hours) / half_life_hours)
