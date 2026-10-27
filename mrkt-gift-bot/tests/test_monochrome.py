"""Тесты определения монохромности.

Проверяются все четыре способа и их приоритеты, а также ключевое требование:
по названию монохромность НЕ определяется с высокой уверенностью.
"""

from __future__ import annotations

import pytest

from app.domain import Confidence, Listing
from app.filters.image_analysis import analyze_image_bytes
from app.filters.monochrome import (
    MonochromeDetector,
    MonochromeSpec,
    combine,
    name_heuristic,
    passes,
)
from tests.conftest import StubChanges, make_listing, make_png

GRAY = (140, 140, 140)
DARK_GRAY = (30, 30, 32)
LIGHT_GRAY = (238, 238, 238)
RED = (200, 40, 40)
BLUE = (40, 90, 200)


def int_color(rgb: tuple[int, int, int]) -> int:
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]


# ----------------------------------------------------------------------------
#  Способ 1: цвета фона из ответа источника
# ----------------------------------------------------------------------------

async def test_gray_backdrop_colors_detected_with_high_confidence(detector: MonochromeDetector) -> None:
    listing = make_listing(
        backdrop_center_color=int_color(GRAY),
        backdrop_edge_color=int_color(DARK_GRAY),
        backdrop_symbol_color=int_color((110, 110, 112)),
        backdrop_text_color=int_color((250, 250, 250)),
    )
    result = await detector.assess(listing, MonochromeSpec(enabled=True, mode="gray", min_score=60))
    assert result.score >= 90
    assert result.confidence == Confidence.HIGH
    assert "backdrop_colors" in result.method


async def test_saturated_backdrop_colors_are_not_monochrome(detector: MonochromeDetector) -> None:
    listing = make_listing(
        backdrop_center_color=int_color(RED),
        backdrop_edge_color=int_color(BLUE),
        backdrop_symbol_color=int_color((240, 180, 20)),
        backdrop_text_color=int_color((255, 255, 255)),
    )
    result = await detector.assess(listing, MonochromeSpec(enabled=True, mode="gray", min_score=60))
    assert result.score < 40
    ok, reason = passes(result, MonochromeSpec(enabled=True, mode="gray", min_score=60))
    assert ok is False and "балл монохромности" in reason


async def test_dark_mode_requires_low_brightness(detector: MonochromeDetector) -> None:
    dark = make_listing(
        id="dark",
        backdrop_center_color=int_color(DARK_GRAY),
        backdrop_edge_color=int_color((12, 12, 14)),
    )
    light = make_listing(
        id="light",
        backdrop_center_color=int_color(LIGHT_GRAY),
        backdrop_edge_color=int_color((250, 250, 250)),
    )
    spec = MonochromeSpec(enabled=True, mode="dark", min_score=60)
    dark_result = await detector.assess(dark, spec)
    light_result = await detector.assess(light, spec)
    assert dark_result.score > light_result.score
    assert passes(dark_result, spec)[0] is True
    assert passes(light_result, spec)[0] is False


async def test_light_mode_is_opposite_of_dark(detector: MonochromeDetector) -> None:
    dark = make_listing(
        id="dark",
        backdrop_center_color=int_color(DARK_GRAY),
        backdrop_edge_color=int_color((12, 12, 14)),
    )
    light = make_listing(
        id="light",
        backdrop_center_color=int_color(LIGHT_GRAY),
        backdrop_edge_color=int_color((250, 250, 250)),
    )
    spec = MonochromeSpec(enabled=True, mode="light", min_score=60)
    assert passes(await detector.assess(light, spec), spec)[0] is True
    assert passes(await detector.assess(dark, spec), spec)[0] is False


