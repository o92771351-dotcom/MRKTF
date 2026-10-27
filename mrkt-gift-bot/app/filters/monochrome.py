"""Определение монохромности лота.

ПРИНЦИПЫ (по требованиям задачи)
--------------------------------
1. Приоритет — у данных источника. MRKT отдаёт цвета фона
   (`backdropColorsCenterColor`, `...EdgeColor`, `...SymbolColor`) — это
   самый надёжный признак, он используется первым.
2. Если цветов нет — запрашиваем их у публичного API @GiftChanges
   (`/backdrop/:gift/:backdrop/info` -> centerColor/edgeColor/patternColor/hex).
3. Если и там пусто, но есть изображение — анализируем картинку
   (статистика насыщенности по HSV).
4. По названию — ТОЛЬКО как последний резерв и ТОЛЬКО с пометкой
   `confidence=low`. Высокая уверенность по названию невозможна никогда.
5. Результат всегда содержит способ определения, уверенность и перечень
   свидетельств — они показываются в карточке уведомления.

РЕЖИМЫ (задаёт пользователь в фильтре)
--------------------------------------
gray   — любой ахроматический art (чёрно-белый/серый);
dark   — чёрный/тёмно-серый;
light  — белый/светло-серый;
custom — заданный тон (hue ± допуск) либо очень низкая насыщенность.

ОЦЕНКА
------
Каждое свидетельство даёт `score` (0..1) и `confidence`. Итоговый балл
0..100 — средневзвешенное по уверенности:

    score = 100 * Σ(score_i * w_i) / Σ(w_i),   w_i = вес уверенности
    high = 1.0, medium = 0.6, low = 0.3

Если единственное свидетельство — эвристика по названию, итоговая уверенность
принудительно ограничивается `low`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.domain import (
    CONFIDENCE_SCORE,
    Confidence,
    Listing,
    MonochromeAssessment,
    MonochromeEvidence,
    int_to_rgb,
)
from app.filters.colors import (
    HSV,
    PaletteStats,
    RGB,
    hex_to_rgb,
    hue_distance,
    ramp_down,
    ramp_up,
    rgb_to_hsv,
    summarize_colors,
)
from app.filters.image_analysis import ImageAnalysis, analyze_image_bytes

logger = logging.getLogger(__name__)

# Ключевые слова для резервной эвристики по названию. Совпадение даёт
# НЕВЫСОКИЙ балл и всегда low confidence.
MONO_KEYWORDS: tuple[str, ...] = (
    "mono", "monochrome", "monoChrome", "grayscale", "greyscale", "gray", "grey",
    "black", "white", "noir", "blanc", "charcoal", "graphite", "silver", "platinum",
    "pearl", "ivory", "alabaster", "chalk", "ash", "smoke", "fog", "mist", "linen",
    "bone", "zinc", "steel", "iron", "obsidian", "onyx", "slate", "basalt", "granite",
    "snow", "ink", "coal", "soot", "серебр", "графит", "черно-бел", "чёрно-бел",
    "монохром", "серый", "белый", "чёрный", "черный",
)

# Слова, которые намекают на ЦВЕТ и потому понижают балл эвристики.
CHROMATIC_KEYWORDS: tuple[str, ...] = (
    "rainbow", "aurora", "crimson", "scarlet", "ruby", "amber", "gold", "golden",
    "emerald", "jade", "sapphire", "azure", "cobalt", "teal", "violet", "purple",
    "pink", "rose", "coral", "saffron", "mint", "copper", "bronze", "neon",
    "iridescent", "holographic", "peach", "sky",
)


@dataclass(slots=True)
class MonochromeSpec:
    """Пользовательские требования к монохромности."""

    enabled: bool = True
    mode: str = "gray"                     # gray | dark | light | custom
    max_saturation: float = 0.12           # порог насыщенности для «серого»
    min_score: int = 60                    # минимальный балл, чтобы лот прошёл
    custom_hue: float | None = None        # 0..360 для mode=custom
    custom_hue_tolerance: float = 25.0
    require_confidence: str = ""           # "", "medium", "high" — минимум уверенности
    allow_image: bool = True
    allow_name_heuristic: bool = True

    @classmethod
    def from_row(cls, row: Any) -> "MonochromeSpec":
        """Собрать спецификацию из строки БД (объект с атрибутами)."""
        return cls(
            enabled=bool(getattr(row, "mono_enabled", True)),
            mode=str(getattr(row, "mono_mode", "gray") or "gray"),
            max_saturation=float(getattr(row, "mono_max_saturation", 0.12)),
            min_score=int(getattr(row, "mono_min_score", 60)),
            custom_hue=(
                float(row.mono_custom_hue) if getattr(row, "mono_custom_hue", None) is not None else None
            ),
            custom_hue_tolerance=float(getattr(row, "mono_custom_hue_tolerance", 25.0)),
            require_confidence=str(getattr(row, "mono_require_confidence", "") or ""),
            allow_image=bool(getattr(row, "mono_allow_image", True)),
            allow_name_heuristic=bool(getattr(row, "mono_allow_name_heuristic", True)),
        )

    def describe(self) -> str:
        labels = {
            "gray": "ч/б и серые тона",
            "dark": "чёрный / тёмно-серый",
            "light": "белый / светло-серый",
            "custom": "заданный цвет",
        }
        text = labels.get(self.mode, self.mode)
        if self.mode == "custom" and self.custom_hue is not None:
            text += f" (тон {self.custom_hue:.0f}° ± {self.custom_hue_tolerance:.0f}°)"
        return f"{text}, порог ≥ {self.min_score}"


def _tone_score(stats: PaletteStats, spec: MonochromeSpec) -> float:
    """Насколько палитра соответствует режиму spec. Возвращает 0..1."""
    threshold = max(0.01, min(0.9, float(spec.max_saturation)))
    # Порог «заметно цветного»: втрое выше пользовательского.
    gray = ramp_down(stats.mean_saturation, threshold, threshold * 3.0)

    if spec.mode == "gray":
        return gray

    # Для dark/light насыщенность тоже важна, а светлота работает как ВОРОТА:
    # если тон явно не тот, лот не проходит, каким бы ахроматичным он ни был
    # (иначе чёрный подарок набирал бы баллы в режиме «белый»).
    # 0.15 — небольшой допуск на случай неполных данных о цвете.
    if spec.mode == "dark":
        tone = ramp_down(stats.mean_value, 0.34, 0.62)
        return gray * (0.15 + 0.85 * tone)
    if spec.mode == "light":
        tone = ramp_up(stats.mean_value, 0.58, 0.86)
        return gray * (0.15 + 0.85 * tone)
    if spec.mode == "custom":
        if spec.custom_hue is None:
            return gray
        if stats.dominant_hue is None:
            return gray
        # Монохромность в пользовательском режиме = «один тон», т.е. малый
        # разброс вокруг доминанты + близость доминанты к целевому тону.
        distance = hue_distance(stats.dominant_hue, spec.custom_hue)
        tolerance = max(1.0, spec.custom_hue_tolerance)
        hue_match = ramp_down(distance, tolerance, tolerance * 2.2)
        spread = stats.hue_spread if stats.hue_spread is not None else 0.0
        uniformity = ramp_down(spread, tolerance, tolerance * 3.0)
        return max(0.0, min(1.0, 0.65 * hue_match + 0.35 * uniformity))
    return gray


def _palette_from_ints(values: Iterable[int | None]) -> list[RGB]:
    colors: list[RGB] = []
    for value in values:
        rgb = int_to_rgb(value)
        if rgb is not None:
            colors.append(rgb)
    return colors


# Цвет АРТА (фон/узор) и цвет ТЕКСТА поверх него — разные вещи.
# `textColor` у MRKT и @GiftChanges — это цвет надписи/UI, подобранный для
# контраста: на тёмном фоне он светлый. Если включить его в статистику,
# режимы dark/light перестают работать (тёмный фон «осветляется» белым
# текстом). Поэтому в расчёт идут только цвета арта.
ART_COLOR_KEYS = ("center", "edge", "pattern", "symbol")
TEXT_COLOR_KEYS = ("text",)


def _palette_from_changes(colors: dict[str, Any] | None) -> list[RGB]:
    """Цвета АРТА из @GiftChanges: center / edge / pattern (text не учитывается)."""
    if not colors:
        return []
    out: list[RGB] = []
    for key in ART_COLOR_KEYS:
        value = colors.get(key)
        if isinstance(value, (tuple, list)) and len(value) >= 3:
            out.append((int(value[0]), int(value[1]), int(value[2])))
    return out


def _palette_from_hex_strings(values: Iterable[str | None]) -> list[RGB]:
    out: list[RGB] = []
    for value in values:
        rgb = hex_to_rgb(value) if isinstance(value, str) else None
        if rgb is not None:
            out.append(rgb)
    return out


class MonochromeDetector:
    """Определяет монохромность лота по доступным свидетельствам."""

    def __init__(self, changes_client: Any | None = None, *, image_timeout: float = 15.0) -> None:
        self._changes = changes_client
        self._image_timeout = image_timeout

    # ------------------------------------------------------------------ публичное
    async def assess(
        self,
        listing: Listing,
        spec: MonochromeSpec,
        *,
        image_bytes: bytes | None = None,
        changes_colors: dict[str, Any] | None = None,
        fetch_image: Any | None = None,
    ) -> MonochromeAssessment:
        """Полный цикл оценки.

        `fetch_image` — необязательная async-функция `(listing) -> ImageResult|None`,
        вызывается лениво и только если цветов из API не хватило для решения.
        """
        evidences: list[MonochromeEvidence] = []

        # 1) Цвета фона из ответа источника (MRKT): только цвета арта.
        api_colors = _palette_from_ints(
            [
                listing.backdrop_center_color,
                listing.backdrop_edge_color,
                listing.backdrop_symbol_color,
            ]
        )
        if api_colors:
            stats = summarize_colors(api_colors)
            evidences.append(
                MonochromeEvidence(
                    method="backdrop_colors",
                    score=_tone_score(stats, spec),
                    confidence=Confidence.HIGH,
                    details={
                        "n_colors": len(api_colors),
                        "hex": [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in api_colors],
                        "mean_saturation": round(stats.mean_saturation, 4),
                        "mean_value": round(stats.mean_value, 4),
                        "palette": stats,
                    },
                )
            )

        # 2) Цвета фона из публичного API @GiftChanges.
        if changes_colors is None and self._changes is not None and listing.backdrop_name:
            changes_colors = await self._changes.backdrop_colors(
                listing.collection_name, listing.backdrop_name
            )
        external_colors = _palette_from_changes(changes_colors)
        if external_colors:
            stats = summarize_colors(external_colors)
            evidences.append(
                MonochromeEvidence(
                    method="changes_api_colors",
                    score=_tone_score(stats, spec),
                    confidence=Confidence.HIGH,
                    details={
                        "n_colors": len(external_colors),
                        "hex": [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in external_colors],
                        "mean_saturation": round(stats.mean_saturation, 4),
                        "mean_value": round(stats.mean_value, 4),
                        "palette": stats,
                    },
                )
            )

        # Достаточно ли данных, чтобы решить без картинки?
        decided = _decided_by_colors(evidences, spec)

        # 3) Анализ изображения — только если цвета не дали решения.
        if not decided and spec.allow_image:
            analysis: ImageAnalysis | None = None
            if image_bytes:
                analysis = await analyze_image_bytes(image_bytes, timeout=self._image_timeout)
            elif fetch_image is not None:
                try:
                    result = await fetch_image(listing)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("fetch_image для монохрома: %r", exc)
                    result = None
                if result is not None and getattr(result, "ok", False):
                    analysis = await analyze_image_bytes(result.data, timeout=self._image_timeout)
            if analysis is not None and analysis.ok and analysis.stats is not None:
                evidences.append(self._image_evidence(analysis, spec))

        # 4) Резерв: эвристика по названию — ТОЛЬКО если нет ни цветов, ни
        # картинки. Иначе слабое свидетельство разбавляло бы точные данные
        # (низкая уверенность не должна тянуть вниз высокую).
        if spec.allow_name_heuristic and not evidences:
            name_evidence = name_heuristic(listing, spec)
            if name_evidence is not None:
                evidences.append(name_evidence)

        return combine(evidences, spec)

    def _image_evidence(self, analysis: ImageAnalysis, spec: MonochromeSpec) -> MonochromeEvidence:
        stats = analysis.stats
        assert stats is not None
        score = _tone_score(stats, spec)
        # Чем меньше «арт-пикселей», тем ниже уверенность: вывод по 30 пикселям
        # обводки ничего не стоит.
        if analysis.pixels_art >= 2000:
            confidence = Confidence.MEDIUM
        elif analysis.pixels_art >= 300:
            confidence = Confidence.LOW
        else:
            confidence = Confidence.LOW
            score *= 0.8
        details: dict[str, Any] = {
            "format": analysis.format,
            "pixels_art": analysis.pixels_art,
            "colored_share": round(analysis.colored_share, 4),
            "mean_saturation": round(stats.mean_saturation, 4),
            "p90_saturation": round(stats.p90_saturation, 4),
            "mean_value": round(stats.mean_value, 4),
            "dominant_colors": [
                {"hex": f"#{r:02x}{g:02x}{b:02x}", "share": round(share, 4)}
                for (r, g, b), share in analysis.dominant_colors[:4]
            ],
            "palette": stats,
        }
        return MonochromeEvidence(method="image", score=score, confidence=confidence, details=details)


def _decided_by_colors(evidences: Sequence[MonochromeEvidence], spec: MonochromeSpec) -> bool:
    """Достаточно ли цветовых свидетельств, чтобы не качать картинку.

    Решением считаем случай, когда все цветовые свидетельства согласованы
    (баллы не противоречат друг другу) и итог уверенно проходит/не проходит
    порог пользователя.
    """
    color_evidences = [e for e in evidences if e.method in ("backdrop_colors", "changes_api_colors")]
    if not color_evidences:
        return False
    scores = [e.score for e in color_evidences]
    if max(scores) - min(scores) > 0.35:
        return False  # противоречие — нужна картинка
    mean = sum(scores) / len(scores)
    threshold = max(0.0, min(1.0, spec.min_score / 100.0))
    return mean <= threshold - 0.2 or mean >= threshold + 0.2


def name_heuristic(listing: Listing, spec: MonochromeSpec) -> MonochromeEvidence | None:
    """Резервная оценка по названию/атрибутам. Всегда low confidence."""
    haystack = " ".join(
        str(x or "")
        for x in (
            listing.title,
            listing.model_name,
            listing.backdrop_name,
            listing.symbol_name,
            listing.collection_name,
        )
    ).lower()
    if not haystack.strip():
        return None

    matched = [word for word in MONO_KEYWORDS if word in haystack]
    chromatic = [word for word in CHROMATIC_KEYWORDS if word in haystack]
    if not matched and not chromatic:
        return None

    score = 0.0
    if matched:
        score = min(0.45, 0.2 + 0.08 * len(matched))
    if chromatic:
        score = max(0.0, score - 0.15 * len(chromatic))

    # Соответствие режиму: «dark»/«light» по названию угадать ещё сложнее.
    if spec.mode == "dark" and matched:
        dark_words = {"black", "noir", "charcoal", "obsidian", "onyx", "ink", "coal", "soot",
                      "чёрный", "черный", "графит", "graphite", "basalt", "iron", "zinc", "slate"}
        score = score if any(w in matched for w in dark_words) else score * 0.4
    if spec.mode == "light" and matched:
        light_words = {"white", "blanc", "snow", "ivory", "pearl", "alabaster", "chalk", "bone",
                       "linen", "silver", "platinum", "белый", "серебр"}
        score = score if any(w in matched for w in light_words) else score * 0.4

    return MonochromeEvidence(
        method="name",
        score=max(0.0, min(1.0, score)),
        confidence=Confidence.LOW,
        details={"matched": matched[:6], "chromatic": chromatic[:6]},
    )


def combine(evidences: Sequence[MonochromeEvidence], spec: MonochromeSpec) -> MonochromeAssessment:
    """Свести свидетельства в итоговую оценку 0..100 + уверенность."""
    usable = [e for e in evidences if e.confidence != Confidence.NONE]
    if not usable:
        return MonochromeAssessment(
            score=0.0,
            method="none",
            confidence=Confidence.NONE,
            evidences=[],
            palette={},
        )

    weight_sum = sum(CONFIDENCE_SCORE[e.confidence] for e in usable)
    score = sum(e.score * CONFIDENCE_SCORE[e.confidence] for e in usable) / weight_sum
    score_100 = max(0.0, min(1.0, score)) * 100.0

    # Итоговая уверенность = максимальная уверенность свидетельств, но если
    # решало только название — не выше low.
    order = [Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH]
    best = max(usable, key=lambda e: order.index(e.confidence) if e.confidence in order else -1)
    confidence = best.confidence
    if all(e.method == "name" for e in usable):
        confidence = Confidence.LOW

    methods = {e.method for e in usable}
    if len(methods) > 1:
        method = "combined"
    else:
        method = next(iter(methods))

    palette: dict[str, Any] = {}
    for evidence in usable:
        stats = evidence.details.get("palette")
        if isinstance(stats, PaletteStats):
            palette[evidence.method] = {
                "mean_saturation": round(stats.mean_saturation, 4),
                "max_saturation": round(stats.max_saturation, 4),
                "mean_value": round(stats.mean_value, 4),
                "mean_luminance": round(stats.mean_luminance, 4),
                "dominant_hue": (round(stats.dominant_hue, 1) if stats.dominant_hue is not None else None),
            }
        if evidence.details.get("hex"):
            palette.setdefault("hex", []).extend(evidence.details["hex"])
        if evidence.details.get("dominant_colors"):
            palette.setdefault("image_colors", []).extend(evidence.details["dominant_colors"])

    return MonochromeAssessment(
        score=round(score_100, 1),
        method=method,
        confidence=confidence,
        evidences=list(usable),
        palette=palette,
    )


def passes(assessment: MonochromeAssessment, spec: MonochromeSpec) -> tuple[bool, str]:
    """Прошёл ли лот требование монохромности. Возвращает (ok, причина).

    Порядок проверок: данные -> требуемая уверенность -> балл. Уверенность
    проверяется раньше балла, потому что «угадали по названию» — это другой
    класс отказа, и пользователю важно видеть именно его.
    """
    if not spec.enabled:
        return True, ""
    if assessment.confidence == Confidence.NONE:
        return False, "нет данных о цвете"
    required = (spec.require_confidence or "").strip().lower()
    if required:
        order = {"low": 0, "medium": 1, "high": 2}
        if order.get(assessment.confidence.value, 0) < order.get(required, 0):
            return False, (
                f"уверенность {assessment.confidence.value} ниже требуемой {required} "
                f"(способ: {assessment.describe_method()})"
            )
    if assessment.score < spec.min_score:
        return False, f"балл монохромности {assessment.score:.0f} < {spec.min_score}"
    return True, ""


__all__ = [
    "MonochromeDetector",
    "MonochromeSpec",
    "combine",
    "name_heuristic",
    "passes",
    "HSV",
    "rgb_to_hsv",
]
