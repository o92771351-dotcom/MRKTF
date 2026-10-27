"""Проверка лота пользовательским фильтром + расчёт степени соответствия.

Два разных результата одной проверки:

* `passed` — прошли ли ЖЁСТКИЕ условия. Жёсткие условия отсекают лот ДО
  ранжирования (требование задачи): текст/коллекция, диапазон цены, валюта,
  модель/фон/символ, требование монохромности, минимальная релевантность.
* `match_score` — насколько «плотно» лот соответствует фильтру (0..1).
  Идёт компонентом `match` в формулу ранжирования. Редкие атрибуты дают
  больший вклад: совпадение с моделью редкостью 0.5 % ценнее совпадения
  с моделью редкостью 30 %.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.domain import Listing, MatchResult, fmt_ton, nano_to_ton, ton_to_nano

# Веса критериев для match_score (нормируются на сумму заданных).
CRITERION_WEIGHTS: dict[str, float] = {
    "collection": 1.0,
    "price": 0.8,
    "query": 0.8,
    "model": 1.0,
    "backdrop": 1.0,
    "symbol": 1.0,
    "monochrome": 1.2,
    "rarity": 0.6,
    "number": 0.6,
}


@dataclass(slots=True)
class FilterCriteria:
    """Нормализованные критерии одного пользовательского фильтра."""

    filter_id: int | None = None
    name: str = ""
    query: str = ""
    collection_names: list[str] = field(default_factory=list)
    price_min_ton: float | None = None
    price_max_ton: float | None = None
    currency: str = "TON"
    model_names: list[str] = field(default_factory=list)
    backdrop_names: list[str] = field(default_factory=list)
    symbol_names: list[str] = field(default_factory=list)

    # Монохромность.
    mono_enabled: bool = False
    mono_mode: str = "gray"
    mono_max_saturation: float = 0.12
    mono_min_score: int = 60
    mono_custom_hue: float | None = None
    mono_custom_hue_tolerance: float = 25.0
    mono_require_confidence: str = ""
    mono_allow_image: bool = True
    mono_allow_name_heuristic: bool = True

    # Пороги и ранжирование.
    min_relevance: float = 0.0
    min_value_percent: float | None = None
    min_rarity_percent: float | None = None
    number_equals: int | None = None

    # Редкости из справочника (подставляются извне, если известны).
    rarity_maps: dict[str, dict[str, float]] = field(default_factory=dict)

    # ------------------------------------------------------------------ фабричные
    @classmethod
    def from_row(cls, row: Any, *, defaults: dict[str, Any] | None = None) -> "FilterCriteria":
        """Собрать критерии из строки БД (объект с атрибутами)."""
        defaults = defaults or {}
        get = lambda key, fallback=None: getattr(row, key, fallback)  # noqa: E731
        return cls(
            filter_id=get("id"),
            name=str(get("name") or ""),
            query=str(get("query_text") or "").strip(),
            collection_names=_split_list(get("collection_names")),
            price_min_ton=_opt_float(get("price_min_ton")),
            price_max_ton=_opt_float(get("price_max_ton")),
            currency=str(get("currency") or "TON").upper(),
            model_names=_split_list(get("model_names")),
            backdrop_names=_split_list(get("backdrop_names")),
            symbol_names=_split_list(get("symbol_names")),
            mono_enabled=bool(get("mono_enabled", False)),
            mono_mode=str(get("mono_mode") or defaults.get("mono_mode", "gray")),
            mono_max_saturation=float(
                get("mono_max_saturation") if get("mono_max_saturation") is not None
                else defaults.get("mono_max_saturation", 0.12)
            ),
            mono_min_score=int(
                get("mono_min_score") if get("mono_min_score") is not None
                else defaults.get("mono_min_score", 60)
            ),
            mono_custom_hue=_opt_float(get("mono_custom_hue")),
            mono_custom_hue_tolerance=float(
                get("mono_custom_hue_tolerance") if get("mono_custom_hue_tolerance") is not None
                else defaults.get("mono_custom_hue_tolerance", 25.0)
            ),
            mono_require_confidence=str(get("mono_require_confidence") or ""),
            mono_allow_image=bool(get("mono_allow_image", True)),
            mono_allow_name_heuristic=bool(
                get("mono_allow_name_heuristic")
                if get("mono_allow_name_heuristic") is not None
                else defaults.get("mono_allow_name_heuristic", True)
            ),
            min_relevance=float(
                get("min_relevance") if get("min_relevance") is not None
                else defaults.get("min_relevance", 0)
            ),
            min_value_percent=_opt_float(get("min_value_percent")),
            min_rarity_percent=_opt_float(get("min_rarity_percent")),
            number_equals=_opt_int(get("number_equals")),
        )

    # ------------------------------------------------------------------ свойства
    @property
    def price_min_nano(self) -> int | None:
        return ton_to_nano(self.price_min_ton)

    @property
    def price_max_nano(self) -> int | None:
        return ton_to_nano(self.price_max_ton)

    def describe(self) -> str:
        parts: list[str] = []
        if self.collection_names:
            parts.append("коллекции: " + ", ".join(self.collection_names[:4]))
        if self.query:
            parts.append(f"поиск: «{self.query}»")
        if self.price_min_ton is not None or self.price_max_ton is not None:
            low = f"{self.price_min_ton:g}" if self.price_min_ton is not None else "0"
            high = f"{self.price_max_ton:g}" if self.price_max_ton is not None else "∞"
            parts.append(f"цена: {low}…{high} {self.currency}")
        if self.model_names:
            parts.append("модели: " + ", ".join(self.model_names[:4]))
        if self.backdrop_names:
            parts.append("фоны: " + ", ".join(self.backdrop_names[:4]))
        if self.symbol_names:
            parts.append("символы: " + ", ".join(self.symbol_names[:4]))
        if self.mono_enabled:
            labels = {"gray": "ч/б и серые", "dark": "тёмные", "light": "светлые", "custom": "заданный тон"}
            parts.append(f"монохром: {labels.get(self.mono_mode, self.mono_mode)} ≥ {self.mono_min_score}")
        if self.min_value_percent is not None:
            parts.append(f"выгода ≥ {self.min_value_percent:g} %")
        if self.min_rarity_percent is not None:
            parts.append(f"редкость ≤ {self.min_rarity_percent:g} %")
        if self.number_equals is not None:
            parts.append(f"номер = {self.number_equals}")
        parts.append(f"релевантность ≥ {self.min_relevance:g}")
        return "\n".join(f"• {p}" for p in parts)

    def rarity_percent(self, listing: Listing) -> float | None:
        """Наименьшая известная редкость атрибутов лота в % (чем меньше — тем реже)."""
        values: list[float] = []
        for section, name in (
            ("models", listing.model_name),
            ("backdrops", listing.backdrop_name),
            ("symbols", listing.symbol_name),
        ):
            if not name:
                continue
            table = self.rarity_maps.get(section) or {}
            pct = table.get(str(name).strip().lower())
            if pct is not None:
                values.append(float(pct))
        # Резерв: редкость в ‰ прямо из ответа источника.
        for permille in (
            listing.model_rarity_permille,
            listing.backdrop_rarity_permille,
            listing.symbol_rarity_permille,
        ):
            if permille and permille > 0 and not values:
                values.append(float(permille) / 10.0)
        return min(values) if values else None


def _split_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items: Iterable[Any] = raw
    else:
        items = str(raw).split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _opt_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _opt_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _in_any(value: str | None, wanted: Sequence[str]) -> bool:
    if not wanted:
        return True
    needle = (value or "").strip().lower()
    return bool(needle) and needle in {w.strip().lower() for w in wanted}


def _contains_any(value: str | None, wanted: Sequence[str]) -> bool:
    if not wanted:
        return True
    haystack = (value or "").strip().lower()
    return any(w.strip().lower() in haystack for w in wanted if w.strip())


def apply(
    listing: Listing,
    criteria: FilterCriteria,
    *,
    monochrome_score: float | None = None,
    monochrome_ok: bool | None = None,
) -> MatchResult:
    """Проверить лот фильтром.

    `monochrome_ok`/`monochrome_score` передаются извне, потому что оценка
    монохромности — отдельный (и потенциально дорогой) шаг: детектор вызывается
    до проверки фильтра, а здесь используется его результат.
    """
    failures: list[str] = []
    tracked: list[tuple[str, float, float]] = []   # (критерий, вес, балл 0..1)

    def add(key: str, score: float) -> None:
        tracked.append((key, CRITERION_WEIGHTS.get(key, 1.0), max(0.0, min(1.0, score))))

    # --- 1. Валюта (жёсткое условие; вклад в балл не даёт) ------------------
    if criteria.currency:
        wanted = criteria.currency.strip().upper()
        actual = (listing.currency or "TON").strip().upper()
        if wanted != actual:
            failures.append(f"валюта {actual}, нужна {wanted}")

    # --- 2. Текст / название (жёсткое) ------------------------------------
    if criteria.query:
        haystack = " ".join(
            str(x or "")
            for x in (
                listing.collection_name,
                listing.title,
                listing.model_name,
                listing.backdrop_name,
                listing.symbol_name,
            )
        ).lower()
        needles = [token for token in criteria.query.lower().split() if token]
        hits = sum(1 for token in needles if token in haystack)
        if needles and hits == 0:
            failures.append(f"нет совпадения с «{criteria.query}»")
        elif needles:
            add("query", hits / len(needles))

    # --- 3. Коллекция (жёсткое) -------------------------------------------
    if criteria.collection_names:
        if _in_any(listing.collection_name, criteria.collection_names):
            add("collection", 1.0)
        else:
            failures.append(f"коллекция «{listing.collection_name}» не в списке")

    # --- 4. Цена (жёсткое) -------------------------------------------------
    if criteria.price_min_nano is not None and listing.price_nano < criteria.price_min_nano:
        failures.append(
            f"цена {fmt_ton(listing.price_nano)} ниже минимума {criteria.price_min_ton:g}"
        )
    elif criteria.price_max_nano is not None and listing.price_nano > criteria.price_max_nano:
        failures.append(
            f"цена {fmt_ton(listing.price_nano)} выше максимума {criteria.price_max_ton:g}"
        )
    elif criteria.price_min_nano is not None or criteria.price_max_nano is not None:
        add("price", _price_position_score(listing.price_nano, criteria))

    # --- 5. Модель / фон / символ (жёсткое) --------------------------------
    if criteria.model_names:
        if _in_any(listing.model_name, criteria.model_names):
            add("model", _rarity_score(listing.model_rarity_permille, criteria, "models", listing.model_name))
        else:
            failures.append(f"модель «{listing.model_name or '—'}» не в списке")

    if criteria.backdrop_names:
        if _in_any(listing.backdrop_name, criteria.backdrop_names):
            add("backdrop", _rarity_score(listing.backdrop_rarity_permille, criteria, "backdrops", listing.backdrop_name))
        else:
            failures.append(f"фон «{listing.backdrop_name or '—'}» не в списке")

    if criteria.symbol_names:
        if _in_any(listing.symbol_name, criteria.symbol_names):
            add("symbol", _rarity_score(listing.symbol_rarity_permille, criteria, "symbols", listing.symbol_name))
        else:
            failures.append(f"символ «{listing.symbol_name or '—'}» не в списке")

    # --- 6. Номер (жёсткое) ------------------------------------------------
    if criteria.number_equals is not None:
        if listing.number == criteria.number_equals:
            add("number", 1.0)
        else:
            failures.append(f"номер {listing.number if listing.number is not None else '—'} ≠ {criteria.number_equals}")

    # --- 7. Редкость (жёсткое, если задано) --------------------------------
    if criteria.min_rarity_percent is not None:
        rarity = criteria.rarity_percent(listing)
        if rarity is None:
            # Данных о редкости нет — не отсекаем, но и не хвалим.
            add("rarity", 0.5)
        elif rarity <= criteria.min_rarity_percent:
            add("rarity", max(0.2, min(1.0, criteria.min_rarity_percent / max(rarity, 1e-6) / 4.0)))
        else:
            failures.append(f"редкость {rarity:g} % выше порога {criteria.min_rarity_percent:g} %")

    # --- 8. Монохромность (жёсткое, если включена) --------------------------
    if criteria.mono_enabled:
        if monochrome_ok is None:
            failures.append("монохромность не определена")
        elif monochrome_ok:
            add("monochrome", (monochrome_score or 0.0) / 100.0)
        else:
            score_text = f"{monochrome_score:.0f}" if monochrome_score is not None else "—"
            failures.append(f"монохромность {score_text} < {criteria.mono_min_score}")

    total_weight = sum(weight for _, weight, _ in tracked)
    match_score = (
        sum(weight * score for _, weight, score in tracked) / total_weight if total_weight else 1.0
    )
    if failures:
        match_score = 0.0

    labels = {
        "collection": "коллекция",
        "query": "текст",
        "price": "цена",
        "model": "модель",
        "backdrop": "фон",
        "symbol": "символ",
        "monochrome": "монохром",
        "rarity": "редкость",
        "number": "номер",
    }
    matched = [labels.get(key, key) for key, _, score in tracked if score > 0]

    return MatchResult(
        passed=not failures,
        hard_failures=failures,
        match_score=round(match_score, 4),
        matched_criteria=matched[:8],
        total_criteria=len(_active_criteria(criteria)),
    )


def _active_criteria(criteria: FilterCriteria) -> list[str]:
    active: list[str] = []
    if criteria.collection_names:
        active.append("collection")
    if criteria.query:
        active.append("query")
    if criteria.price_min_ton is not None or criteria.price_max_ton is not None:
        active.append("price")
    if criteria.model_names:
        active.append("model")
    if criteria.backdrop_names:
        active.append("backdrop")
    if criteria.symbol_names:
        active.append("symbol")
    if criteria.number_equals is not None:
        active.append("number")
    if criteria.min_rarity_percent is not None:
        active.append("rarity")
    if criteria.mono_enabled:
        active.append("monochrome")
    return active


def _price_position_score(price_nano: int, criteria: FilterCriteria) -> float:
    """Ближе к нижней границе диапазона — выше балл (интереснее для покупки)."""
    low = criteria.price_min_nano
    high = criteria.price_max_nano
    if low is None and high is None:
        return 1.0
    if low is None:
        low = 0
    if high is None:
        # Открытый верх: считаем относительно нижней границы.
        return 0.6 if price_nano >= low else 1.0
    if high <= low:
        return 1.0
    position = (price_nano - low) / (high - low)
    return max(0.0, min(1.0, 1.0 - position))


def _rarity_score(
    permille: int | None,
    criteria: FilterCriteria,
    section: str,
    name: str | None,
) -> float:
    """Совпадение атрибута: редкие варианты дают больший вклад (0.3..1.0)."""
    pct: float | None = None
    table = criteria.rarity_maps.get(section) or {}
    if name:
        pct = table.get(str(name).strip().lower())
    if pct is None and permille and permille > 0:
        pct = float(permille) / 10.0
    if pct is None:
        return 0.6  # совпало, но редкость неизвестна
    # 0.1 % -> ~1.0, 1 % -> ~0.83, 10 % -> ~0.55, 50 % -> ~0.3
    return max(0.3, min(1.0, 0.5 + 0.5 * math.exp(-max(pct, 0.0) / 5.0)))


def matches_price_range(listing: Listing, criteria: FilterCriteria) -> bool:
    """Быстрая проверка только цены (используется в /search для подсказок)."""
    if criteria.price_min_nano is not None and listing.price_nano < criteria.price_min_nano:
        return False
    if criteria.price_max_nano is not None and listing.price_nano > criteria.price_max_nano:
        return False
    return True


def price_range_ton(criteria: FilterCriteria) -> tuple[float | None, float | None]:
    return criteria.price_min_ton, criteria.price_max_ton


def nano_from_ton(ton: float) -> int:
    return int(round(ton * 1_000_000_000))


__all__ = [
    "FilterCriteria",
    "apply",
    "matches_price_range",
    "nano_to_ton",
    "price_range_ton",
]