async def test_custom_hue_mode_matches_target_color(detector: MonochromeDetector) -> None:
    blue_listing = make_listing(
        id="blue",
        backdrop_center_color=int_color((40, 90, 200)),
        backdrop_edge_color=int_color((30, 70, 170)),
        backdrop_symbol_color=int_color((50, 100, 210)),
    )
    red_listing = make_listing(
        id="red",
        backdrop_center_color=int_color((200, 40, 40)),
        backdrop_edge_color=int_color((170, 30, 30)),
        backdrop_symbol_color=int_color((210, 50, 50)),
    )
    spec = MonochromeSpec(
        enabled=True, mode="custom", custom_hue=215.0, custom_hue_tolerance=25.0, min_score=60
    )
    assert passes(await detector.assess(blue_listing, spec), spec)[0] is True
    assert passes(await detector.assess(red_listing, spec), spec)[0] is False


# ----------------------------------------------------------------------------
#  Способ 2: цвета из публичного API @GiftChanges
# ----------------------------------------------------------------------------

async def test_changes_api_colors_used_when_source_has_none() -> None:
    changes = StubChanges(
        colors={
            ("frost fox", "fog"): {
                "center": (201, 204, 209),
                "edge": (169, 173, 180),
                "pattern": (120, 124, 130),
                "text": (245, 246, 248),
            }
        }
    )
    detector = MonochromeDetector(changes)
    listing = make_listing(backdrop_name="Fog", backdrop_center_color=None,
                           backdrop_edge_color=None, backdrop_symbol_color=None,
                           backdrop_text_color=None)
    result = await detector.assess(listing, MonochromeSpec(enabled=True, mode="gray"))
    assert result.score >= 90
    assert result.confidence == Confidence.HIGH
    assert any(e.method == "changes_api_colors" for e in result.evidences)
    assert "colors:Frost Fox/Fog" in changes.calls


# ----------------------------------------------------------------------------
#  Способ 3: анализ изображения
# ----------------------------------------------------------------------------

async def test_image_analysis_detects_grayscale() -> None:
    analysis = await analyze_image_bytes(make_png(GRAY, accent=(90, 90, 90)))
    assert analysis.ok
    assert analysis.stats is not None
    assert analysis.stats.mean_saturation < 0.02
    assert analysis.colored_share == 0.0


async def test_image_analysis_detects_color() -> None:
    analysis = await analyze_image_bytes(make_png(GRAY, accent=RED))
    assert analysis.ok
    assert analysis.colored_share > 0.2
    assert analysis.stats is not None and analysis.stats.mean_saturation > 0.05


async def test_image_used_only_when_colors_missing() -> None:
    """Экономим трафик: при достаточных цветах картинка не запрашивается."""
    fetched: list[str] = []

    async def fetch_image(listing: Listing):
        fetched.append(listing.id)
        from app.sources.base import ImageResult

        return ImageResult(data=make_png(GRAY), content_type="image/png", origin="test")

    detector = MonochromeDetector(None)
    with_colors = make_listing(id="with-colors", backdrop_center_color=int_color(GRAY),
                               backdrop_edge_color=int_color(DARK_GRAY))
    result = await detector.assess(
        with_colors, MonochromeSpec(enabled=True, mode="gray", min_score=60), fetch_image=fetch_image
    )
    assert fetched == []          # картинка не понадобилась
    assert result.score >= 90

    without_colors = make_listing(id="no-colors", backdrop_center_color=None,
                                  backdrop_edge_color=None, backdrop_symbol_color=None,
                                  backdrop_text_color=None, title="Something 12345")
    result2 = await detector.assess(
        without_colors, MonochromeSpec(enabled=True, mode="gray", min_score=60),
        fetch_image=fetch_image,
    )
    assert fetched == ["no-colors"]
    assert any(e.method == "image" for e in result2.evidences)
    assert result2.score >= 80


async def test_image_evidence_confidence_is_not_high() -> None:
    analysis = await analyze_image_bytes(make_png(GRAY))
    detector = MonochromeDetector(None)
    evidence = detector._image_evidence(analysis, MonochromeSpec(enabled=True, mode="gray"))
    assert evidence.confidence in (Confidence.MEDIUM, Confidence.LOW)
    assert evidence.confidence != Confidence.HIGH


