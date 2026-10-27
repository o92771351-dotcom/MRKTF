"""Ранжирование: оценка релевантности лота 0..100.

ФОРМУЛА
-------
    relevance = 100 * Σ(w_i · c_i) / Σ(w_i),   i ∈ {match, monochrome, value,
                                                  confidence, freshness}

где компоненты c_i нормированы в 0..1:

    match       = match_score из проверки фильтром (плотность соответствия);
    monochrome  = балл монохромности / 100;
    value       = clamp(diff_percent / 100, 0, 1) — скидка к ориентиру;
                  20 % ниже ориентира -> 0.20, 100 % и больше -> 1.0;
                  лот дороже ориентира -> 0.0;
    confidence  = среднее уверенностей оценки выгоды и монохромности
                  (high=1.0, medium=0.6, low=0.3);
    freshness   = 0.5 ** (age_hours / half_life_hours), 0.5 если дата
                  выставления неизвестна.

ПЕРЕРАСПРЕДЕЛЕНИЕ ВЕСОВ
-----------------------
Если компонент посчитать нельзя (например, ориентир цены не определён и
`value` отсутствует), его вес НЕ обнуляет оценку, а пропорционально
перераспределяется между доступными компонентами:

    relevance = 100 * Σ(w_i · c_i) / Σ(w_i)   только по доступным i

Так лот с неизвестной выгодой не тонет автоматически, но и не получает
бонус — в карточке честно пишется, что оценка выгоды отсутствует.

ВЕСА НАСТРАИВАЮТСЯ
------------------
Значения по умолчанию берутся из .env (RANK_WEIGHT_*), а каждый пользователь
может изменить их командой /weights — они хранятся в БД отдельно от глобальных.
Жёсткие фильтры применяются ДО ранжирования (см. filters/matching.py),
поэтому веса влияют только на порядок и на порог min_relevance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.domain import (
    CONFIDENCE_SCORE,
    Confidence,
    Listing,
    MatchResult,
    MonochromeAssessment,
    RankingResult,
    ValueAssessment,
    freshness_component,
)

COMPONENT_ORDER: tuple[str, ...] = ("match", "monochrome", "value", "confidence", "freshness")

DEFAULT_WEIGHTS: dict[str, float] = {
    "match": 0.25,
    "monochrome": 0.25,
    "value": 0.25,
    "confidence": 0.15,
    "freshness": 0.10,
}

WEIGHT_LIMITS: dict[str, tuple[float, float]] = {
    key: (0.0, 1.0) for key in COMPONENT_ORDER
}


@dataclass(slots=True)
class RankingWeights:
    """Веса формулы ранжирования."""

    match: float = DEFAULT_WEIGHTS["match"]
    monochrome: float = DEFAULT_WEIGHTS["monochrome"]
    value: float = DEFAULT_WEIGHTS["value"]
    confidence: float = DEFAULT_WEIGHTS["confidence"]
    freshness: float = DEFAULT_WEIGHTS["freshness"]

    # ------------------------------------------------------------------ фабрики
    @classmethod
    def from_settings(cls, settings: Any) -> "RankingWeights":
        return cls(
            match=float(getattr(settings, "rank_weight_match", DEFAULT_WEIGHTS["match"])),
            monochrome=float(getattr(settings, "rank_weight_monochrome", DEFAULT_WEIGHTS["monochrome"])),
            value=float(getattr(settings, "rank_weight_value", DEFAULT_WEIGHTS["value"])),
            confidence=float(getattr(settings, "rank_weight_confidence", DEFAULT_WEIGHTS["confidence"])),
            freshness=float(getattr(settings, "rank_weight_freshness", DEFAULT_WEIGHTS["freshness"])),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RankingWeights":
        """Собрать веса из словаря (например, из JSON-колонки БД).

        Отсутствующие или некорректные ключи берут значение по умолчанию.
        """
        kwargs: dict[str, float] = {}
        for key in COMPONENT_ORDER:
            default = float(DEFAULT_WEIGHTS[key])
            value = (raw or {}).get(key)
            if value is None:
                kwargs[key] = default
                continue
            try:
                kwargs[key] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                kwargs[key] = default
        return cls(**kwargs)

    # ------------------------------------------------------------------ методы
    def as_dict(self) -> dict[str, float]:
        return {key: float(getattr(self, key)) for key in COMPONENT_ORDER}

    def normalized(self) -> dict[str, float]:
        total = sum(self.as_dict().values())
        if total <= 0:
            equal = 1.0 / len(COMPONENT_ORDER)
            return {key: equal for key in COMPONENT_ORDER}
        return {key: value / total for key, value in self.as_dict().items()}

    def describe(self) -> str:
        norm = self.normalized()
        labels = {
            "match": "соответствие фильтрам",
            "monochrome": "степень монохромности",
            "value": "предполагаемая выгода",
            "confidence": "надёжность данных",
            "freshness": "актуальность лота",
        }
        return "\n".join(
            f"• {labels[key]} — {norm[key] * 100:.0f} %" for key in COMPONENT_ORDER
        )


# ----------------------------------------------------------------------------
#  Компоненты
# ----------------------------------------------------------------------------

def value_component(assessment: ValueAssessment | None) -> float | None:
    """Скидка к ориентиру, 0..1. None — если ориентир не определён."""
    if assessment is None or assessment.benchmark_nano is None:
        return None
    if assessment.diff_percent is None:
        return None
    return max(0.0, min(1.0, assessment.diff_percent / 100.0))


def confidence_component(
    value: ValueAssessment | None,
    monochrome: MonochromeAssessment | None,
) -> float | None:
    """Средняя уверенность доступных оценок. None — если уверенностей нет."""
    parts: list[float] = []
    if value is not None and value.benchmark_nano is not None:
        parts.append(CONFIDENCE_SCORE.get(value.confidence, 0.0))
    if monochrome is not None and monochrome.confidence != Confidence.NONE:
        parts.append(CONFIDENCE_SCORE.get(monochrome.confidence, 0.0))
    if not parts:
        return None
    return sum(parts) / len(parts)


# ----------------------------------------------------------------------------
#  Итоговый расчёт
# ----------------------------------------------------------------------------

def rank(
    listing: Listing,
    *,
    match: MatchResult,
    monochrome: MonochromeAssessment | None,
    value: ValueAssessment | None,
    weights: RankingWeights | None = None,
    freshness_half_life_hours: float = 12.0,
) -> RankingResult:
    """Посчитать релевантность 0..100 с разложением по компонентам."""
    weights = weights or RankingWeights()
    weight_map = weights.as_dict()

    components: dict[str, float] = {
        "match": max(0.0, min(1.0, match.match_score if match.passed else 0.0)),
        "freshness": freshness_component(listing.age_hours, freshness_half_life_hours),
    }

    mono_value = (monochrome.score / 100.0) if monochrome is not None else None
    if mono_value is not None:
        components["monochrome"] = max(0.0, min(1.0, mono_value))

    value_value = value_component(value)
    if value_value is not None:
        components["value"] = value_value

    confidence_value = confidence_component(value, monochrome)
    if confidence_value is not None:
        components["confidence"] = confidence_value

    used_weights = {key: weight_map.get(key, 0.0) for key in components}
    total_weight = sum(used_weights.values())
    if total_weight <= 0:
        # Все веса обнулены пользователем — считаем простым средним.
        total_weight = float(len(components)) or 1.0
        used_weights = {key: 1.0 for key in components}

    score = 100.0 * sum(used_weights[key] * components[key] for key in components) / total_weight
    score = max(0.0, min(100.0, score))

    return RankingResult(
        score=round(score, 1),
        components={key: round(val, 4) for key, val in components.items()},
        weights=weight_map,
        used_weights={key: round(val, 4) for key, val in used_weights.items()},
    )


def explain(result: RankingResult) -> str:
    """Текстовое объяснение оценки — для карточки и для /help."""
    labels = {
        "match": "соответствие фильтрам",
        "monochrome": "монохромность",
        "value": "выгода",
        "confidence": "надёжность данных",
        "freshness": "свежесть",
    }
    total = sum(result.used_weights.values()) or 1.0
    lines = [f"Релевантность: {result.score:.1f} / 100"]
    for key in COMPONENT_ORDER:
        if key not in result.components:
            lines.append(f"• {labels[key]}: нет данных (вес перераспределён)")
            continue
        share = result.used_weights.get(key, 0.0) / total * 100.0
        contribution = result.components[key] * result.used_weights.get(key, 0.0) / total * 100.0
        lines.append(
            f"• {labels[key]}: {result.components[key] * 100:.0f} × вес {share:.0f} % "
            f"= {contribution:.1f} п."
        )
    return "\n".join(lines)


__all__ = [
    "COMPONENT_ORDER",
    "DEFAULT_WEIGHTS",
    "RankingWeights",
    "confidence_component",
    "explain",
    "rank",
    "value_component",
]
