"""Демонстрационный источник.

ЗАЧЕМ ОН НУЖЕН
--------------
Реальный API MRKT (`api.tgmrkt.io/api/v1`) — приватный: токен выдаётся только
после входа настоящего Telegram-аккаунта в мини-приложение @mrkt. Без валидного
токена эндпоинты данных отвечают HTTP 404, а `/auth` — HTTP 403 "Forbid 1"
(проверено 27.10.2026). Поэтому бот по умолчанию запускается на этом источнике,
чтобы всю логику (фильтры, монохром, выгода, ранжирование, дедупликация,
уведомления) можно было проверить без доступа к площадке.

ЧЕСТНОСТЬ ДАННЫХ
----------------
Каждый лот имеет `is_test_data=True`, а `SourceStatus.is_test_data=True`.
Бот обязан показывать это в карточках и в /status (см. app/bot/cards.py).
Все совпадения с реальными ценами случайны.

ФОРМАТ
------
Генератор намеренно повторяет форму ответа MRKT: те же поля и цены в nanoTON,
чтобы `MrktSource` и `DemoSource` были взаимозаменяемы.
"""

from __future__ import annotations

import hashlib
import random
from datetime import timedelta

from app.domain import (
    NANO,
    Confidence,
    FetchRequest,
    FetchResult,
    Listing,
    SourceKind,
    SourceStatus,
    int_to_rgb,
    utcnow,
)
from app.sources.base import ImageResult, ListingSource

# Названия коллекций и атрибутов вымышленные/общие — совпадения с реальными
# лотами MRKT случайны и не являются котировками.
DEMO_COLLECTIONS: dict[str, dict[str, object]] = {
    "Frost Fox": {
        "floor_ton": 42.0,
        "models": ["Alabaster", "Ember", "Noir", "Aurora", "Graphite"],
        "backdrops": ["Snow White", "Ash Grey", "Crimson", "Midnight", "Silver Mist"],
        "symbols": ["Snowflake", "Ember Spark", "Crescent", "Anchor"],
    },
    "Velvet Owl": {
        "floor_ton": 18.5,
        "models": ["Pearl", "Slate", "Copper", "Ivory", "Onyx"],
        "backdrops": ["Fog", "Charcoal", "Amber", "Pale Rose", "Ink"],
        "symbols": ["Feather", "Star", "Key", "Moon"],
    },
    "Paper Crane": {
        "floor_ton": 7.2,
        "models": ["Chalk", "Basalt", "Saffron", "Mint", "Ash"],
        "backdrops": ["Linen", "Granite", "Sky", "Sand", "Smoke"],
        "symbols": ["Wave", "Lotus", "Bolt", "Leaf"],
    },
    "Iron Whale": {
        "floor_ton": 120.0,
        "models": ["Steel", "Mono", "Cobalt", "Zinc", "Obsidian"],
        "backdrops": ["Deep Grey", "Black Glass", "Teal", "Bone", "Storm"],
        "symbols": ["Harpoon", "Compass", "Anchor", "Tide"],
    },
}

# Фоны, которые генератор делает заведомо «монохромными» (низкая насыщенность).
MONO_BACKDROPS = {
    "Snow White": (0xF4F4F4, 0xE2E2E2),
    "Ash Grey": (0x9A9A9A, 0x6E6E6E),
    "Charcoal": (0x3A3A3C, 0x222224),
    "Fog": (0xC9CCD1, 0xA9ADB4),
    "Linen": (0xEDE7DC, 0xD8D0C2),
    "Granite": (0x77787B, 0x55565A),
    "Smoke": (0x8C8F94, 0x63666B),
    "Deep Grey": (0x4A4E54, 0x2E3238),
    "Black Glass": (0x1A1A1C, 0x0C0C0D),
    "Bone": (0xE8E4DA, 0xCFC9BC),
    "Ink": (0x24262B, 0x14161A),
    "Silver Mist": (0xD6D9DE, 0xB4B8BF),
    "Pale Rose": (0xE9D9D6, 0xCBB4B0),
}

# Фоны с выраженной насыщенностью — заведомо НЕ монохромные.
COLOR_BACKDROPS = {
    "Crimson": (0xC0392B, 0x7B241C),
    "Midnight": (0x1F3A93, 0x14213D),
    "Amber": (0xE0A106, 0xA56F04),
    "Sky": (0x4FA3D1, 0x2E6E96),
    "Teal": (0x148F8F, 0x0B5F5F),
    "Sand": (0xD9B26A, 0xA9863F),
}