async def test_tiny_image_lowers_confidence() -> None:
    from PIL import Image
    import io

    tiny = Image.new("RGBA", (8, 8), (*GRAY, 255))
    buffer = io.BytesIO()
    tiny.save(buffer, format="PNG")
    analysis = await analyze_image_bytes(buffer.getvalue())
    detector = MonochromeDetector(None)
    evidence = detector._image_evidence(analysis, MonochromeSpec(enabled=True, mode="gray"))
    assert evidence.confidence == Confidence.LOW
    assert evidence.score < 1.0        # штраф за малое количество данных


async def test_broken_image_does_not_crash() -> None:
    analysis = await analyze_image_bytes(b"not an image at all")
    assert analysis.ok is False and analysis.error


# ----------------------------------------------------------------------------
#  Способ 4: эвристика по названию — только low confidence
# ----------------------------------------------------------------------------

def test_name_heuristic_never_high_confidence() -> None:
    listing = make_listing(model_name="Monochrome Noir", backdrop_name="Grayscale Ash")
    evidence = name_heuristic(listing, MonochromeSpec(mode="gray"))
    assert evidence is not None
    assert evidence.confidence == Confidence.LOW
    assert evidence.score <= 0.45


def test_name_heuristic_penalizes_chromatic_words() -> None:
    mono = name_heuristic(make_listing(model_name="Noir"), MonochromeSpec(mode="gray"))
    chromatic = name_heuristic(
        make_listing(model_name="Noir Rainbow Aurora"), MonochromeSpec(mode="gray")
    )
    assert mono is not None and chromatic is not None
    assert chromatic.score < mono.score


def test_name_only_assessment_is_low_confidence_and_fails_strict_filter() -> None:
    from app.filters.monochrome import MonochromeEvidence

    evidence = MonochromeEvidence(method="name", score=0.45, confidence=Confidence.LOW)
    assessment = combine([evidence], MonochromeSpec(mode="gray", min_score=60))
    assert assessment.confidence == Confidence.LOW
    assert assessment.method == "name"

    strict = MonochromeSpec(enabled=True, mode="gray", min_score=60, require_confidence="medium")
    ok, reason = passes(assessment, strict)
    assert ok is False
    assert "уверенность" in reason


async def test_detector_marks_name_only_result_as_low(detector: MonochromeDetector) -> None:
    listing = Listing(
        id="nameless",
        source="demo",
        collection_name="Test",
        title="Test Mono",
        model_name="Mono",
        backdrop_name=None,
        price_nano=1_000_000_000,
    )
    result = await detector.assess(listing, MonochromeSpec(enabled=True, mode="gray", min_score=10))
    assert result.method == "name"
    assert result.confidence == Confidence.LOW


# ----------------------------------------------------------------------------
#  Комбинирование и пороги
# ----------------------------------------------------------------------------

def test_combine_weights_by_confidence() -> None:
    from app.filters.monochrome import MonochromeEvidence

    high = MonochromeEvidence(method="backdrop_colors", score=0.9, confidence=Confidence.HIGH)
    low = MonochromeEvidence(method="name", score=0.1, confidence=Confidence.LOW)
    combined = combine([high, low], MonochromeSpec(mode="gray"))
    # (0.9*1.0 + 0.1*0.3) / 1.3 = 0.7153... -> 71.5
    assert 70.0 < combined.score < 73.0
    assert combined.confidence == Confidence.HIGH
    assert combined.method == "combined"


def test_combine_without_evidence_returns_none_confidence() -> None:
    assessment = combine([], MonochromeSpec(mode="gray"))
    assert assessment.confidence == Confidence.NONE
    assert assessment.score == 0.0
    ok, reason = passes(assessment, MonochromeSpec(enabled=True, min_score=1))
    assert ok is False and "нет данных" in reason


def test_disabled_spec_always_passes() -> None:
    assessment = combine([], MonochromeSpec(mode="gray"))
    ok, reason = passes(assessment, MonochromeSpec(enabled=False))
    assert ok is True and reason == ""


def test_spec_describe_is_human_readable() -> None:
    text = MonochromeSpec(enabled=True, mode="custom", custom_hue=210.0,
                          custom_hue_tolerance=20.0, min_score=70).describe()
    assert "210" in text and "70" in text
