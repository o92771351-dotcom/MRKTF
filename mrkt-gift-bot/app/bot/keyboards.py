"""Inline-клавиатуры бота."""

from __future__ import annotations

from typing import Any, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.ranking import COMPONENT_ORDER

MONO_MODES: tuple[tuple[str, str], ...] = (
    ("gray", "Ч/б и серые"),
    ("dark", "Чёрный / тёмно-серый"),
    ("light", "Белый / светло-серый"),
    ("custom", "Заданный тон (hue)"),
)

CONFIDENCE_LEVELS: tuple[tuple[str, str], ...] = (
    ("", "Любая уверенность"),
    ("low", "Не ниже low"),
    ("medium", "Не ниже medium"),
    ("high", "Только high"),
)

WEIGHT_LABELS: dict[str, str] = {
    "match": "Соответствие фильтрам",
    "monochrome": "Монохромность",
    "value": "Выгода",
    "confidence": "Надёжность данных",
    "freshness": "Свежесть лота",
}


def _kb(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button for button in row] for row in rows if row]
    )


def main_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                InlineKeyboardButton(text="🔎 Фильтры", callback_data="menu:filters"),
                InlineKeyboardButton(text="🛰 Статус", callback_data="menu:status"),
            ],
        ]
    )


def filters_list(filters: Sequence[Any], page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    chunk = list(filters)[page * per_page : (page + 1) * per_page]
    for row in chunk:
        icon = "🟢" if row.is_enabled else "⚪️"
        text = f"{icon} {row.id}. {row.name}"
        rows.append([InlineKeyboardButton(text=text[:60], callback_data=f"flt:open:{row.id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"flt:page:{page - 1}"))
    if len(filters) > (page + 1) * per_page:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"flt:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Создать фильтр", callback_data="flt:new")])
    return _kb(rows)


def filter_actions(filter_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"flt:edit:{filter_id}"),
                InlineKeyboardButton(text="🔕 Включить/выключить", callback_data=f"flt:toggle:{filter_id}"),
            ],
            [
                InlineKeyboardButton(text="⏱ Интервал", callback_data=f"flt:field:{filter_id}:interval_minutes"),
                InlineKeyboardButton(text="🔢 Лимит лотов", callback_data=f"flt:field:{filter_id}:max_results"),
            ],
            [
                InlineKeyboardButton(text="🔍 Проверить сейчас", callback_data=f"flt:run:{filter_id}"),
                InlineKeyboardButton(text="🧹 Забыть отправленные", callback_data=f"flt:forget:{filter_id}"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"flt:del:{filter_id}")],
            [InlineKeyboardButton(text="← К списку", callback_data="flt:list:0")],
        ]
    )


def filter_confirm_delete(filter_id: int) -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"flt:delconf:{filter_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"flt:open:{filter_id}"),
            ]
        ]
    )


def filter_fields(filter_id: int) -> InlineKeyboardMarkup:
    fields: tuple[tuple[str, str], ...] = (
        ("name", "Название"),
        ("query_text", "Текст поиска"),
        ("collection_names", "Коллекции"),
        ("price_min_ton", "Цена от, TON"),
        ("price_max_ton", "Цена до, TON"),
        ("currency", "Валюта"),
        ("model_names", "Модели"),
        ("backdrop_names", "Фоны"),
        ("symbol_names", "Символы"),
        ("number_equals", "Номер лота"),
        ("min_value_percent", "Мин. выгода, %"),
        ("min_rarity_percent", "Макс. редкость, %"),
        ("min_relevance", "Мин. релевантность"),
    )
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(fields), 2):
        pair = fields[index : index + 2]
        rows.append(
            [
                InlineKeyboardButton(text=label[:32], callback_data=f"flt:field:{filter_id}:{key}")
                for key, label in pair
            ]
        )
    rows.append([InlineKeyboardButton(text="🎨 Монохромность", callback_data=f"flt:mono:{filter_id}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"flt:open:{filter_id}")])
    return _kb(rows)


def mono_menu(filter_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="✅ Требовать монохром", callback_data=f"flt:mono_toggle:{filter_id}:1"
            ),
            InlineKeyboardButton(
                text="◻️ Не требовать", callback_data=f"flt:mono_toggle:{filter_id}:0"
            ),
        ]
    ]
    for key, label in MONO_MODES:
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mono:mode:{filter_id}:{key}")])
    rows.append(
        [
            InlineKeyboardButton(text="Порог балла", callback_data=f"flt:field:{filter_id}:mono_min_score"),
            InlineKeyboardButton(text="Порог насыщенности", callback_data=f"flt:field:{filter_id}:mono_max_saturation"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="Тон (hue)", callback_data=f"flt:field:{filter_id}:mono_custom_hue"),
            InlineKeyboardButton(text="Допуск тона", callback_data=f"flt:field:{filter_id}:mono_custom_hue_tolerance"),
        ]
    )
    for key, label in CONFIDENCE_LEVELS:
        suffix = key or "any"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mono:conf:{filter_id}:{suffix}")])
    rows.append(
        [
            InlineKeyboardButton(text="Анализ картинки: вкл", callback_data=f"mono:img:{filter_id}:1"),
            InlineKeyboardButton(text="Анализ картинки: выкл", callback_data=f"mono:img:{filter_id}:0"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="Эвристика по названию: вкл", callback_data=f"mono:name:{filter_id}:1"),
            InlineKeyboardButton(text="Эвристика по названию: выкл", callback_data=f"mono:name:{filter_id}:0"),
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"flt:edit:{filter_id}")])
    return _kb(rows)


