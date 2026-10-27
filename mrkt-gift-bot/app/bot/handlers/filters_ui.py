"""Управление фильтрами: список, создание, изменение, включение/выключение,
удаление, а также ручной поиск /search.

Ввод значений — через FSM: кнопка поля -> бот просит значение -> пользователь
отправляет текст -> значение валидируется и сохраняется. Список значений
(коллекции, модели, фоны, символы) вводится через запятую.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from app.bot import i18n
from app.bot.keyboards import (
    back,
    cancel_state,
    filter_actions,
    filter_confirm_delete,
    filter_fields,
    filters_list,
    mono_menu,
    toggle,
    wizard_menu,
)
from app.config import Settings
from app.db.models import Filter
from app.db.repo import Repository
from app.domain import ScoredListing, ensure_aware
from app.filters.matching import FilterCriteria
from app.pipeline import PipelineContext, record_sent_batch, run_pipeline
from app.ranking import RankingWeights

logger = logging.getLogger(__name__)

router = Router(name="filters")

PER_PAGE = 5


class FilterStates(StatesGroup):
    """Состояния ввода значения поля (фильтр или мастер создания)."""

    waiting_value = State()
    waiting_wizard_value = State()


# Поля, их подписи, типы и подсказки.
FIELD_SPECS: dict[str, dict[str, Any]] = {
    "name": {"label": "Название фильтра", "type": "str", "hint": "Например: «Серые лисы до 50 TON»"},
    "query_text": {
        "label": "Текст поиска",
        "type": "str",
        "hint": "Слово или несколько слов, которые должны встречаться в названии/коллекции/атрибутах. «-» — очистить.",
    },
    "collection_names": {
        "label": "Коллекции",
        "type": "list",
        "hint": "Через запятую, например: <code>Frost Fox, Velvet Owl</code>. «-» — любая коллекция.",
    },
    "model_names": {
        "label": "Модели",
        "type": "list",
        "hint": "Через запятую. «-» — любая модель.",
    },
    "backdrop_names": {
        "label": "Фоны",
        "type": "list",
        "hint": "Через запятую. «-» — любой фон.",
    },
    "symbol_names": {
        "label": "Символы",
        "type": "list",
        "hint": "Через запятую. «-» — любой символ.",
    },
    "currency": {
        "label": "Валюта",
        "type": "str",
        "hint": "MRKT отдаёт цены в <code>TON</code>. Другая валюта отсечёт все лоты.",
    },
    "price_min_ton": {"label": "Цена от, TON", "type": "float?", "hint": "Например: <code>10</code>. «-» — без минимума."},
    "price_max_ton": {"label": "Цена до, TON", "type": "float?", "hint": "Например: <code>250</code>. «-» — без максимума."},
    "number_equals": {"label": "Номер лота", "type": "int?", "hint": "Точный номер подарка, например <code>42</code>. «-» — любой."},
    "min_value_percent": {
        "label": "Минимальная выгода, %",
        "type": "float?",
        "hint": "Сколько процентов ниже ориентира должен стоить лот. «-» — не проверять.",
    },
    "min_rarity_percent": {
        "label": "Максимальная редкость атрибута, %",
        "type": "float?",
        "hint": "Например <code>2</code> — только атрибуты редкостью ≤ 2 %. «-» — не проверять.",
    },
    "min_relevance": {
        "label": "Минимальная релевантность",
        "type": "float",
        "hint": "Порог оценки 0…100. Лоты ниже порога не присылаются. Отправьте <code>-</code>, чтобы снять порог.",
    },
    "mono_min_score": {
        "label": "Порог монохромности",
        "type": "int",
        "hint": "Балл 0…100. Обычно 60–80: чем выше, тем строже отбор. Отправьте <code>-</code> для значения по умолчанию (60).",
    },
    "mono_max_saturation": {
        "label": "Порог насыщенности",
        "type": "float",
        "hint": "Доля насыщенности цвета 0…1. Для серых тонов подходит 0.08–0.15. Отправьте <code>-</code> для значения по умолчанию (0.12).",
    },
    "mono_custom_hue": {
        "label": "Целевой тон (hue)",
        "type": "float?",
        "hint": "Градусы 0…360: 0 — красный, 60 — жёлтый, 120 — зелёный, 210 — синий. «-» — не задавать.",
    },
    "mono_custom_hue_tolerance": {
        "label": "Допуск тона",
        "type": "float",
        "hint": "± градусов вокруг целевого тона, например <code>25</code>.",
    },
    "interval_minutes": {
        "label": "Периодичность проверки, минут",
        "type": "int",
        "hint": "Как часто проверять рынок по этому фильтру. Отправьте <code>-</code> для значения по умолчанию (15 минут).",
    },
    "max_results": {
        "label": "Сколько лотов присылать за раз",
        "type": "int",
        "hint": "Лучшие лоты по релевантности, например <code>5</code>. Отправьте <code>-</code> для значения по умолчанию (5).",
    },
}

LIST_FIELDS = {"collection_names", "model_names", "backdrop_names", "symbol_names"}
CLEAR_TOKENS = {"-", "—", "нет", "none", "null", "сброс", "очистить", ""}

# Для полей, которые не могут быть пустыми (пороги, периодичность), «-» означает
# «вернуть значение по умолчанию». Значения совпадают с дефолтами модели Filter.
RESET_DEFAULTS: dict[str, Any] = {
    "min_relevance": 0.0,
    "mono_min_score": 60,
    "mono_max_saturation": 0.12,
    "mono_custom_hue_tolerance": 25.0,
    "interval_minutes": 15,
    "max_results": 5,
}


# ----------------------------------------------------------------------------
#  Форматирование
# ----------------------------------------------------------------------------

def field_value_text(row: Filter, field: str) -> str:
    value = getattr(row, field, None)
    if field in LIST_FIELDS:
        items = row.list_field(field)
        return ", ".join(items) if items else "не задано"
    if value is None or value == "":
        return "не задано"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def filter_overview(row: Filter, *, settings: Settings) -> str:
    criteria = FilterCriteria.from_row(row, defaults=_defaults(settings))
    state = "🟢 включён" if row.is_enabled else "⚪️ выключен"
    notify = "уведомления идут" if row.notify_enabled else "уведомления выключены"
    lines = [
        f"<b>{row.id}. {row.name}</b> — {state}",
        f"Проверка: каждые {row.interval_minutes} мин · до {row.max_results} лотов за раз · {notify}",
        "",
        criteria.describe(),
    ]
    last = ensure_aware(row.last_run_at)
    if last:
        lines += ["", f"Последняя проверка: {last.strftime('%d.%m.%Y %H:%M UTC')}"]
    if row.last_status:
        lines.append(f"Результат: {row.last_status[:400]}")
    return "\n".join(lines)


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "mono_mode": settings.mono_mode_default,
        "mono_max_saturation": settings.mono_max_saturation,
        "mono_min_score": settings.mono_min_score,
        "mono_allow_name_heuristic": settings.mono_allow_name_heuristic,
        "min_relevance": settings.default_min_relevance,
    }


# ----------------------------------------------------------------------------
#  Парсинг введённых значений
# ----------------------------------------------------------------------------

class ParseError(ValueError):
    pass


def parse_field(field: str, raw: str) -> Any:
    """Преобразовать пользовательский ввод в значение поля. Бросает ParseError."""
    spec = FIELD_SPECS.get(field)
    if spec is None:
        raise ParseError(f"неизвестное поле {field}")
    text = (raw or "").strip()
    kind = spec["type"]

    # Пустое сообщение — не команда сброса: иначе случайный пробел молча
    # обнулил бы настройку. Сброс принимается только явным токеном.
    if text and text.lower() in CLEAR_TOKENS:
        if field in RESET_DEFAULTS:
            # «-» для порога/периодичности = вернуть значение по умолчанию.
            return RESET_DEFAULTS[field]
        if kind.endswith("?"):
            return None
        if kind == "list":
            return ""
        if kind == "str":
            if field == "name":
                raise ParseError("название не может быть пустым")
            return ""

    if kind == "list":
        items = [chunk.strip() for chunk in text.replace(";", ",").split(",") if chunk.strip()]
        if not items:
            raise ParseError("введите хотя бы одно значение через запятую или «-» для сброса")
        return ",".join(items)

    if kind == "str":
        if not text:
            raise ParseError("значение не может быть пустым")
        return text[:80] if field == "name" else text

    if kind in ("int", "int?"):
        try:
            value = int(float(text))
        except ValueError as exc:
            raise ParseError("нужно целое число, например 5") from exc
        if field == "interval_minutes":
            return max(1, value)
        if field == "max_results":
            return max(1, min(value, 25))
        if field == "mono_min_score":
            return max(0, min(value, 100))
        if field == "number_equals":
            return max(0, value)
        return value

    if kind in ("float", "float?"):
        try:
            value = float(text.replace(",", "."))
        except ValueError as exc:
            raise ParseError("нужно число, например 12.5") from exc
        if field in ("price_min_ton", "price_max_ton"):
            if value < 0:
                raise ParseError("цена не может быть отрицательной")
            return round(value, 6)
        if field == "min_relevance":
            return max(0.0, min(value, 100.0))
        if field == "mono_max_saturation":
            return max(0.005, min(value, 0.9))
        if field == "mono_custom_hue":
            return max(0.0, min(value, 360.0))
        if field == "mono_custom_hue_tolerance":
            return max(1.0, min(value, 180.0))
        if field == "min_value_percent":
            return max(-100.0, min(value, 1000.0))
        if field == "min_rarity_percent":
            return max(0.0, min(value, 100.0))
        return value

    raise ParseError(f"не поддерживаемый тип поля {field}")


def validate_consistency(row: Filter) -> list[str]:
    """Предупреждения о противоречивых настройках (не блокируют сохранение)."""
    warnings: list[str] = []
    if (
        row.price_min_ton is not None
        and row.price_max_ton is not None
        and row.price_min_ton > row.price_max_ton
    ):
        warnings.append("⚠️ минимальная цена больше максимальной — лоты не найдутся")
    if row.mono_enabled and row.mono_mode == "custom" and row.mono_custom_hue is None:
        warnings.append("⚠️ режим «custom» без тона работает как обычный серый фильтр")
    if not row.collection_names.strip() and not row.query_text.strip() and not row.model_names.strip():
        warnings.append(
            "ℹ️ не заданы ни коллекция, ни текст, ни модель — буду проверять весь рынок "
            "(это медленно и может упираться в лимиты запросов)"
        )
    if row.interval_minutes < 5:
        warnings.append("ℹ️ интервал меньше 5 минут будет поднят до минимума из настроек")
    return warnings


# ----------------------------------------------------------------------------
#  Список фильтров
# ----------------------------------------------------------------------------

@router.message(Command("filters"))
async def cmd_filters(message: Message, repo: Repository, settings: Settings) -> None:
    user = message.from_user
    if user is None:
        return
    await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    rows = await repo.list_filters(user.id)
    if not rows:
        await message.answer(
            i18n.NO_FILTERS_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=_new_filter_keyboard(),
        )
        return
    await message.answer(
        _filters_header(rows),
        parse_mode=ParseMode.HTML,
        reply_markup=filters_list(rows, page=0, per_page=PER_PAGE),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def _filters_header(rows: list[Filter]) -> str:
    enabled = sum(1 for row in rows if row.is_enabled)
    return (
        f"<b>Ваши фильтры</b> — всего {len(rows)}, включено {enabled}.\n"
        "Откройте фильтр, чтобы изменить, выключить, проверить вручную или удалить."
    )


def _new_filter_keyboard() -> Any:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать фильтр", callback_data="flt:new")]
        ]
    )


@router.callback_query(F.data == "menu:filters")
async def cb_menu_filters(callback: CallbackQuery, repo: Repository) -> None:
    await callback.answer()
    user = callback.from_user
    if user is None or callback.message is None:
        return
    rows = await repo.list_filters(user.id)
    text = i18n.NO_FILTERS_TEXT if not rows else _filters_header(rows)
    keyboard = _new_filter_keyboard() if not rows else filters_list(rows, 0, PER_PAGE)
    await _edit_or_send(callback, text, keyboard)


@router.callback_query(F.data.startswith("flt:list:"))
async def cb_filters_page(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    page = int(callback.data.split(":")[-1] or 0)
    rows = await repo.list_filters(user.id)
    await callback.answer()
    text = i18n.NO_FILTERS_TEXT if not rows else _filters_header(rows)
    keyboard = _new_filter_keyboard() if not rows else filters_list(rows, page, PER_PAGE)
    await _edit_or_send(callback, text, keyboard)


@router.callback_query(F.data.startswith("flt:open:"))
async def cb_filter_open(callback: CallbackQuery, repo: Repository, settings: Settings) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    row = await repo.get_filter(user.id, filter_id)
    await callback.answer()
    if row is None:
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    await _edit_or_send(callback, filter_overview(row, settings=settings), filter_actions(filter_id))


@router.callback_query(F.data.startswith("flt:edit:"))
async def cb_filter_edit(callback: CallbackQuery, repo: Repository, settings: Settings) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    row = await repo.get_filter(user.id, filter_id)
    await callback.answer()
    if row is None:
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    await _edit_or_send(callback, _edit_header(row), filter_fields(filter_id))


def _edit_header(row: Filter) -> str:
    lines = [f"<b>Изменение фильтра «{row.name}»</b>", ""]
    for field in (
        "name",
        "collection_names",
        "price_min_ton",
        "price_max_ton",
        "model_names",
        "backdrop_names",
        "symbol_names",
        "query_text",
        "min_value_percent",
        "min_relevance",
        "interval_minutes",
        "max_results",
    ):
        lines.append(f"• {FIELD_SPECS[field]['label']}: <code>{field_value_text(row, field)}</code>")
    mono = "включена" if row.mono_enabled else "выключена"
    lines.append(f"• Монохромность: {mono} (режим {row.mono_mode}, порог {row.mono_min_score})")
    lines.append("")
    lines.append("Выберите поле и отправьте новое значение одним сообщением.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
#  Изменение отдельного поля
# ----------------------------------------------------------------------------

@router.callback_query(F.data.startswith("flt:field:"))
async def cb_filter_field(
    callback: CallbackQuery,
    repo: Repository,
    state: FSMContext,
    settings: Settings,
    ctx: Any,
) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, field = callback.data.split(":", 3)
    filter_id = int(filter_id_raw)
    row = await repo.get_filter(user.id, filter_id)
    if row is None:
        await callback.answer()
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return

    spec = FIELD_SPECS.get(field)
    if spec is None:
        await callback.answer("Неизвестное поле")
        return

    await state.set_state(FilterStates.waiting_value)
    await state.update_data(filter_id=filter_id, field=field)

    prompt_lines = [
        f"<b>{spec['label']}</b>",
        f"Текущее значение: <code>{field_value_text(row, field)}</code>",
        "",
        spec["hint"],
    ]
    hints = await _suggest_values(field, row, ctx)
    if hints:
        prompt_lines += ["", "<b>Доступные значения:</b>", hints]

    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "\n".join(prompt_lines),
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_state(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


async def _suggest_values(field: str, row: Filter, ctx: Any) -> str:
    """Подсказать реальные значения из источника (коллекции/модели/фоны/символы)."""
    source = getattr(ctx, "source", None)
    if source is None:
        return ""
    try:
        if field == "collection_names":
            names = await source.collections()
        elif field == "model_names":
            names = await source.models(row.list_field("collection_names")[0] if row.list_field("collection_names") else "")
        elif field == "backdrop_names":
            names = await source.backdrops(row.list_field("collection_names")[0] if row.list_field("collection_names") else "")
        elif field == "symbol_names":
            names = await source.symbols(row.list_field("collection_names")[0] if row.list_field("collection_names") else "")
        else:
            return ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("Подсказки значений недоступны: %r", exc)
        return ""
    if not names:
        return ""
    preview = names[:40]
    suffix = f" …и ещё {len(names) - len(preview)}" if len(names) > len(preview) else ""
    return "<code>" + ", ".join(str(n) for n in preview) + suffix + "</code>"


@router.message(FilterStates.waiting_value)
async def msg_filter_value(
    message: Message,
    state: FSMContext,
    repo: Repository,
    settings: Settings,
) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    data = await state.get_data()
    filter_id = data.get("filter_id")
    field = data.get("field")
    if filter_id is None or field is None:
        await state.clear()
        return

    row = await repo.get_filter(user.id, filter_id)
    if row is None:
        await state.clear()
        await message.answer(i18n.FILTER_NOT_FOUND, parse_mode=ParseMode.HTML)
        return

    try:
        value = parse_field(field, message.text)
    except ParseError as exc:
        await message.answer(
            f"Не получилось: {exc}\n\n{FIELD_SPECS[field]['hint']}",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_state(),
        )
        return

    # Особые случаи: связанные поля.
    updates: dict[str, Any] = {field: value}
    if field == "mono_min_score" or field == "mono_max_saturation":
        updates.setdefault("mono_enabled", True)
    if field == "price_min_ton" and row.price_max_ton is not None and value is not None:
        if float(value) > float(row.price_max_ton):
            updates["price_max_ton"] = None
    if field == "price_max_ton" and row.price_min_ton is not None and value is not None:
        if float(value) < float(row.price_min_ton):
            updates["price_min_ton"] = None

    updated = await repo.update_filter(user.id, int(filter_id), **updates)
    await state.clear()
    if updated is None:
        await message.answer(i18n.FILTER_NOT_FOUND, parse_mode=ParseMode.HTML)
        return

    warnings = validate_consistency(updated)
    text = (
        f"✅ Сохранено: <b>{FIELD_SPECS[field]['label']}</b> = "
        f"<code>{field_value_text(updated, field)}</code>"
    )
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    text += "\n\n" + _edit_header(updated)
    await message.answer(
        text[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=filter_fields(int(filter_id)),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ----------------------------------------------------------------------------
#  Монохромность
# ----------------------------------------------------------------------------

@router.callback_query(F.data.startswith("flt:mono:"))
async def cb_mono_menu(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    row = await repo.get_filter(user.id, filter_id)
    await callback.answer()
    if row is None:
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    await _edit_or_send(callback, _mono_header(row), mono_menu(filter_id))


def _mono_header(row: Filter) -> str:
    labels = {"gray": "ч/б и серые", "dark": "чёрный / тёмно-серый",
              "light": "белый / светло-серый", "custom": "заданный тон"}
    confidence = row.mono_require_confidence or "любая"
    return "\n".join(
        [
            f"<b>Монохромность — фильтр «{row.name}»</b>",
            "",
            f"• Требование включено: {'да' if row.mono_enabled else 'нет'}",
            f"• Режим: {labels.get(row.mono_mode, row.mono_mode)}",
            f"• Порог балла: {row.mono_min_score}/100",
            f"• Порог насыщенности: {row.mono_max_saturation:g}",
            f"• Целевой тон: {row.mono_custom_hue if row.mono_custom_hue is not None else 'не задан'}"
            f" ± {row.mono_custom_hue_tolerance:g}°",
            f"• Минимальная уверенность: {confidence}",
            f"• Анализ изображения: {'вкл' if row.mono_allow_image else 'выкл'}",
            f"• Эвристика по названию: {'вкл' if row.mono_allow_name_heuristic else 'выкл'} "
            "(всегда даёт низкую уверенность)",
            "",
            "Как определяется монохромность — см. /help. Порядок источников: "
            "цвета фона из API MRKT → цвета фона @GiftChanges → анализ картинки → "
            "эвристика по названию.",
        ]
    )


@router.callback_query(F.data.startswith("mono:mode:"))
async def cb_mono_mode(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, mode = callback.data.split(":", 3)
    row = await repo.update_filter(user.id, int(filter_id_raw), mono_mode=mode, mono_enabled=True)
    await callback.answer("Режим обновлён")
    if row is None:
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    await _edit_or_send(callback, _mono_header(row), mono_menu(int(filter_id_raw)))


@router.callback_query(F.data.startswith("mono:conf:"))
async def cb_mono_conf(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, level = callback.data.split(":", 3)
    value = "" if level == "any" else level
    row = await repo.update_filter(user.id, int(filter_id_raw), mono_require_confidence=value)
    await callback.answer("Обновлено")
    if row is None:
        return
    await _edit_or_send(callback, _mono_header(row), mono_menu(int(filter_id_raw)))


@router.callback_query(F.data.startswith("mono:img:"))
async def cb_mono_img(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, flag = callback.data.split(":", 3)
    row = await repo.update_filter(user.id, int(filter_id_raw), mono_allow_image=flag == "1")
    await callback.answer("Обновлено")
    if row is None:
        return
    await _edit_or_send(callback, _mono_header(row), mono_menu(int(filter_id_raw)))


@router.callback_query(F.data.startswith("mono:name:"))
async def cb_mono_name(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, flag = callback.data.split(":", 3)
    row = await repo.update_filter(
        user.id, int(filter_id_raw), mono_allow_name_heuristic=flag == "1"
    )
    await callback.answer("Обновлено")
    if row is None:
        return
    await _edit_or_send(callback, _mono_header(row), mono_menu(int(filter_id_raw)))


@router.callback_query(F.data.startswith("flt:mono_toggle:"))
async def cb_mono_toggle(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, flag = callback.data.split(":", 3)
    row = await repo.update_filter(user.id, int(filter_id_raw), mono_enabled=flag == "1")
    await callback.answer("Обновлено")
    if row is None:
        return
    await _edit_or_send(callback, _mono_header(row), mono_menu(int(filter_id_raw)))


# ----------------------------------------------------------------------------
#  Включение / удаление / ручная проверка / сброс истории
# ----------------------------------------------------------------------------

@router.callback_query(F.data.startswith("flt:toggle:"))
async def cb_filter_toggle(callback: CallbackQuery, repo: Repository, settings: Settings) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    row = await repo.get_filter(user.id, filter_id)
    if row is None:
        await callback.answer()
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    updated = await repo.update_filter(user.id, filter_id, is_enabled=not row.is_enabled)
    await callback.answer("Включён" if updated and updated.is_enabled else "Выключен")
    if updated is None:
        return
    await _edit_or_send(callback, filter_overview(updated, settings=settings), filter_actions(filter_id))


@router.callback_query(F.data.startswith("flt:notify:"))
async def cb_filter_notify(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    _, _, filter_id_raw, flag = callback.data.split(":", 3)
    row = await repo.update_filter(user.id, int(filter_id_raw), notify_enabled=flag == "1")
    await callback.answer("Обновлено")
    if row is None:
        return
    await _edit_or_send(
        callback,
        "Уведомления по фильтру: " + ("включены" if row.notify_enabled else "выключены"),
        toggle(row.notify_enabled, "Уведомлять", "Молчать", "flt:notify", int(filter_id_raw)),
    )


@router.callback_query(F.data.startswith("flt:del:"))
async def cb_filter_delete(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    row = await repo.get_filter(user.id, filter_id)
    await callback.answer()
    if row is None:
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    await _edit_or_send(
        callback,
        f"Удалить фильтр «{row.name}» вместе с историей его отправок?",
        filter_confirm_delete(filter_id),
    )


@router.callback_query(F.data.startswith("flt:delconf:"))
async def cb_filter_delete_confirm(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    ok = await repo.delete_filter(user.id, filter_id)
    await callback.answer("Удалено" if ok else "Не найдено")
    rows = await repo.list_filters(user.id)
    text = i18n.FILTER_DELETED if ok else i18n.FILTER_NOT_FOUND
    if rows:
        text += "\n\n" + _filters_header(rows)
    keyboard = filters_list(rows, 0, PER_PAGE) if rows else _new_filter_keyboard()
    await _edit_or_send(callback, text, keyboard)


@router.callback_query(F.data.startswith("flt:forget:"))
async def cb_filter_forget(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    removed = await repo.clear_history(user.id, filter_id)
    await callback.answer(f"Забыто лотов: {removed}")
    await _edit_or_send(
        callback,
        f"🧹 История отправок по фильтру очищена ({removed} записей).\n"
        "При следующей проверке подходящие лоты придут снова — даже если уже приходили.",
        filter_actions(filter_id),
    )


@router.callback_query(F.data.startswith("flt:run:"))
async def cb_filter_run(
    callback: CallbackQuery,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    notifier: Any,
    monitor: Any,
) -> None:
    """Проверить один фильтр немедленно (с отправкой и дедупликацией)."""
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    filter_id = int(callback.data.split(":")[-1])
    row = await repo.get_filter(user.id, filter_id)
    if row is None:
        await callback.answer()
        await _edit_or_send(callback, i18n.FILTER_NOT_FOUND, back("flt:list:0"))
        return
    await callback.answer("Проверяю…")

    criteria = FilterCriteria.from_row(row, defaults=_defaults(settings))
    weights = await _weights_for(repo, settings, user.id)
    outcome = await run_pipeline(
        ctx, repo, user_id=user.id, filter_row=row, criteria=criteria, weights=weights
    )
    delivered_items: list[ScoredListing] = []
    for item in outcome.results:
        delivered = await notifier.notify(
            user.id,
            item,
            {
                "filter_id": filter_id,
                "filter_name": row.name,
                "is_test_data": outcome.is_test_data or item.listing.is_test_data,
                "reason": item.listing.extra.get("send_reason", "new"),
            },
        )
        if delivered:
            delivered_items.append(item)
    sent = len(delivered_items)
    if delivered_items:
        await record_sent_batch(
            repo, user_id=user.id, filter_id=filter_id, items=delivered_items
        )
    await repo.mark_filter_run(
        user.id, filter_id, status="manual", message=f"ручная проверка: {outcome.summary()}"
    )
    if monitor is not None:
        monitor.request_tick()

    if sent:
        await callback.message.answer(  # type: ignore[union-attr]
            f"✅ Отправлено лотов: {sent}. {outcome.summary()}",
            parse_mode=ParseMode.HTML,
            reply_markup=back(f"flt:open:{filter_id}"),
        )
    else:
        reason = ""
        if outcome.errors:
            reason = "\n\n" + "\n".join(outcome.errors[:3])
        elif outcome.duplicates_skipped:
            reason = (
                f"\n\nВсе подходящие лоты уже приходили ранее "
                f"(пропущено дублей: {outcome.duplicates_skipped}). "
                "Повтор приходит только при существенном снижении цены."
            )
        elif outcome.fetched == 0:
            reason = "\n\nИсточник не вернул ни одного лота по этим условиям."
        else:
            reason = "\n\nНи один лот не прошёл фильтр или порог релевантности."
        await callback.message.answer(  # type: ignore[union-attr]
            f"Ничего не отправлено. {outcome.summary()}{reason}"[:4000],
            parse_mode=ParseMode.HTML,
            reply_markup=back(f"flt:open:{filter_id}"),
        )


async def _weights_for(repo: Repository, settings: Settings, user_id: int) -> RankingWeights:
    user = await repo.get_user(user_id)
    stored = user.weights() if user else {}
    base = RankingWeights.from_settings(settings).as_dict()
    return RankingWeights.from_mapping({**base, **stored}) if stored else RankingWeights.from_settings(settings)


# ----------------------------------------------------------------------------
#  Мастер создания фильтра
# ----------------------------------------------------------------------------

@router.callback_query(F.data == "flt:new")
async def cb_filter_new(callback: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    await callback.answer()
    draft = _new_filter_draft(settings)
    await state.set_state(FilterStates.waiting_wizard_value)
    await state.update_data(draft=draft)
    if callback.message is not None:
        await callback.message.answer(
            _wizard_text(draft, settings),
            parse_mode=ParseMode.HTML,
            reply_markup=wizard_menu(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def _new_filter_draft(settings: Settings) -> dict[str, Any]:
    return {
        "name": "",
        "query_text": "",
        "collection_names": "",
        "model_names": "",
        "backdrop_names": "",
        "symbol_names": "",
        "currency": "TON",
        "price_min_ton": None,
        "price_max_ton": None,
        "mono_enabled": False,
        "mono_mode": settings.mono_mode_default,
        "mono_max_saturation": settings.mono_max_saturation,
        "mono_min_score": settings.mono_min_score,
        "mono_custom_hue": None,
        "mono_custom_hue_tolerance": settings.mono_custom_hue_tolerance,
        "mono_require_confidence": "",
        "mono_allow_image": True,
        "mono_allow_name_heuristic": settings.mono_allow_name_heuristic,
        "min_relevance": float(settings.default_min_relevance),
        "min_value_percent": None,
        "min_rarity_percent": None,
        "number_equals": None,
        "interval_minutes": int(settings.default_interval_minutes),
        "max_results": int(settings.max_results_per_run),
        "notify_enabled": True,
        "is_enabled": True,
    }


def _wizard_text(draft: dict[str, Any], settings: Settings) -> str:
    class _Row:
        pass

    row = _Row()
    for key, value in draft.items():
        setattr(row, key, value)
    row.id = 0
    criteria = FilterCriteria.from_row(row, defaults=_defaults(settings))
    lines = [
        "<b>Новый фильтр</b>",
        "",
        criteria.describe(),
        "",
        f"Название: <code>{draft['name'] or 'не задано'}</code>",
        f"Интервал проверки: <code>{draft['interval_minutes']} мин</code>",
        f"Лотов за раз: <code>{draft['max_results']}</code>",
        f"Монохромность: <code>{'включена' if draft['mono_enabled'] else 'выключена'}</code>",
        "",
        "Нажимайте на поля, чтобы задать значения, затем «Создать фильтр».",
    ]
    return "\n".join(lines)


@router.callback_query(F.data.startswith("wiz:field:"))
async def cb_wizard_field(callback: CallbackQuery, state: FSMContext, ctx: Any) -> None:
    _, _, _idx, field = callback.data.split(":", 3)
    spec = FIELD_SPECS.get(field)
    if spec is None:
        await callback.answer("Неизвестное поле")
        return
    data = await state.get_data()
    draft: dict[str, Any] = data.get("draft") or {}
    await state.set_state(FilterStates.waiting_wizard_value)
    await state.update_data(draft=draft, wizard_field=field)

    current = draft.get(field)
    prompt = [
        f"<b>{spec['label']}</b>",
        f"Текущее значение: <code>{'не задано' if current in (None, '') else current}</code>",
        "",
        spec["hint"],
    ]
    if field in LIST_FIELDS and ctx is not None:
        try:
            names: list[str] = []
            collection = ""
            if field != "collection_names":
                collections = [c.strip() for c in str(draft.get("collection_names") or "").split(",") if c.strip()]
                collection = collections[0] if collections else ""
            if field == "collection_names":
                names = await ctx.source.collections()
            elif field == "model_names" and collection:
                names = await ctx.source.models(collection)
            elif field == "backdrop_names" and collection:
                names = await ctx.source.backdrops(collection)
            elif field == "symbol_names" and collection:
                names = await ctx.source.symbols(collection)
            if names:
                preview = names[:40]
                suffix = f" …и ещё {len(names) - len(preview)}" if len(names) > 40 else ""
                prompt += ["", "<b>Доступные значения:</b>", "<code>" + ", ".join(map(str, preview)) + suffix + "</code>"]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Подсказки в мастере недоступны: %r", exc)

    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "\n".join(prompt),
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_state(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


@router.callback_query(F.data.startswith("wiz:mono:"))
async def cb_wizard_mono(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    draft: dict[str, Any] = data.get("draft") or {}
    await state.update_data(draft=draft, wizard_field="__mono__")
    await state.set_state(FilterStates.waiting_wizard_value)
    labels = {"gray": "gray — ч/б и серые", "dark": "dark — чёрный/тёмно-серый",
              "light": "light — белый/светло-серый", "custom": "custom — заданный тон"}
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            "\n".join(
                [
                    "<b>Монохромность в новом фильтре</b>",
                    "",
                    f"Сейчас: <code>{'включена' if draft.get('mono_enabled') else 'выключена'}</code>, "
                    f"режим <code>{draft.get('mono_mode', 'gray')}</code>, "
                    f"порог <code>{draft.get('mono_min_score', 60)}</code>",
                    "",
                    "Отправьте одно из:",
                    "• <code>on</code> / <code>off</code> — включить или выключить требование",
                    "• <code>gray</code>, <code>dark</code>, <code>light</code>, <code>custom</code> — режим",
                    "• число 0…100 — порог балла монохромности",
                    "• <code>hue 210 25</code> — целевой тон и допуск (для режима custom)",
                    "",
                    "Режимы: " + "; ".join(labels.values()),
                ]
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_state(),
        )


@router.message(FilterStates.waiting_wizard_value)
async def msg_wizard_value(
    message: Message,
    state: FSMContext,
    settings: Settings,
    repo: Repository,
    monitor: Any,
) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    data = await state.get_data()
    draft: dict[str, Any] = dict(data.get("draft") or {})
    field = data.get("wizard_field")
    text = message.text.strip()

    if field == "__mono__":
        lowered = text.lower()
        if lowered in ("on", "вкл", "да", "1", "true"):
            draft["mono_enabled"] = True
        elif lowered in ("off", "выкл", "нет", "0", "false"):
            draft["mono_enabled"] = False
        elif lowered in ("gray", "dark", "light", "custom"):
            draft["mono_mode"] = lowered
            draft["mono_enabled"] = True
        elif lowered.startswith("hue"):
            parts = lowered.split()
            try:
                draft["mono_custom_hue"] = float(parts[1]) % 360.0
                if len(parts) > 2:
                    draft["mono_custom_hue_tolerance"] = max(1.0, min(float(parts[2]), 180.0))
                draft["mono_mode"] = "custom"
                draft["mono_enabled"] = True
            except (IndexError, ValueError):
                await message.answer(
                    "Формат: <code>hue 210 25</code> (тон и допуск в градусах).",
                    parse_mode=ParseMode.HTML,
                )
                return
        else:
            try:
                draft["mono_min_score"] = max(0, min(int(float(text)), 100))
                draft["mono_enabled"] = True
            except ValueError:
                await message.answer(
                    "Не понял. Отправьте <code>on</code>/<code>off</code>, режим "
                    "(<code>gray</code>, <code>dark</code>, <code>light</code>, <code>custom</code>), "
                    "порог числом или <code>hue 210 25</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return
        await state.update_data(draft=draft, wizard_field=None)
        await message.answer(
            _wizard_text(draft, settings),
            parse_mode=ParseMode.HTML,
            reply_markup=wizard_menu(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    if field is None or field not in FIELD_SPECS:
        await state.clear()
        await message.answer(
            "Сессия создания фильтра устарела — начните заново с /filters.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        value = parse_field(field, text)
    except ParseError as exc:
        await message.answer(
            f"Не получилось: {exc}\n\n{FIELD_SPECS[field]['hint']}",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_state(),
        )
        return

    draft[field] = value
    if field in ("mono_min_score", "mono_max_saturation", "mono_custom_hue", "mono_custom_hue_tolerance"):
        draft["mono_enabled"] = True
    if field == "collection_names" and value:
        # Смена коллекции делает выбранные модели/фоны/символы неактуальными.
        for dependent in ("model_names", "backdrop_names", "symbol_names"):
            draft[dependent] = ""

    await state.update_data(draft=draft, wizard_field=None)
    await message.answer(
        _wizard_text(draft, settings),
        parse_mode=ParseMode.HTML,
        reply_markup=wizard_menu(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data.startswith("wiz:done"))
async def cb_wizard_done(
    callback: CallbackQuery,
    state: FSMContext,
    repo: Repository,
    settings: Settings,
    monitor: Any,
) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    data = await state.get_data()
    draft: dict[str, Any] = dict(data.get("draft") or {})
    if not draft:
        await state.clear()
        await callback.answer("Черновик пуст")
        return

    if not draft.get("name"):
        draft["name"] = _auto_name(draft)

    draft["interval_minutes"] = max(
        int(settings.min_interval_minutes), int(draft.get("interval_minutes") or settings.default_interval_minutes)
    )
    row = await repo.create_filter(user.id, **{k: v for k, v in draft.items() if k != "id"})
    await state.clear()
    if monitor is not None:
        monitor.request_tick()

    warnings = validate_consistency(row)
    text = i18n.FILTER_CREATED.format(name=row.name, interval=row.interval_minutes)
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    text += "\n\n" + filter_overview(row, settings=settings)
    await callback.answer("Фильтр создан")
    if callback.message is not None:
        await callback.message.answer(
            text[:4000],
            parse_mode=ParseMode.HTML,
            reply_markup=filter_actions(row.id),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def _auto_name(draft: dict[str, Any]) -> str:
    parts: list[str] = []
    collections = [c.strip() for c in str(draft.get("collection_names") or "").split(",") if c.strip()]
    if collections:
        parts.append(collections[0] if len(collections) == 1 else f"{len(collections)} коллекции")
    if draft.get("price_max_ton") is not None:
        parts.append(f"до {draft['price_max_ton']:g} TON")
    if draft.get("mono_enabled"):
        parts.append("монохром")
    if draft.get("query_text"):
        parts.append(f"«{draft['query_text']}»")
    return " · ".join(parts) if parts else "Новый фильтр"


@router.callback_query(F.data == "wiz:cancel")
async def cb_wizard_cancel(callback: CallbackQuery, state: FSMContext, repo: Repository) -> None:
    user = callback.from_user
    await state.clear()
    await callback.answer("Создание отменено")
    if user is None:
        return
    rows = await repo.list_filters(user.id)
    text = i18n.NO_FILTERS_TEXT if not rows else _filters_header(rows)
    keyboard = _new_filter_keyboard() if not rows else filters_list(rows, 0, PER_PAGE)
    await _edit_or_send(callback, text, keyboard)


# ----------------------------------------------------------------------------
#  Ручной поиск /search
# ----------------------------------------------------------------------------

@router.message(Command("search"))
async def cmd_search(
    message: Message,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    notifier: Any,
    monitor: Any,
) -> None:
    """/search [limit] — проверить рынок прямо сейчас по включённым фильтрам."""
    user = message.from_user
    if user is None:
        return
    await repo.touch_user(user.id, username=user.username, first_name=user.first_name)

    limit_override: int | None = None
    if message.text:
        parts = message.text.split()
        if len(parts) > 1:
            try:
                limit_override = max(1, min(int(parts[1]), 25))
            except ValueError:
                await message.answer(
                    "Формат: <code>/search</code> или <code>/search 10</code> (число лотов).",
                    parse_mode=ParseMode.HTML,
                )
                return

    rows = [row for row in await repo.list_filters(user.id, enabled_only=True)]
    if not rows:
        await message.answer(i18n.SEARCH_NO_FILTERS, parse_mode=ParseMode.HTML)
        return

    waiting = await message.answer(
        f"🔎 Проверяю рынок по {len(rows)} фильтр(ам)… Источник: <code>{ctx.source.name}</code>",
        parse_mode=ParseMode.HTML,
    )
    weights = await _weights_for(repo, settings, user.id)
    total_sent = 0
    total_fetched = 0
    errors: list[str] = []

    for row in rows:
        criteria = FilterCriteria.from_row(row, defaults=_defaults(settings))
        if limit_override:
            row.max_results = limit_override
        outcome = await run_pipeline(
            ctx,
            repo,
            user_id=user.id,
            filter_row=row,
            criteria=criteria,
            weights=weights,
            manual=True,
        )
        total_fetched += outcome.fetched
        errors.extend(outcome.errors)
        for item in outcome.results:
            delivered = await notifier.notify(
                user.id,
                item,
                {
                    "filter_id": row.id,
                    "filter_name": row.name,
                    "is_test_data": outcome.is_test_data or item.listing.is_test_data,
                    "reason": item.listing.extra.get("send_reason", "new"),
                    "manual": True,
                },
            )
            if delivered:
                total_sent += 1

    # Ручной поиск намеренно НЕ записывает лоты как «уже отправленные»:
    # иначе разовый /search заблокировал бы будущие уведомления мониторинга.
    # Дедуп здесь работает в одну сторону — уже присланные лоты не повторяются.
    summary = (
        f"🔎 Готово. Фильтров: {len(rows)}, просмотрено лотов: {total_fetched}, "
        f"отправлено: {total_sent}."
    )
    if total_sent == 0:
        summary += (
            "\n\nПодходящих новых лотов нет. Возможные причины: условия слишком узкие, "
            "порог релевантности высокий, либо все найденные лоты уже приходили ранее."
        )
    if errors:
        summary += "\n\nОшибки источника:\n" + "\n".join(dict.fromkeys(errors))[:800]
    try:
        await waiting.edit_text(summary[:4000], parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        await message.answer(summary[:4000], parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------------------
#  Утилиты
# ----------------------------------------------------------------------------

async def _edit_or_send(callback: CallbackQuery, text: str, keyboard: Any) -> None:
    """Отредактировать сообщение, а если нельзя — отправить новое."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text[:4000], parse_mode=ParseMode.HTML, reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramBadRequest as exc:
        logger.debug("edit_text не удался (%s) — отправляю новое сообщение", exc)
        await callback.message.answer(
            text[:4000], parse_mode=ParseMode.HTML, reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
