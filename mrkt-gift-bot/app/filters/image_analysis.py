"""Анализ изображения: преобладающие цвета, насыщенность, доля «цветных» пикселей.

Нужен, когда источник НЕ отдаёт цвета фона (или их недостаточно для вывода).
Работает на Pillow: декодирует байты, отбрасывает прозрачные пиксели и считает
статистику по HSV. Отдельно считает «полезные» пиксели — без почти чёрных и
почти белых, которые в подарках обычно являются обводкой/подложкой и сами по
себе не говорят о цветности арта.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field
from typing import Any

from app.filters.colors import (
    PaletteStats,
    RGB,
    summarize_colors,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ImageAnalysis:
    ok: bool
    width: int = 0
    height: int = 0
    pixels_total: int = 0
    pixels_opaque: int = 0
    pixels_art: int = 0
    stats_opaque: PaletteStats | None = None
    stats_art: PaletteStats | None = None
    dominant_colors: list[tuple[RGB, float]] = field(default_factory=list)
    colored_share: float = 0.0     # доля пикселей арта с насыщенностью > 0.2
    format: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def stats(self) -> PaletteStats | None:
        """Предпочитаем статистику по «арту», иначе — по всем непрозрачным."""
        return self.stats_art or self.stats_opaque


def _analyze_sync(
    data: bytes,
    *,
    max_pixels: int = 60_000,
    sample_stride_min: int = 1,
) -> ImageAnalysis:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
            width, height = image.size
            image = image.convert("RGBA")
            # Уменьшаем большие картинки: анализ не требует полного разрешения.
            if width * height > max_pixels:
                ratio = (max_pixels / float(width * height)) ** 0.5
                image = image.resize(
                    (max(1, int(width * ratio)), max(1, int(height * ratio))),
                    Image.BILINEAR,
                )
                width, height = image.size
            pixels = list(image.getdata())
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        return ImageAnalysis(ok=False, error=f"не удалось разобрать изображение: {exc}")

    opaque: list[RGB] = []
    art: list[RGB] = []
    colored = 0
    stride = max(1, sample_stride_min)
    total = 0

    for index, pixel in enumerate(pixels):
        if index % stride:
            continue
        total += 1
        alpha = pixel[3] if len(pixel) >= 4 else 255
        if alpha < 24:
            continue
        rgb = (int(pixel[0]), int(pixel[1]), int(pixel[2]))
        opaque.append(rgb)
        extreme = max(rgb)
        floor = min(rgb)
        # Почти чёрный или почти белый пиксель — служебный, не «арт».
        if extreme >= 248 or floor <= 6:
            continue
        art.append(rgb)
        if _saturation(rgb) > 0.20:
            colored += 1

    if not opaque:
        return ImageAnalysis(
            ok=False, width=width, height=height, pixels_total=total, format=fmt,
            error="изображение полностью прозрачное",
        )

    stats_opaque = summarize_colors(opaque)
    stats_art = summarize_colors(art) if art else None
    colored_share = (colored / len(art)) if art else 0.0

    # Преобладающие цвета: квантование до шага 32 и подсчёт долей.
    buckets: dict[tuple[int, int, int], int] = {}
    for rgb in art or opaque:
        key = (rgb[0] // 32 * 32, rgb[1] // 32 * 32, rgb[2] // 32 * 32)
        buckets[key] = buckets.get(key, 0) + 1
    pool = len(art) or len(opaque) or 1
    dominant = sorted(
        ((key, count / pool) for key, count in buckets.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:6]

    return ImageAnalysis(
        ok=True,
        width=width,
        height=height,
        pixels_total=total,
        pixels_opaque=len(opaque),
        pixels_art=len(art),
        stats_opaque=stats_opaque,
        stats_art=stats_art,
        dominant_colors=dominant,
        colored_share=colored_share,
        format=fmt,
    )


def _saturation(rgb: RGB) -> float:
    extreme = max(rgb)
    if extreme == 0:
        return 0.0
    return (extreme - min(rgb)) / extreme


async def analyze_image_bytes(
    data: bytes,
    *,
    timeout: float = 15.0,
    max_pixels: int = 60_000,
) -> ImageAnalysis:
    """Асинхронная обёртка: тяжёлая работа уходит в поток."""
    if not data:
        return ImageAnalysis(ok=False, error="пустые данные изображения")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_analyze_sync, data, max_pixels=max_pixels),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return ImageAnalysis(ok=False, error=f"анализ превысил {timeout:.0f} c")
    except Exception as exc:  # noqa: BLE001
        logger.debug("analyze_image_bytes: %r", exc)
        return ImageAnalysis(ok=False, error=repr(exc))