def wizard_menu(filter_id: int | None = None) -> InlineKeyboardMarkup:
    """Меню мастера создания фильтра: те же поля, но с кнопкой «Готово»."""
    rows: list[list[InlineKeyboardButton]] = []
    fields: tuple[tuple[str, str], ...] = (
        ("name", "Название"),
        ("collection_names", "Коллекции"),
        ("price_min_ton", "Цена от, TON"),
        ("price_max_ton", "Цена до, TON"),
        ("model_names", "Модели"),
        ("backdrop_names", "Фоны"),
        ("symbol_names", "Символы"),
        ("query_text", "Текст поиска"),
        ("min_value_percent", "Мин. выгода, %"),
        ("min_relevance", "Мин. релевантность"),
        ("interval_minutes", "Интервал, мин"),
    )
    for index in range(0, len(fields), 2):
        pair = fields[index : index + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:32], callback_data=f"wiz:field:{filter_id or 0}:{key}"
                )
                for key, label in pair
            ]
        )
    rows.append([InlineKeyboardButton(text="🎨 Монохромность", callback_data=f"wiz:mono:{filter_id or 0}")])
    rows.append([InlineKeyboardButton(text="✅ Создать фильтр", callback_data=f"wiz:done:{filter_id or 0}")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="wiz:cancel")])
    return _kb(rows)


def toggle(value: bool, on_text: str, off_text: str, callback_prefix: str, filter_id: int) -> InlineKeyboardMarkup:
    active = "✅" if value else "◻️"
    inactive = "◻️" if value else "✅"
    return _kb(
        [
            [
                InlineKeyboardButton(
                    text=f"{active} {on_text}", callback_data=f"{callback_prefix}:{filter_id}:1"
                ),
                InlineKeyboardButton(
                    text=f"{inactive} {off_text}", callback_data=f"{callback_prefix}:{filter_id}:0"
                ),
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"flt:open:{filter_id}")],
        ]
    )


def weights_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key in COMPONENT_ORDER:
        builder.button(text=WEIGHT_LABELS[key][:32], callback_data=f"w:set:{key}")
    builder.button(text="Сбросить к значениям по умолчанию", callback_data="w:reset")
    builder.button(text="Как считается оценка", callback_data="w:explain")
    builder.adjust(1)
    return builder.as_markup()


def listing_card_button(url: str, text: str = "🔗 Открыть предложение") -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton(text=text, url=url)]])


def status_menu() -> InlineKeyboardMarkup:
    return _kb(
        [
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="st:refresh"),
                InlineKeyboardButton(text="🩺 Проверить источник", callback_data="st:check"),
            ],
            [InlineKeyboardButton(text="🧹 Очистить историю отправок", callback_data="st:clear")],
        ]
    )


def back(callback_data: str = "flt:list:0", text: str = "← Назад") -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton(text=text, callback_data=callback_data)]])


def cancel_state() -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton(text="✖️ Отмена", callback_data="any:cancel")]])
