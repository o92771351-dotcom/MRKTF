"""Общие фикстуры тестов.

Все тесты офлайн: сеть не требуется (демо-источник и синтетические картинки).
Для детектора монохромности подменяется клиент @GiftChanges заглушкой.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db.db import Database  # noqa: E402
from app.db.repo import Repository  # noqa: E402
from app.domain import Listing, utcnow  # noqa: E402
from app.filters.monochrome import MonochromeDetector  # noqa: E402
from app.pipeline import PipelineContext  # noqa: E402
from app.sources.base import ImageResult  # noqa: E402
from app.sources.demo import DemoSource  # noqa: E402
from app.sources.factory import ImageProvider  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token="test:token",
        source="demo",
        database_url="sqlite+aiosqlite:///:memory:",
        data_dir="./data",
        cache_dir="./data/cache",
        changes_api_enabled=False,   # тесты не ходят в сеть
        mrkt_request_delay=0.0,
        poll_tick_seconds=5,
        value_min_comparables=3,
        resend_price_drop_percent=5.0,
        resend_after_hours=24,
        log_level="WARNING",
    )


@pytest_asyncio.fixture
async def db(settings: Settings):
    database = Database(settings.database_url, echo=False)
    await database.create_all()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def repo(db: Database):
    async with db.session_factory() as session:
        yield Repository(session)


@pytest.fixture
def demo_source() -> DemoSource:
    return DemoSource()


class StubChanges:
    """Заглушка @GiftChanges: отдаёт заранее заданные цвета и редкости."""

    def __init__(
        self,
        colors: dict[tuple[str, str], dict[str, Any]] | None = None,
        rarities: dict[str, dict[str, dict[str, float]]] | None = None,
    ) -> None:
        self.colors = colors or {}
        self.rarities = rarities or {}
        self.calls: list[str] = []

    async def backdrop_colors(self, collection: str, backdrop: str) -> dict[str, Any]:
        self.calls.append(f"colors:{collection}/{backdrop}")
        return self.colors.get((collection.lower(), backdrop.lower()), {})

    async def rarities(self, collection: str) -> dict[str, dict[str, float]]:
        self.calls.append(f"rarities:{collection}")
        return self.rarities.get(collection.lower(), {})

    async def model_png(self, collection: str, model: str, size: int = 256) -> ImageResult | None:
        self.calls.append(f"png:{collection}/{model}")
        return None

    async def original_png(self, collection: str, size: int = 256) -> ImageResult | None:
        self.calls.append(f"original:{collection}")
        return None

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "data": {}}

    async def close(self) -> None:
        return None


@pytest.fixture
def stub_changes() -> StubChanges:
    return StubChanges()


@pytest.fixture
def detector(stub_changes: StubChanges, settings: Settings) -> MonochromeDetector:
    return MonochromeDetector(stub_changes, image_timeout=settings.mono_image_analysis_timeout)


@pytest.fixture
def images(demo_source: DemoSource, settings: Settings) -> ImageProvider:
    return ImageProvider(demo_source, None, settings=settings)


@pytest.fixture
def ctx(settings: Settings, demo_source: DemoSource, images: ImageProvider, detector: MonochromeDetector):
    return PipelineContext(settings=settings, source=demo_source, images=images, detector=detector)


def make_listing(**kwargs: Any) -> Listing:
    """Лот с разумными значениями по умолчанию для юнит-тестов."""
    base: dict[str, Any] = dict(
        id="lot-1",
        source="demo",
        collection_name="Frost Fox",
        title="Frost Fox · Noir",
        number=7,
        model_name="Noir",
        backdrop_name="Ash Grey",
        symbol_name="Snowflake",
        price_nano=50_000_000_000,
        price_without_fee_nano=47_619_047_619,
        currency="TON",
        listed_at=utcnow(),
        is_active=True,
        is_test_data=True,
    )
    base.update(kwargs)
    return Listing(**base)


def make_png(color: tuple[int, int, int], size: int = 64, accent: tuple[int, int, int] | None = None) -> bytes:
    """Однотонный PNG (опционально с цветным пятном) для проверки анализа."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (*color, 255))
    if accent is not None:
        draw = ImageDraw.Draw(image)
        draw.rectangle((size // 4, size // 4, size * 3 // 4, size * 3 // 4), fill=(*accent, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
