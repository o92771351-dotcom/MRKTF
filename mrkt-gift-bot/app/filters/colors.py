"""Цветовые вычисления: RGB -> HSV, насыщенность, светлота, расстояние по тону.

Используется детектором монохромности (цвета фона из API и анализ пикселей
изображения). Реализация намеренно без внешних зависимостей, чтобы её можно
было покрыть тестами и не тянуть numpy.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

RGB = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class HSV:
    h: float  # 0..360
    s: float  # 0..1
    v: float  # 0..1

    @property
    def saturation(self) -> float:
        return self.s

    @property
    def value(self) -> float:
        return self.v


def rgb_to_hsv(rgb: Sequence[int]) -> HSV:
    r, g, b = (max(0, min(255, int(c))) / 255.0 for c in rgb[:3])
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return HSV(h=h * 360.0, s=s, v=v)


def hex_to_rgb(value: str) -> RGB | None:
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        number = int(text, 16)
    except ValueError:
        return None
    return (number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF


def int_to_rgb(value: int | None) -> RGB | None:
    if value is None:
        return None
    try:
        number = int(value) & 0xFFFFFF
    except (TypeError, ValueError):
        return None
    return (number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF


def rgb_to_hex(rgb: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in rgb[:3]))


def relative_luminance(rgb: Sequence[int]) -> float:
    """Линейная яркость по WCAG, 0..1."""
    channels = []
    for raw in rgb[:3]:
        c = max(0, min(255, int(raw))) / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def is_transparent_or_useless(rgba: Sequence[int], alpha_threshold: int = 24) -> bool:
    """Пиксель прозрачный — в расчёте цвета не участвует."""
    return len(rgba) >= 4 and int(rgba[3]) < alpha_threshold


def hue_distance(a: float, b: float) -> float:
    """Кратчайшее расстояние по цветовому кругу, 0..180."""
    diff = abs((float(a) % 360.0) - (float(b) % 360.0))
    return min(diff, 360.0 - diff)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pct = max(0.0, min(100.0, pct)) / 100.0
    index = pct * (len(ordered) - 1)
    low = int(math.floor(index))
    high = int(math.ceil(index))
    if low == high:
        return float(ordered[low])
    weight = index - low
    return float(ordered[low]) * (1 - weight) + float(ordered[high]) * weight


def median(values: Sequence[float]) -> float:
    return percentile(values, 50.0)


@dataclass(frozen=True, slots=True)
class PaletteStats:
    """Сводка по набору цветов."""

    count: int
    mean_saturation: float
    p90_saturation: float
    max_saturation: float
    mean_value: float
    p10_value: float
    dominant_hue: float | None
    hue_spread: float | None       # средний разброс тонов вокруг доминанты, 0..180
    mean_luminance: float
    colors: tuple[RGB, ...] = ()

    @property
    def is_grayscale(self) -> bool:
        return self.max_saturation < 1e-6


def summarize_colors(colors: Iterable[RGB], *, weights: Iterable[float] | None = None) -> PaletteStats:
    """Посчитать статистику по набору RGB-цветов."""
    rows = [tuple(int(c) for c in color[:3]) for color in colors if color is not None]
    if not rows:
        return PaletteStats(
            count=0, mean_saturation=0.0, p90_saturation=0.0, max_saturation=0.0,
            mean_value=0.0, p10_value=0.0, dominant_hue=None, hue_spread=None,
            mean_luminance=0.0, colors=(),
        )

    weight_list = list(weights) if weights is not None else [1.0] * len(rows)
    if len(weight_list) != len(rows):
        weight_list = [1.0] * len(rows)
    total_weight = sum(weight_list) or 1.0

    hsv_rows = [rgb_to_hsv(row) for row in rows]
    saturations = [h.s for h in hsv_rows]
    values = [h.v for h in hsv_rows]

    mean_saturation = sum(s * w for s, w in zip(saturations, weight_list)) / total_weight
    mean_value = sum(v * w for v, w in zip(values, weight_list)) / total_weight
    mean_luminance = sum(relative_luminance(row) * w for row, w in zip(rows, weight_list)) / total_weight

    # Доминирующий тон — по взвешенным «цветным» пикселям (насыщенность > 5 %).
    chromatic = [(h, w) for h, w in zip(hsv_rows, weight_list) if h.s > 0.05]
    dominant_hue: float | None = None
    hue_spread: float | None = None
    if chromatic:
        dominant_hue = circular_mean([h.h for h, _ in chromatic], [w for _, w in chromatic])
        hue_spread = sum(
            hue_distance(h.h, dominant_hue) * w for h, w in chromatic
        ) / (sum(w for _, w in chromatic) or 1.0)

    return PaletteStats(
        count=len(rows),
        mean_saturation=mean_saturation,
        p90_saturation=percentile(saturations, 90.0),
        max_saturation=max(saturations),
        mean_value=mean_value,
        p10_value=percentile(values, 10.0),
        dominant_hue=dominant_hue,
        hue_spread=hue_spread,
        mean_luminance=mean_luminance,
        colors=tuple(rows[:12]),
    )


def circular_mean(angles: Sequence[float], weights: Sequence[float] | None = None) -> float:
    """Средний тон с учётом перехода 359° -> 1°."""
    if not angles:
        return 0.0
    weight_list = list(weights) if weights else [1.0] * len(angles)
    sin_sum = sum(math.sin(math.radians(a)) * w for a, w in zip(angles, weight_list))
    cos_sum = sum(math.cos(math.radians(a)) * w for a, w in zip(angles, weight_list))
    mean = math.degrees(math.atan2(sin_sum, cos_sum))
    return mean % 360.0


def ramp_down(value: float, threshold: float, ceiling: float) -> float:
    """1.0 при value <= threshold, линейно к 0.0 при value >= ceiling."""
    if ceiling <= threshold:
        return 1.0 if value <= threshold else 0.0
    if value <= threshold:
        return 1.0
    if value >= ceiling:
        return 0.0
    return 1.0 - (value - threshold) / (ceiling - threshold)


def ramp_up(value: float, threshold: float, ceiling: float) -> float:
    """0.0 при value <= threshold, линейно к 1.0 при value >= ceiling."""
    if ceiling <= threshold:
        return 0.0 if value < threshold else 1.0
    if value <= threshold:
        return 0.0
    if value >= ceiling:
        return 1.0
    return (value - threshold) / (ceiling - threshold)