def _rng(seed: str) -> random.Random:
    """Детерминированный ГПСЧ от строки — чтобы лот был стабилен между циклами."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


class DemoSource(ListingSource):
    """Генератор тестовых лотов. Не обращается в сеть."""

    name = "demo"
    is_test_data = True

    def __init__(self, *, drift_seed: int = 0, catalogue_size: int = 140) -> None:
        self._drift_seed = drift_seed
        self._catalogue_size = catalogue_size
        self._requests_total = 0

    # ------------------------------------------------------------------ статус
    async def status(self) -> SourceStatus:
        self._requests_total += 1
        return SourceStatus(
            name=self.name,
            kind=SourceKind.DEMO,
            available=True,
            is_test_data=True,
            checked_at=utcnow(),
            message="Демонстрационный источник: данные сгенерированы локально и НЕ являются реальными лотами MRKT.",
            detail="Для реальных данных укажите SOURCE=mrkt и MRKT_TOKEN (см. README, раздел 5).",
            requires_token=False,
            requests_total=self._requests_total,
        )

    # ------------------------------------------------------------ справочники
    async def collections(self) -> list[str]:
        return list(DEMO_COLLECTIONS)

    async def models(self, collection: str) -> list[str]:
        meta = DEMO_COLLECTIONS.get(collection)
        return list(meta["models"]) if meta else []

    async def backdrops(self, collection: str) -> list[str]:
        meta = DEMO_COLLECTIONS.get(collection)
        return list(meta["backdrops"]) if meta else []

    async def symbols(self, collection: str) -> list[str]:
        meta = DEMO_COLLECTIONS.get(collection)
        return list(meta["symbols"]) if meta else []

    # ------------------------------------------------------------------ данные
    def _build_catalogue(self) -> list[Listing]:
        """Полный каталог демо-лотов (стабилен в рамках одного drift_seed)."""
        listings: list[Listing] = []
        now = utcnow()
        index = 0
        for collection, meta in DEMO_COLLECTIONS.items():
            models = list(meta["models"])          # type: ignore[arg-type]
            backdrops = list(meta["backdrops"])    # type: ignore[arg-type]
            symbols = list(meta["symbols"])        # type: ignore[arg-type]
            floor_ton = float(meta["floor_ton"])   # type: ignore[arg-type]
            per_collection = max(8, self._catalogue_size // len(DEMO_COLLECTIONS))
            for i in range(per_collection):
                index += 1
                seed = f"{self._drift_seed}:{collection}:{i}"
                rnd = _rng(seed)
                model = models[rnd.randrange(len(models))]
                backdrop = backdrops[rnd.randrange(len(backdrops))]
                symbol = symbols[rnd.randrange(len(symbols))]

                # Редкость в ‰: чем меньше, тем реже.
                model_rarity = rnd.choice([5, 10, 20, 40, 80, 150, 300])
                backdrop_rarity = rnd.choice([10, 20, 50, 100, 200, 400])
                symbol_rarity = rnd.choice([2, 5, 10, 25, 60, 120])

                rarity_multiplier = 1.0 + (300 - min(model_rarity, 300)) / 220.0
                noise = rnd.uniform(0.78, 1.45)
                price_ton = max(0.5, floor_ton * rarity_multiplier * noise)
                price_nano = int(round(price_ton * NANO))
                # Комиссия площадки ~5 %: брутто = нетто * 1.05.
                net_nano = int(round(price_nano / 1.05))

                if backdrop in MONO_BACKDROPS:
                    center, edge = MONO_BACKDROPS[backdrop]
                elif backdrop in COLOR_BACKDROPS:
                    center, edge = COLOR_BACKDROPS[backdrop]
                else:
                    center = rnd.randrange(0x222222, 0xDDDDDD)
                    edge = rnd.randrange(0x111111, 0xCCCCCC)

                age_hours = rnd.choice([0.2, 1.0, 3.0, 8.0, 20.0, 48.0, 96.0, 240.0])
                listing = Listing(
                    id=f"demo-{index:05d}",
                    source=self.name,
                    collection_name=collection,
                    title=f"{collection} · {model}",
                    number=rnd.randrange(1, 900),
                    model_name=model,
                    backdrop_name=backdrop,
                    symbol_name=symbol,
                    model_rarity_permille=model_rarity,
                    backdrop_rarity_permille=backdrop_rarity,
                    symbol_rarity_permille=symbol_rarity,
                    price_nano=price_nano,
                    price_without_fee_nano=net_nano,
                    currency="TON",
                    floor_by_collection_nano=int(round(floor_ton * NANO)),
                    # Ориентир по атрибутам иногда отсутствует — как в реальном API.
                    floor_by_attributes_nano=(
                        int(round(floor_ton * rarity_multiplier * NANO)) if rnd.random() < 0.55 else None
                    ),
                    backdrop_center_color=center,
                    backdrop_edge_color=edge,
                    backdrop_symbol_color=_shift(center, rnd),
                    backdrop_text_color=0xFFFFFF,
                    image_url=f"demo://{collection}/{model}.png",
                    url=None,
                    listed_at=now - timedelta(hours=age_hours),
                    is_active=True,
                    is_test_data=True,
                    extra={"demo_seed": seed},
                )
                listings.append(listing)
        return listings

    async def fetch(self, request: FetchRequest) -> FetchResult:
        self._requests_total += 1
        catalogue = self._build_catalogue()
        rnd = _rng(f"fetch:{self._drift_seed}:{request.query or ''}")

        wanted = {c.strip().lower() for c in request.collections if c and c.strip()}
        rows = [
            item
            for item in catalogue
            if not wanted or item.collection_name.strip().lower() in wanted
        ]

        # Серверные фильтры источника (MRKT умеет то же самое на своей стороне).
        if request.min_price_nano is not None:
            rows = [r for r in rows if r.price_nano >= request.min_price_nano]
        if request.max_price_nano is not None:
            rows = [r for r in rows if r.price_nano <= request.max_price_nano]
        if request.models:
            pool = {m.strip().lower() for m in request.models}
            rows = [r for r in rows if (r.model_name or "").strip().lower() in pool]
        if request.backdrops:
            pool = {b.strip().lower() for b in request.backdrops}
            rows = [r for r in rows if (r.backdrop_name or "").strip().lower() in pool]
        if request.symbols:
            pool = {s.strip().lower() for s in request.symbols}
            rows = [r for r in rows if (r.symbol_name or "").strip().lower() in pool]
        if request.query:
            needle = request.query.strip().lower()
            rows = [
                r
                for r in rows
                if needle in r.collection_name.lower()
                or needle in (r.model_name or "").lower()
                or needle in (r.title or "").lower()
            ]

        if request.ordering == "Price":
            rows.sort(key=lambda r: r.price_nano, reverse=not request.low_to_high)
        elif request.ordering in ("ModelRarity", "BackgroundRarity", "SymbolRarity"):
            key = {
                "ModelRarity": lambda r: r.model_rarity_permille or 1000,
                "BackgroundRarity": lambda r: r.backdrop_rarity_permille or 1000,
                "SymbolRarity": lambda r: r.symbol_rarity_permille or 1000,
            }[request.ordering]
            rows.sort(key=key, reverse=not request.low_to_high)
        else:
            rows.sort(key=lambda r: r.listed_at or utcnow(), reverse=True)

        truncated = len(rows) > request.limit
        rows = rows[: max(1, request.limit)]

        # Небольшой «дрейф» цен между циклами, чтобы дедупликация была заметна.
        for row in rows:
            if rnd.random() < 0.12:
                factor = rnd.choice([0.86, 0.92, 0.97, 1.04, 1.11])
                row.price_nano = max(NANO // 10, int(round(row.price_nano * factor)))
                row.price_without_fee_nano = int(round(row.price_nano / 1.05))

        return FetchResult(
            listings=rows,
            truncated=truncated,
            is_test_data=True,
            complete=not truncated,
        )

    async def get_image(self, listing: Listing) -> ImageResult | None:
        """Рисуем картинку локально (Pillow), чтобы карточки были с фото.

        Изображение намеренно соответствует цветам фона лота — так демо
        позволяет проверить и анализ картинки, а не только цвета из API.
        """
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover - Pillow указан в requirements
            return ImageResult(data=b"", error="Pillow не установлен")

        import io

        rnd = _rng(f"img:{listing.id}")
        size = 256
        center = int_to_rgb(listing.backdrop_center_color) or (128, 128, 128)
        edge = int_to_rgb(listing.backdrop_edge_color) or (64, 64, 64)

        image = Image.new("RGB", (size, size), edge)
        draw = ImageDraw.Draw(image)
        draw.ellipse((size * 0.12, size * 0.12, size * 0.88, size * 0.88), fill=center)
        # «Силуэт» модели — просто геометрическая фигура, цвет зависит от модели.
        model_seed = hashlib.sha256((listing.model_name or "x").encode()).hexdigest()
        body_rnd = random.Random(int(model_seed[:12], 16))
        body_color = tuple(body_rnd.randrange(40, 235) for _ in range(3))
        shape = rnd.choice(("rect", "triangle", "ellipse"))
        box = (size * 0.32, size * 0.34, size * 0.68, size * 0.72)
        if shape == "rect":
            draw.rectangle(box, fill=body_color)
        elif shape == "triangle":
            draw.triangle(
                [(box[0], box[3]), ((box[0] + box[2]) / 2, box[1]), (box[2], box[3])],
                fill=body_color,
            )
        else:
            draw.ellipse(box, fill=body_color)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        return ImageResult(
            data=buffer.getvalue(),
            content_type="image/jpeg",
            url=listing.image_url,
            origin="demo",
        )


def _shift(color: int, rnd: random.Random) -> int:
    """Слегка сдвинуть цвет символа относительно центра фона."""
    rgb = int_to_rgb(color) or (128, 128, 128)
    shifted = tuple(max(0, min(255, c + rnd.randrange(-28, 29))) for c in rgb)
    return (shifted[0] << 16) | (shifted[1] << 8) | shifted[2]


def self_check() -> dict[str, object]:
    """Быстрая офлайн-проверка демо-источника (используется тестами и /diag)."""
    import asyncio

    async def _run() -> dict[str, object]:
        source = DemoSource()
        status = await source.status()
        result = await source.fetch(FetchRequest(limit=10))
        mono = [r for r in result.listings if (r.backdrop_name or "") in MONO_BACKDROPS]
        return {
            "available": status.available,
            "is_test_data": status.is_test_data,
            "fetched": len(result.listings),
            "with_mono_backdrop": len(mono),
            "confidence": Confidence.HIGH.value,
        }

    return asyncio.run(_run())
