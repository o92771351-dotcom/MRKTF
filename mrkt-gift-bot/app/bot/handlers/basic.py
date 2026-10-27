"""Базовые команды: /start, /help, /credits, /about."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from app.bot import i18n
from app.bot.keyboards import main_menu
from app.config import Settings
from app.db.repo import Repository

logger = logging.getLogger(__name__)

router = Router(name="basic")


def _name_of(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "друг"
    return user.first_name or user.username or "друг"


@router.message(CommandStart())
async def cmd_start(message: Message, repo: Repository, settings: Settings, ctx: Any) -> None:
    user = message.from_user
    if user is not None:
        await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    status = await ctx.source.status()
    text = i18n.start_text(_name_of(message), status.name, status.is_test_data)
    if status.kind.value == "mrkt" and not status.available:
        text += f"\n\n{i18n.MRKT_NOT_CONNECTED}\n<code>{status.message}</code>"
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, repo: Repository) -> None:
    user = message.from_user
    if user is not None:
        await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    await message.answer(
        i18n.HELP_TEXT,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(Command("credits", "about"))
async def cmd_credits(message: Message) -> None:
    await message.answer(
        i18n.CREDITS_TEXT,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == "any:cancel")
async def cancel_any(callback: CallbackQuery, state: FSMContext) -> None:
    """Универсальная отмена: сбрасываем состояние ввода и закрываем сообщение."""
    await state.clear()
    await callback.answer("Отменено")
    if callback.message is not None:
        try:
            await callback.message.edit_text(
                "Действие отменено.", parse_mode=ParseMode.HTML
            )
        except Exception:  # noqa: BLE001
            try:
                await callback.message.delete()
            except Exception:  # noqa: BLE001
                pass
