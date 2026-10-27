"""Состояние мониторинга, пауза, веса ранжирования, история и диагностика.

Команды: /status, /pause, /resume, /weights, /history, /diag.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message

from app.bot import i18n
from app.bot.keyboards import WEIGHT_LABELS, back, cancel_state, status_menu, weights_menu
from app.config import Settings
from app.db.repo import Repository
from app.domain import ensure_aware, fmt_ton
from app.pipeline import PipelineContext
from app.ranking import COMPONENT_ORDER, RankingWeights

logger = logging.getLogger(__name__)

router = Router(name="status")


class WeightStates(StatesGroup):
    waiting_value = State()


# ----------------------------------------------------------------------------
#  /status
# ----------------------------------------------------------------------------

@router.message(Command("status"))
async def cmd_status(
    message: Message,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    monitor: Any,
    notifier: Any,
) -> None:
    user = message.from_user
    if user is None:
        return
    await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    text = await build_status_text(repo, settings, ctx, monitor, notifier, user.id)
    await message.answer(
        text[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=status_menu(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == "menu:status")
async def cb_menu_status(
    callback: CallbackQuery,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    monitor: Any,
    notifier: Any,
) -> None:
    await send_status(callback, repo, settings, ctx, monitor, notifier)


@router.callback_query(F.data == "st:refresh")
async def cb_status_refresh(
    callback: CallbackQuery,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    monitor: Any,
    notifier: Any,
) -> None:
    await send_status(callback, repo, settings, ctx, monitor, notifier, edit=True)


@router.callback_query(F.data == "st:check")
async def cb_status_check(
    callback: CallbackQuery,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    monitor: Any,
    notifier: Any,
) -> None:
    """Живая проверка источника (делает реальный запрос)."""
    await callback.answer("Проверяю источник…")
    status = await ctx.source.status()
    try:
        await repo.save_source_status(status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось сохранить статус источника: %r", exc)
    text = (
        f"<b>Проверка источника</b>\n"
        f"Источник: <code>{status.name}</code> ({status.kind.value})\n"
        f"Доступен: {'да' if status.available else 'нет'}\n"
        f"Тестовые данные: {'да' if status.is_test_data else 'нет'}\n"
        f"{status.message}\n"
    )
    if status.detail:
        text += f"Подробности: {status.detail}\n"
    if status.last_error:
        text += f"Последняя ошибка: <code>{status.last_error[:300]}</code>\n"
    text += (
        f"Запросов: {status.requests_total}, с ошибками: {status.requests_failed}\n\n"
        "Что нужно для реальных данных — README, раздел 5."
    )
    if callback.message is not None:
        await callback.message.answer(
            text[:4000], parse_mode=ParseMode.HTML, reply_markup=back("menu:status")
        )


@router.callback_query(F.data == "st:clear")
async def cb_status_clear(callback: CallbackQuery, repo: Repository) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    removed = await repo.clear_history(user.id)
    await callback.answer(f"Удалено записей: {removed}")
    if callback.message is not None:
        await callback.message.answer(
            f"🧹 История отправок очищена ({removed} записей).\n"
            "Подходящие лоты придут снова при следующей проверке — даже если уже приходили.",
            parse_mode=ParseMode.HTML,
            reply_markup=status_menu(),
        )


async def send_status(
    callback: CallbackQuery,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    monitor: Any,
    notifier: Any,
    *,
    edit: bool = False,
) -> None:
    await callback.answer()
    user = callback.from_user
    if user is None or callback.message is None:
        return
    text = await build_status_text(repo, settings, ctx, monitor, notifier, user.id)
    if edit:
        try:
            await callback.message.edit_text(
                text[:4000], parse_mode=ParseMode.HTML, reply_markup=status_menu(),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return
        except TelegramBadRequest as exc:
            logger.debug("edit_text в /status не удался: %s", exc)
    await callback.message.answer(
        text[:4000], parse_mode=ParseMode.HTML, reply_markup=status_menu(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def build_status_text(
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    monitor: Any,
    notifier: Any,
    user_id: int,
) -> str:
    user = await repo.get_user(user_id)
    status = await ctx.source.status()
    try:
        await repo.save_source_status(status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("save_source_status: %r", exc)

    mon_stats = monitor.stats if monitor is not None else {}
    cache = await repo.cache_stats(status.name)
    filters = await repo.list_filters(user_id)
    enabled = [row for row in filters if row.is_enabled]
    runs = await repo.last_runs(user_id, limit=4)
    weights = RankingWeights.from_settings(settings)
    if user and user.weights():
        weights = RankingWeights.from_mapping({**weights.as_dict(), **user.weights()})

    lines: list[str] = ["<b>Состояние мониторинга</b>", ""]

    # --- Источник ---
    lines.append(
        f"📡 Источник: <code>{status.name}</code> · "
        f"{'доступен' if status.available else 'НЕ доступен'} · "
        f"{'ТЕСТОВЫЕ данные' if status.is_test_data else 'реальные данные'}"
    )
    lines.append(f"   {status.message[:300]}")
    if status.last_error:
        lines.append(f"   последняя ошибка: <code>{status.last_error[:200]}</code>")
    lines.append(
        f"   запросов: {status.requests_total}, ошибок: {status.requests_failed}"
    )
    if not status.available and not status.is_test_data:
        lines.append("")
        lines.append(i18n.MRKT_NOT_CONNECTED)
    elif status.is_test_data:
        lines.append("")
        lines.append(i18n.DEMO_BANNER)

    # --- Планировщик ---
    lines += [
        "",
        f"⚙️ Планировщик: {'работает' if mon_stats.get('running') else 'остановлен'} · "
        f"циклов: {mon_stats.get('cycles', 0)} · ошибок: {mon_stats.get('errors', 0)}",
        f"   тик: {settings.poll_tick_seconds} c · минимум интервала: {settings.min_interval_minutes} мин",
    ]
    if mon_stats.get("last_cycle_at"):
        lines.append(f"   последний цикл: {mon_stats['last_cycle_at']}")
    if mon_stats.get("last_error"):
        lines.append(f"   последняя ошибка цикла: <code>{str(mon_stats['last_error'])[:200]}</code>")

    # --- Пользователь ---
    paused = bool(user and user.is_paused)
    lines += [
        "",
        f"👤 Уведомления: {'⏸ на паузе (/resume)' if paused else '▶️ включены (/pause)'}",
        f"   фильтров: {len(filters)} (включено {len(enabled)})",
    ]
    for row in enabled[:5]:
        last = ensure_aware(row.last_run_at)
        last_text = last.strftime("%d.%m %H:%M UTC") if last else "ещё не запускался"
        lines.append(
            f"   • {row.id}. {row.name}: каждые {row.interval_minutes} мин, "
            f"последний запуск {last_text}"
        )
    if not enabled:
        lines.append("   • включённых фильтров нет — мониторинг ничего не проверяет")

    # --- Доставка и кэш ---
    delivery = notifier.stats.as_dict() if notifier is not None else {}
    lines += [
        "",
        f"📨 Доставка: отправлено {delivery.get('delivered', 0)} из {delivery.get('total', 0)}, "
        f"ошибок {delivery.get('failed', 0)}",
        f"🗄 Кэш лотов: {cache.get('total', 0)} записей, активных {cache.get('active', 0)}",
    ]
    if delivery.get("last_error"):
        lines.append(f"   последняя ошибка доставки: <code>{str(delivery['last_error'])[:200]}</code>")

    # --- Веса ---
    lines += ["", "⭐ Веса ранжирования: " + weights.describe().replace("\n", " | ")]

    # --- Последние циклы ---
    if runs:
        lines += ["", "🕒 Последние циклы:"]
        for run in runs:
            started = ensure_aware(run.started_at)
            stamp = started.strftime("%d.%m %H:%M") if started else "?"
            lines.append(
                f"   • {stamp} · фильтр {run.filter_id} · {run.status} · "
                f"найдено {run.fetched}, отправлено {run.sent}, дублей {run.skipped_duplicates}"
            )
            if run.error:
                lines.append(f"     <code>{run.error[:160]}</code>")

    lines += ["", "Повторная отправка: при снижении цены ≥ "
                f"{settings.resend_price_drop_percent:g} % или возвращении лота "
                f"через {settings.resend_after_hours} ч."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
#  /pause /resume
# ----------------------------------------------------------------------------

@router.message(Command("pause"))
async def cmd_pause(message: Message, repo: Repository, monitor: Any) -> None:
    user = message.from_user
    if user is None:
        return
    await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    await repo.set_paused(user.id, True)
    await message.answer(i18n.PAUSED_TEXT, parse_mode=ParseMode.HTML)


@router.message(Command("resume"))
async def cmd_resume(message: Message, repo: Repository, monitor: Any) -> None:
    user = message.from_user
    if user is None:
        return
    await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    await repo.set_paused(user.id, False)
    if monitor is not None:
        monitor.request_tick()
    await message.answer(i18n.RESUMED_TEXT, parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------------------
#  /weights — настройка весов формулы релевантности
# ----------------------------------------------------------------------------

@router.message(Command("weights"))
async def cmd_weights(
    message: Message, repo: Repository, settings: Settings, state: FSMContext
) -> None:
    user = message.from_user
    if user is None:
        return
    await repo.touch_user(user.id, username=user.username, first_name=user.first_name)
    weights = await _user_weights(repo, settings, user.id)

    parts = message.text.split(maxsplit=1) if message.text else [""]
    if len(parts) > 1 and parts[1].strip():
        parsed, error = parse_weights_input(parts[1])
        if error:
            await message.answer(
                f"Не получилось: {error}\n\n{weights_help()}", parse_mode=ParseMode.HTML
            )
            return
        merged = {**weights.as_dict(), **parsed}
        await repo.set_weights(user.id, merged)
        weights = RankingWeights.from_mapping(merged)
        await message.answer(
            f"✅ Веса обновлены.\n\n{weights_text(weights)}",
            parse_mode=ParseMode.HTML,
            reply_markup=weights_menu(),
        )
        return

    await message.answer(
        weights_text(weights),
        parse_mode=ParseMode.HTML,
        reply_markup=weights_menu(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def weights_text(weights: RankingWeights) -> str:
    norm = weights.normalized()
    lines = [
        "<b>Веса формулы релевантности</b>",
        "<code>score = 100 · Σ(wᵢ · cᵢ) / Σ(wᵢ)</code>",
        "",
    ]
    for key in COMPONENT_ORDER:
        lines.append(
            f"• {WEIGHT_LABELS[key]}: <code>{key}</code> = {weights.as_dict()[key]:g} "
            f"(доля {norm[key] * 100:.0f} %)"
        )
    lines += [
        "",
        "Если компонент посчитать нельзя (нет ориентира цены, нет данных о цвете), "
        "его вес перераспределяется между доступными — лот не тонет из-за "
        "отсутствия данных.",
        "",
        "Задать все веса одной строкой:",
        "<code>/weights match=30 monochrome=25 value=25 confidence=10 freshness=10</code>",
        "или только один:",
        "<code>/weights value=40</code>",
    ]
    return "\n".join(lines)


def weights_help() -> str:
    return (
        "Формат: <code>match=30 monochrome=25 value=25 confidence=10 freshness=10</code>\n"
        "Допустимы любые подмножества и значения 0…100 (нормируются автоматически)."
    )


# Короткие псевдонимы, чтобы «mono=30» работало так же, как «monochrome=30».
WEIGHT_ALIASES: dict[str, str] = {
    "mono": "monochrome",
    "моно": "monochrome",
    "монo": "monochrome",
    "conf": "confidence",
    "confidence_data": "confidence",
    "надёжность": "confidence",
    "reliability": "confidence",
    "fresh": "freshness",
    "свежесть": "freshness",
    "match_score": "match",
    "filters": "match",
    "фильтры": "match",
    "value_score": "value",
    "выгода": "value",
    "benefit": "value",
    "relevance": "match",
}


def _normalize_weight_key(key: str) -> str | None:
    key = key.strip().lower().replace("-", "_")
    if key in COMPONENT_ORDER:
        return key
    return WEIGHT_ALIASES.get(key)


def _split_weight_tokens(raw: str) -> list[str]:
    """Разбить ввод на токены, не ломая европейскую десятичную запятую.

    «value=0,4, mono=0,3» -> ["value=0,4", "mono=0,3"],
    «1 2 3 4 5» и «1,2,3,4,5» -> ["1","2","3","4","5"].
    """
    if "=" not in raw:
        # Позиционный ввод: запятая — обычный разделитель («1,2,3,4,5»).
        parts = raw.replace(",", " ").split()
        return [part for part in parts if part]
    # Запятая считается разделителем, только если за ней идёт пробел/конец строки
    # или пара «ключ=значение». Иначе это десятичная запятая: «value=0,4».
    parts = re.split(r",(?=\s|$|[A-Za-zА-Яа-яЁё_-]+\s*=)", raw)
    tokens: list[str] = []
    for part in parts:
        tokens.extend(part.split())
    return tokens


def parse_weights_input(raw: str) -> tuple[dict[str, float], str | None]:
    """Разобрать строку весов. Возвращает (значения, текст ошибки)."""
    parsed: dict[str, float] = {}
    positional: list[str] = []
    for chunk in _split_weight_tokens(raw):
        token = chunk
        if "=" in token:
            raw_key, _, value = token.partition("=")
            key = _normalize_weight_key(raw_key)
            if key is None:
                return {}, (
                    f"неизвестный компонент <code>{raw_key.strip()}</code> "
                    f"(доступны: {', '.join(COMPONENT_ORDER)})"
                )
            try:
                number = float(value.replace(",", "."))
            except ValueError:
                return {}, f"не число: <code>{value}</code>"
            parsed[key] = max(0.0, min(100.0, number))
        else:
            positional.append(token)

    if positional:
        # Разрешаем positional-ввод в фиксированном порядке COMPONENT_ORDER.
        if len(positional) > len(COMPONENT_ORDER):
            return {}, "слишком много значений"
        for key, value in zip(COMPONENT_ORDER, positional):
            try:
                number = float(value.replace(",", "."))
            except ValueError:
                return {}, f"не число: <code>{value}</code>"
            parsed[key] = max(0.0, min(100.0, number))

    if not parsed:
        return {}, "пусто"
    if sum(parsed.values()) <= 0 and len(parsed) == len(COMPONENT_ORDER):
        return {}, "все веса нулевые — оценка потеряет смысл"
    return parsed, None


@router.callback_query(F.data.startswith("w:set:"))
async def cb_weight_set(callback: CallbackQuery, state: FSMContext, settings: Settings, repo: Repository) -> None:
    key = callback.data.split(":")[-1]
    if key not in COMPONENT_ORDER:
        await callback.answer("Неизвестный компонент")
        return
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    await state.set_state(WeightStates.waiting_value)
    await state.update_data(weight_key=key)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            f"<b>{WEIGHT_LABELS[key]}</b>\n"
            f"Отправьте число 0…100.\n\n{weights_help()}",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_state(),
        )


@router.message(WeightStates.waiting_value)
async def msg_weight_value(
    message: Message, state: FSMContext, repo: Repository, settings: Settings
) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    data = await state.get_data()
    key = data.get("weight_key")
    await state.clear()
    if not key:
        return
    parsed, error = parse_weights_input(f"{key}={message.text.strip()}")
    if error:
        await message.answer(f"Не получилось: {error}", parse_mode=ParseMode.HTML)
        return
    weights = await _user_weights(repo, settings, user.id)
    merged = {**weights.as_dict(), **parsed}
    await repo.set_weights(user.id, merged)
    await message.answer(
        f"✅ Вес «{WEIGHT_LABELS[key]}» обновлён.\n\n{weights_text(RankingWeights.from_mapping(merged))}",
        parse_mode=ParseMode.HTML,
        reply_markup=weights_menu(),
    )


@router.callback_query(F.data == "w:reset")
async def cb_weight_reset(callback: CallbackQuery, repo: Repository, settings: Settings) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer()
        return
    await repo.set_weights(user.id, {})
    weights = RankingWeights.from_settings(settings)
    await callback.answer("Сброшено")
    if callback.message is not None:
        await callback.message.answer(
            f"✅ Веса сброшены к значениям из .env.\n\n{weights_text(weights)}",
            parse_mode=ParseMode.HTML,
            reply_markup=weights_menu(),
        )


@router.callback_query(F.data == "w:explain")
async def cb_weight_explain(callback: CallbackQuery) -> None:
    await callback.answer()
    text = "\n".join(
        [
            "<b>Как считается релевантность</b>",
            "",
            "<code>score = 100 · Σ(wᵢ · cᵢ) / Σ(wᵢ)</code>",
            "",
            "Компоненты (каждый 0…1):",
            "• <b>match</b> — плотность соответствия фильтру: совпала ли коллекция, "
            "насколько цена близка к нижней границе, насколько редки совпавшие "
            "модель/фон/символ.",
            "• <b>monochrome</b> — балл монохромности 0…100, делённый на 100.",
            "• <b>value</b> — скидка к ориентиру цены, делённая на 100 и "
            "ограниченная диапазоном 0…1. Лот дороже ориентира даёт 0.",
            "• <b>confidence</b> — средняя уверенность оценки выгоды и "
            "монохромности (high=1.0, medium=0.6, low=0.3).",
            "• <b>freshness</b> — <code>0.5^(возраст_в_часах / период_полураспада)</code>; "
            "если дата выставления неизвестна — нейтральные 0.5.",
            "",
            "Жёсткие условия фильтра (коллекция, цена, атрибуты, монохром, пороги) "
            "отсекают лоты ДО ранжирования: веса влияют только на порядок и на "
            "прохождение порога min_relevance.",
            "",
            i18n.DISCLAIMER,
        ]
    )
    if callback.message is not None:
        await callback.message.answer(
            text[:4000], parse_mode=ParseMode.HTML, reply_markup=back("menu:status")
        )


async def _user_weights(repo: Repository, settings: Settings, user_id: int) -> RankingWeights:
    user = await repo.get_user(user_id)
    base = RankingWeights.from_settings(settings).as_dict()
    if user and user.weights():
        return RankingWeights.from_mapping({**base, **user.weights()})
    return RankingWeights.from_settings(settings)


# ----------------------------------------------------------------------------
#  /history
# ----------------------------------------------------------------------------

@router.message(Command("history"))
async def cmd_history(message: Message, repo: Repository) -> None:
    user = message.from_user
    if user is None:
        return
    rows = await repo.history(user.id, limit=15)
    if not rows:
        await message.answer(
            "История пуста: ещё ничего не отправлял.\n"
            "Создайте фильтр (/filters) и запустите /search.",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = ["<b>Отправленные лоты</b> (последние 15)", ""]
    for row in rows:
        sent = ensure_aware(row.sent_at)
        stamp = sent.strftime("%d.%m %H:%M") if sent else "?"
        diff = f"{row.diff_percent:+.1f} %" if row.diff_percent is not None else "—"
        lines.append(
            f"• {stamp} · <code>{row.listing_id[:22]}</code> · {fmt_ton(row.price_nano)} TON · "
            f"монохром {row.monochrome_score:.0f} ({row.monochrome_method or '?'}/"
            f"{row.monochrome_confidence or '?'}) · выгода {diff} · "
            f"релевантность {row.relevance:.0f} · {row.reason}"
            + (f" · повторов {row.resend_count}" if row.resend_count else "")
        )
    await message.answer(
        "\n".join(lines)[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=status_menu(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ----------------------------------------------------------------------------
#  /diag — диагностика (для администраторов)
# ----------------------------------------------------------------------------

@router.message(Command("diag"))
async def cmd_diag(
    message: Message,
    repo: Repository,
    settings: Settings,
    ctx: PipelineContext,
    is_admin: bool = False,
) -> None:
    user = message.from_user
    if user is None:
        return
    allowed, admins = settings.id_lists()
    if admins and user.id not in admins:
        await message.answer(
            "Диагностика доступна только администраторам (ADMIN_USER_IDS).",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["<b>Диагностика</b>", ""]
    status = await ctx.source.status()
    lines += [
        f"Источник: <code>{status.name}</code> ({status.kind.value})",
        f"Доступен: {'да' if status.available else 'нет'}",
        f"Тестовые данные: {'да' if status.is_test_data else 'нет'}",
        f"Сообщение: {status.message}",
    ]
    if status.detail:
        lines.append(f"Подробности: {status.detail}")
    if status.last_error:
        lines.append(f"Последняя ошибка: <code>{status.last_error[:300]}</code>")
    if status.rate_limited_until:
        lines.append(f"Ограничение частоты действует до: {status.rate_limited_until.isoformat()}")

    # Справочники источника
    try:
        collections = await ctx.source.collections()
        lines.append(f"Коллекций в источнике: {len(collections)}")
        if collections:
            lines.append("Примеры: <code>" + ", ".join(collections[:8]) + "</code>")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Список коллекций недоступен: <code>{str(exc)[:160]}</code>")

    # Пробная выдача
    from app.domain import FetchRequest

    try:
        result = await ctx.source.fetch(FetchRequest(limit=3))
        lines.append(f"Пробная выдача: {len(result.listings)} лот(ов), полная={result.complete}")
        for item in result.listings[:3]:
            lines.append(
                f"  • {item.collection_name} · {item.model_name or '—'} · "
                f"{fmt_ton(item.price_nano)} {item.currency} · id <code>{item.id[:20]}</code>"
            )
        if result.errors:
            lines.append("  ошибки: " + "; ".join(result.errors[:2]))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Пробная выдача не удалась: <code>{str(exc)[:200]}</code>")

    # @GiftChanges
    changes = getattr(ctx.images, "_changes", None)
    if changes is not None:
        health = await changes.health()
        if health.get("ok"):
            data = health.get("data") or {}
            lines.append(
                f"@GiftChanges: доступен · подарков {data.get('gifts', {}).get('total', '?') if isinstance(data.get('gifts'), dict) else '?'}"
            )
        else:
            lines.append(f"@GiftChanges: недоступен · {health.get('error', '')}")
    else:
        lines.append("@GiftChanges: отключён (CHANGES_API_ENABLED=false)")

    lines += [
        "",
        f"БД: <code>{settings.database_url.split('://', 1)[0]}</code>",
        f"Кэш: {await repo.cache_stats(status.name)}",
        "",
        "Авто-вход в MRKT:",
    ]
    try:
        from app.sources.mrkt_auth import session_credentials_ready, telethon_available

        lines.append(
            f"  телеграм-креденшелы: {'заданы' if session_credentials_ready(settings) else 'не заданы'}; "
            f"telethon: {'установлен' if telethon_available() else 'не установлен'}"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  не удалось проверить: {exc}")
    lines.append(
        f"  MRKT_TOKEN: {'задан' if settings.mrkt_token else 'пуст'} · "
        f"шаблон ссылки: <code>{settings.mrkt_link_template}</code>"
    )

    await message.answer(
        "\n".join(lines)[:4000],
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
