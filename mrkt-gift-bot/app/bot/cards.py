"""Формирование карточки лота для Telegram (HTML).

Карточка обязана показывать: фото (если доступно), название и коллекцию, цену,
атрибуты, оценку монохромности со способом и уверенностью, ориентир цены и
разницу, оценку релевантности и кнопку «Открыть предложение».

Отдельно и всегда явно:
* признак ТЕСТОВЫХ данных (демо-источник);
* ненадёжность оценки выгоды, если аналогов мало;
* оговорка, что оценка — не гарантия прибыли.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

from app.bot.keyboards import listing_card_button
from app.config import Settings
from app.domain import Confidence, ScoredListing, fmt_ton, nano_to_ton
from app.ranking import explain as explain_ranking
from app.sources.base import ImageResult
from app.sources.factory import ImageProvider, convert_for_telegram
from app.sources.mrkt import build_link

logger = logging.getLogger(__name__)

CONFIDENCE_LABEL = {
    Confidence.HIGH.value: "высокая",
    Confidence.MEDIUM.value: "средняя",
    Confidence.LOW.value: "низкая",
    Confidence.NONE.value: "нет данных",
}

REASON_LABEL = {
    "new": "новый лот",
    "price_drop": "снижение цены",
    "returned": "лот снова в продаже",
    "changed": "изменились атрибуты",
}


@dataclass(slots=True)
class PreparedCard:
    text: str
    photo: bytes | None
    keyboard: InlineKeyboardMarkup
    caption_limit: int = 1024
    text_limit: int = 4096


def _rarity_text(permille: int | None) -> str:
    if not permille or permille <= 0:
        return ""
    return f"{permille / 10.0:g} %"


def _attributes_block(scored: ScoredListing) -> list[str]:
    listing = scored.listing
    rows: list[str] = []
    pairs = (
        ("Модель", listing.model_name, _rarity_text(listing.model_rarity_permille)),
        ("Фон", listing.backdrop_name, _rarity_text(listing.backdrop_rarity_permille)),
        ("Символ", listing.symbol_name, _rarity_text(listing.symbol_rarity_permille)),
    )
    for label, value, rarity in pairs:
        if not value:
            continue
        suffix = f" · редкость {rarity}" if rarity else ""
        rows.append(f"• {label}: <b>{escape(str(value))}</b>{suffix}")
    if listing.number is not None:
        rows.append(f"• Номер: <b>#{listing.number}</b>")
    return rows


def _monochrome_block(scored: ScoredListing) -> list[str]:
    mono = scored.monochrome
    if mono is None or mono.confidence == Confidence.NONE:
        return ["🎨 <b>Монохромность:</b> данных о цвете нет — не определялась"]
    confidence = CONFIDENCE_LABEL.get(mono.confidence.value, mono.confidence.value)
    lines = [
        f"🎨 <b>Монохромность:</b> {mono.score:.0f}/100 "
        f"(способ: {escape(mono.describe_method())}, уверенность: {confidence})"
    ]
    palette = mono.palette or {}
    for key, label in (
        ("backdrop_colors", "цвета фона API"),
        ("changes_api_colors", "цвета @GiftChanges"),
        ("image", "анализ картинки"),
        ("name", "эвристика по названию"),
    ):
        stats = palette.get(key)
        if isinstance(stats, dict):
            lines.append(
                f"  ↳ {label}: насыщенность {stats.get('mean_saturation', 0):.3f}, "
                f"яркость {stats.get('mean_value', 0):.3f}"
            )
    colors = palette.get("image_colors")
    if colors:
        preview = ", ".join(
            f"{item.get('hex')} ({item.get('share', 0) * 100:.0f} %)" for item in colors[:3]
        )
        lines.append(f"  ↳ преобладающие цвета: {preview}")
    hexes = palette.get("hex")
    if hexes:
        lines.append("  ↳ цвета фона: " + ", ".join(str(h) for h in hexes[:4]))
    if mono.confidence == Confidence.LOW and mono.method in ("name", "combined"):
        lines.append("  ⚠️ низкая уверенность: вывод сделан без надёжных данных о цвете")
    return lines


def _value_block(scored: ScoredListing, settings: Settings) -> list[str]:
    value = scored.value
    if value is None or value.benchmark_nano is None:
        note = value.note if value else "ориентир не определён"
        return [f"📊 <b>Выгода:</b> не рассчитана — {escape(note)}"]

    percent = value.diff_percent or 0.0
    if percent >= 0:
        arrow = f"−{percent:.1f} % к ориентиру"
    else:
        arrow = f"+{abs(percent):.1f} % к ориентиру"

    lines = [
        "📊 <b>Выгода:</b>",
        f"  • ориентир: <b>{fmt_ton(value.benchmark_nano)} TON</b> "
        f"({escape(value.describe_benchmark())})",
        f"  • разница: <b>{arrow}</b> ({fmt_ton(abs(value.diff_nano or 0))} TON)",
        f"  • аналогов в сравнении: <b>{value.comparables}</b> "
        f"(минимум {value.min_comparables})",
    ]
    confidence = CONFIDENCE_LABEL.get(value.confidence.value, value.confidence.value)
    lines.append(f"  • надёжность оценки: <b>{confidence}</b>")
    if value.fee_permille:
        lines.append(
            f"  • комиссия площадки в цене: ~{value.fee_permille / 10.0:g} % "
            f"(выведена из данных лота)"
        )
    if value.comparables < value.min_comparables:
        lines.append("  ⚠️ аналогов мало — оценка ненадёжна")
    return lines


def _relevance_block(scored: ScoredListing) -> list[str]:
    ranking = scored.ranking
    labels = {
        "match": "фильтры",
        "monochrome": "монохром",
        "value": "выгода",
        "confidence": "надёжность",
        "freshness": "свежесть",
    }
    parts = []
    total = sum(ranking.used_weights.values()) or 1.0
    for key in ("match", "monochrome", "value", "confidence", "freshness"):
        if key not in ranking.components:
            continue
        share = ranking.used_weights.get(key, 0.0) / total * 100.0
        parts.append(f"{labels[key]} {ranking.components[key] * 100:.0f}×{share:.0f}%")
    return [
        f"⭐ <b>Релевантность:</b> <b>{ranking.score:.1f}</b>/100",
        "  • " + " · ".join(parts) if parts else "",
        f"  • совпадение с фильтром: {escape(scored.match.describe())}",
    ]


def build_card_text(
    scored: ScoredListing,
    *,
    settings: Settings,
    meta: dict[str, Any] | None = None,
    compact: bool = False,
) -> str:
    """Полный текст карточки (для отправки сообщением, лимит 4096)."""
    meta = meta or {}
    listing = scored.listing
    lines: list[str] = []

    if listing.is_test_data or meta.get("is_test_data"):
        lines += ["⚠️ <b>ТЕСТОВЫЕ ДАННЫЕ</b> — лот сгенерирован демо-источником, это не реальный MRKT.", ""]

    reason = REASON_LABEL.get(str(meta.get("reason") or "new"), "")
    header = f"🎁 <b>{escape(listing.display_title)}</b>"
    if reason and str(meta.get("reason")) != "new":
        header += f"  ·  {reason}"
    lines.append(header)
    lines.append(f"Коллекция: <b>{escape(listing.collection_name)}</b>")
    if listing.listed_at:
        lines.append(f"Выставлен: {listing.listed_at.strftime('%d.%m.%Y %H:%M UTC')}")
    if meta.get("filter_name"):
        lines.append(f"Фильтр: {escape(str(meta['filter_name']))}")

    lines.append("")
    price_line = f"💰 <b>Цена:</b> <b>{fmt_ton(listing.price_nano)} {escape(listing.currency or 'TON')}</b>"
    if listing.price_without_fee_nano:
        price_line += f" (без комиссии {fmt_ton(listing.price_without_fee_nano)})"
    lines.append(price_line)

    attributes = _attributes_block(scored)
    if attributes:
        lines += attributes

    if not compact:
        lines.append("")
        lines += _monochrome_block(scored)
        lines.append("")
        lines += _value_block(scored, settings)
        lines.append("")
        relevance = [line for line in _relevance_block(scored) if line]
        lines += relevance

    lines.append("")
    link = build_link(listing, settings.mrkt_link_template)
    if listing.url or "{" in settings.mrkt_link_template:
        lines.append(f"🔗 {escape(link)}")
    else:
        lines.append(
            f"🔗 Кнопка ниже открывает приложение MRKT: публичная ссылка на конкретный "
            f"лот не подтверждена. Задайте формат в <code>MRKT_LINK_TEMPLATE</code>."
        )
        lines.append(f"   ID лота: <code>{escape(str(listing.id))}</code>")

    if not compact:
        lines.append("")
        lines.append(
            "<i>Оценка выгоды — сравнение с текущими ценами предложения, "
            "не гарантия прибыли.</i>"
        )

    text = "\n".join(lines)
    return text[:4000]


def build_caption(
    scored: ScoredListing, *, settings: Settings, meta: dict[str, Any] | None = None
) -> str:
    """Короткая подпись под фото (лимит Telegram для caption — 1024 символа).

    Если название слишком длинное, обрезаем именно его: цена, монохром,
    ориентир и релевантность должны остаться в подписи всегда.
    """
    meta = meta or {}
    limit = 1000
    caption = _caption_text(scored, meta, title_limit=None)
    if len(caption) <= limit:
        return caption

    # Бинарный поиск максимальной длины названия, при которой влезает всё.
    low, high = 0, len(scored.listing.display_title)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = _caption_text(scored, meta, title_limit=mid)
        if len(candidate) <= limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best or _caption_text(scored, meta, title_limit=0)[:limit]


def _caption_text(scored: ScoredListing, meta: dict[str, Any], title_limit: int | None) -> str:
    listing = scored.listing
    title = listing.display_title
    if title_limit is not None:
        title = title[:title_limit].rstrip()
        if len(listing.display_title) > len(title):
            title += "…"
    lines: list[str] = []
    if listing.is_test_data or meta.get("is_test_data"):
        lines.append("⚠️ ТЕСТОВЫЕ ДАННЫЕ (демо-источник)")
    reason = REASON_LABEL.get(str(meta.get("reason") or "new"), "")
    if reason and str(meta.get("reason")) != "new":
        lines.append(reason)
    lines.append(f"🎁 {escape(title)} · {escape(listing.collection_name)}")
    lines.append(f"💰 {fmt_ton(listing.price_nano)} {escape(listing.currency or 'TON')}")

    mono = scored.monochrome
    if mono is not None and mono.confidence != Confidence.NONE:
        confidence = CONFIDENCE_LABEL.get(mono.confidence.value, mono.confidence.value)
        lines.append(
            f"🎨 монохром {mono.score:.0f}/100 · {escape(mono.describe_method())} · {confidence}"
        )
    else:
        lines.append("🎨 монохром: данных о цвете нет")

    value = scored.value
    if value is not None and value.benchmark_nano is not None:
        percent = value.diff_percent or 0.0
        sign = "−" if percent >= 0 else "+"
        lines.append(
            f"📊 ориентир {fmt_ton(value.benchmark_nano)} TON · {sign}{abs(percent):.1f} % · "
            f"аналогов {value.comparables}"
        )
        if value.comparables < value.min_comparables:
            lines.append("⚠️ оценка выгоды ненадёжна (мало аналогов)")
    else:
        lines.append("📊 выгода не рассчитана (нет ориентира)")

    lines.append(f"⭐ релевантность {scored.ranking.score:.1f}/100")
    return "\n".join(lines)


def prepare_card(
    scored: ScoredListing,
    *,
    settings: Settings,
    meta: dict[str, Any] | None = None,
    photo: bytes | None = None,
) -> PreparedCard:
    """Собрать карточку: текст, фото (если есть) и клавиатуру."""
    meta = meta or {}
    listing = scored.listing
    link = build_link(listing, settings.mrkt_link_template)
    keyboard = listing_card_button(link)
    text = build_card_text(scored, settings=settings, meta=meta)
    caption = build_caption(scored, settings=settings, meta=meta)

    if photo:
        # Под короткую подпись; подробности send_card шлёт вторым сообщением.
        return PreparedCard(text=caption, photo=photo, keyboard=keyboard)
    return PreparedCard(text=text, photo=None, keyboard=keyboard)


async def prepare_photo(
    scored: ScoredListing, images: ImageProvider, settings: Settings
) -> bytes | None:
    """Получить и подготовить JPEG для sendPhoto. None — шлём текстовую карточку."""
    if not settings.image_enabled:
        return None
    try:
        result: ImageResult | None = await images.fetch(scored.listing)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось получить изображение: %r", exc)
        return None
    if result is None or not result.ok:
        return None
    return convert_for_telegram(result, size=int(settings.image_size))


async def send_card(
    bot: Bot,
    chat_id: int,
    scored: ScoredListing,
    *,
    settings: Settings,
    images: ImageProvider,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Отправить карточку. Возвращает True, если доставка удалась.

    Стратегия: фото + подпись -> при неудаче текстовая карточка -> при ошибке
    разметки plain-текст. Если чат недоступен (бот заблокирован пользователем),
    TelegramForbiddenError пробрасывается вызывающему коду — Notifier помечает
    чат заблокированным и больше в него не пишет.
    """
    meta = meta or {}
    photo = await prepare_photo(scored, images, settings)
    card = prepare_card(scored, settings=settings, meta=meta, photo=photo)

    if card.photo:
        try:
            await bot.send_photo(
                chat_id,
                photo=BufferedInputFile(card.photo, filename="gift.jpg"),
                caption=card.text,
                parse_mode=ParseMode.HTML,
                reply_markup=card.keyboard,
            )
            # Подробности — вторым сообщением: в caption они не помещаются.
            details = build_card_text(scored, settings=settings, meta=meta, compact=False)
            if len(details) > len(card.text) + 40:
                await bot.send_message(
                    chat_id,
                    details[:4000],
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            return True
        except TelegramForbiddenError:
            # Чат недоступен (бот заблокирован/удалён). Пробрасываем наверх:
            # Notifier запомнит чат как заблокированный и перестанет в него
            # долбить. Возврат False здесь означал бы «пробуем дальше».
            raise
        except TelegramBadRequest as exc:
            logger.info("Фото не отправилось (%s) — шлю текстовую карточку", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("send_photo: %r — шлю текстовую карточку", exc)

    text_card = prepare_card(scored, settings=settings, meta=meta, photo=None)
    try:
        await bot.send_message(
            chat_id,
            text_card.text,
            parse_mode=ParseMode.HTML,
            reply_markup=text_card.keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return True
    except TelegramForbiddenError:
        raise           # см. комментарий выше: Notifier сам обработает блокировку
    except TelegramBadRequest as exc:
        # Чаще всего — ошибка разметки HTML. Шлём безопасный plain-текст.
        logger.warning("send_message (HTML) не удался: %s — повторяю без разметки", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_message: %r", exc)
        return False

    try:
        await bot.send_message(chat_id, text_card.text[:4000], reply_markup=text_card.keyboard)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Доставка карточки в чат %s невозможна: %r", chat_id, exc)
        return False


def explain_score(scored: ScoredListing) -> str:
    """Подробное объяснение оценки релевантности (для кнопки «почему этот лот»)."""
    return explain_ranking(scored.ranking)


def image_to_jpeg_bytes(result: ImageResult | None, size: int = 512) -> bytes | None:
    """Публичная обёртка над конвертацией (используется в /search и тестах)."""
    return convert_for_telegram(result, size=size)


__all__ = [
    "PreparedCard",
    "build_caption",
    "build_card_text",
    "explain_score",
    "image_to_jpeg_bytes",
    "prepare_card",
    "prepare_photo",
    "send_card",
]
