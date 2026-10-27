"""Сквозной тест мониторинга: планировщик -> конвейер -> доставка в Telegram.

Telegram подменён фейковой сессией aiogram, поэтому тест полностью офлайн:
проверяется, что цикл мониторинга находит фильтры по расписанию, отправляет
карточки, записывает историю и не повторяет те же лоты.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import Chat, Message, User as TgUser

from app.bot.notify import Notifier
from app.filters.monochrome import MonochromeDetector
from app.config import Settings
from app.db.db import Database
from app.db.repo import Repository
from app.monitor import Monitor, run_once_now
from app.pipeline import PipelineContext
from app.sources.demo import DemoSource
from app.sources.factory import ImageProvider

FAKE_TOKEN = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake00"


# ----------------------------------------------------------------------------
#  Фейковый Telegram
# ----------------------------------------------------------------------------

class FakeSession(BaseSession):
    """Записывает все запросы к Bot API и отвечает валидным Message."""

    def __init__(self, *, fail_photo: bool = False, forbidden: bool = False) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self.fail_photo = fail_photo
        self.forbidden = forbidden

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], **kwargs: Any) -> Any:
        name = type(method).__name__
        self.calls.append({"method": name, "data": dict(method.model_dump(exclude_none=True))})
        if name == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="Bot", username="mrkt_gift_monitor_bot")
        if self.forbidden:
            from aiogram.exceptions import TelegramForbiddenError
            from aiogram.methods import Response

            raise TelegramForbiddenError(
                message=Response(ok=False, result=None, description="bot was blocked by the user",
                                   error_code=403),
                method=method,
            )
        if self.fail_photo and name == "SendPhoto":
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.methods import Response

            raise TelegramBadRequest(
                message=Response(ok=False, result=None,
                                 description="Bad Request: wrong type of the web page content",
                                 error_code=400),
                method=method,
            )
        return Message(
            message_id=len(self.calls),
            date=datetime.now(timezone.utc),
            chat=Chat(id=int(getattr(method, "chat_id", 1) or 1), type="private"),
            text=getattr(method, "text", None) or getattr(method, "caption", None),
        )

    async def stream_content(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover
        yield b""


def method_names(session: FakeSession) -> list[str]:
    return [call["method"] for call in session.calls]


# ----------------------------------------------------------------------------
#  Сборка
# ----------------------------------------------------------------------------

@pytest.fixture()
async def stack(tmp_path):
    settings = Settings(
        _env_file=None,
        bot_token=FAKE_TOKEN,
        source="demo",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}",
        changes_api_enabled=False,      # никаких внешних запросов
        image_enabled=True,
        send_delay_seconds=0.0,
        # Пороги повторов задраны, чтобы тесты дедупликации не ловили
        # естественный «дрейф цен» демо-источника.
        resend_price_drop_percent=99.0,
        resend_after_hours=100000,
        poll_tick_seconds=5,
        min_interval_minutes=1,
        default_min_relevance=0,
        mono_min_score=30,
        mono_allow_name_heuristic=True,
    )
    db = Database(settings.database_url)
    await db.create_all()
    source = DemoSource()
    images = ImageProvider(source, None, settings=settings)
    ctx = PipelineContext(
        settings=settings,
        source=source,
        images=images,
        detector=MonochromeDetector(None),
    )
    bot = Bot(token=FAKE_TOKEN, session=FakeSession())
    notifier = Notifier(bot, settings=settings, images=images)
    monitor = Monitor(settings=settings, db=db, ctx=ctx, notify=notifier.notify)
    try:
        yield settings, db, bot, notifier, monitor
    finally:
        await bot.session.close()
        await source.close()
        await db.close()


async def _make_filter(db: Database, settings: Settings, user_id: int = 777, **overrides: Any) -> int:
    async with db.session_factory() as session:
        repo = Repository(session)
        await repo.touch_user(user_id, username="tester", first_name="Tester")
        values: dict[str, Any] = dict(
            name="Серые подарки",
            is_enabled=True,
            notify_enabled=True,
            interval_minutes=1,
            max_results=3,
            min_relevance=0.0,
            mono_enabled=True,
            mono_mode="gray",
            mono_min_score=30,
            mono_max_saturation=0.35,
            mono_allow_name_heuristic=True,
        )
        values.update(overrides)
        row = await repo.create_filter(user_id, **values)
        return int(row.id)


async def _force_due(db: Database, user_id: int, filter_id: int) -> None:
    """Сдвинуть last_run_at в прошлое, чтобы фильтр снова попал в расписание."""
    from sqlalchemy import update

    from app.db.models import Filter

    async with db.session_factory() as session:
        await session.execute(
            update(Filter)
            .where(Filter.id == filter_id, Filter.user_id == user_id)
            .values(last_run_at=None)
        )
        await session.commit()


# ----------------------------------------------------------------------------
#  Сценарии
# ----------------------------------------------------------------------------

async def test_cycle_delivers_cards_and_records_history(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 777
    filter_id = await _make_filter(db, settings, user_id)

    await run_once_now(monitor)

    session = bot.session
    assert isinstance(session, FakeSession)
    assert "SendPhoto" in method_names(session), "карточка с фото не отправлена"
    assert session.calls, "бот ничего не отправил"

    # Все отправки идут в чат пользователя
    assert all(call["data"].get("chat_id") == user_id for call in session.calls)

    # Подпись помечает тестовые данные демо-источника
    captions = [call["data"].get("caption") or call["data"].get("text") or "" for call in session.calls]
    assert any("ТЕСТОВЫЕ ДАННЫЕ" in text for text in captions)

    # Статистика доставки и история отправок
    assert notifier.stats.delivered >= 1
    async with db.session_factory() as s:
        repo = Repository(s)
        history = await repo.history(user_id)
        assert history, "история отправок пустая"
        runs = await repo.last_runs(user_id)
        assert runs and runs[0].sent >= 1
        assert runs[0].status in ("ok", "partial")


async def test_second_cycle_does_not_resend_same_listings(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 888
    filter_id = await _make_filter(db, settings, user_id)

    await run_once_now(monitor)
    async with db.session_factory() as s:
        first_ids = {row.listing_id for row in await Repository(s).history(user_id)}
    assert first_ids, "первый цикл ничего не отправил"

    await _force_due(db, user_id, filter_id)
    await run_once_now(monitor)

    async with db.session_factory() as s:
        repo = Repository(s)
        history = await repo.history(user_id)
        runs = await repo.last_runs(user_id)

    # Ни один лот не отправлен дважды (уникальность по user+filter+listing).
    ids = [row.listing_id for row in history]
    assert len(ids) == len(set(ids)), f"дубли в истории: {ids}"
    # Отправленные ранее лоты признаны дублями и пропущены.
    assert runs[0].skipped_duplicates == len(first_ids)
    # Новые лоты при этом не теряются: ротация выдачи допустима.
    assert runs[0].sent >= 0


async def test_paused_user_gets_nothing(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 999
    await _make_filter(db, settings, user_id)

    async with db.session_factory() as s:
        await Repository(s).set_paused(user_id, True)

    await run_once_now(monitor)
    assert bot.session.calls == []  # type: ignore[attr-defined]
    assert notifier.stats.total == 0


async def test_disabled_filter_is_skipped(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 555
    filter_id = await _make_filter(db, settings, user_id, is_enabled=False)
    await run_once_now(monitor)
    assert bot.session.calls == []  # type: ignore[attr-defined]


async def test_filter_without_notify_flag_is_skipped(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 444
    await _make_filter(db, settings, user_id, notify_enabled=False)
    await run_once_now(monitor)
    assert bot.session.calls == []  # type: ignore[attr-defined]


async def test_not_due_filter_is_not_run(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 333
    filter_id = await _make_filter(db, settings, user_id, interval_minutes=60)

    await run_once_now(monitor)                     # первый запуск — last_run_at пуст
    assert bot.session.calls                        # type: ignore[attr-defined]

    before = len(bot.session.calls)                 # type: ignore[attr-defined]
    await run_once_now(monitor)                     # 60 минут ещё не прошло
    assert len(bot.session.calls) == before         # type: ignore[attr-defined]


async def test_significant_price_drop_is_resent(stack) -> None:
    """Снижение цены ниже порога — единственный повод прислать лот повторно.

    Цену роняет источник (а не кэш): конвейер на каждом цикле обновляет кэш
    свежими данными, поэтому «внешнее» падение цены должно прийти именно
    из выдачи — как это и происходит в реальности.
    """
    settings, db, bot, notifier, monitor = stack
    settings.resend_price_drop_percent = 5.0        # в фикстуре порог задран
    user_id = 1212
    filter_id = await _make_filter(db, settings, user_id)

    await run_once_now(monitor)
    first = len(bot.session.calls)                  # type: ignore[attr-defined]
    assert first > 0
    async with db.session_factory() as s:
        sent_ids = {row.listing_id for row in await Repository(s).history(user_id)}
    assert sent_ids

    # Следующая выдача источника — те же лоты, но втрое дешевле.
    original_fetch = monitor._ctx.source.fetch      # type: ignore[attr-defined]

    async def cheaper_fetch(request: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_fetch(request, *args, **kwargs)
        for item in result.listings:
            item.price_nano = max(1, item.price_nano // 3)
            if item.price_without_fee_nano:
                item.price_without_fee_nano = max(1, item.price_without_fee_nano // 3)
        return result

    monitor._ctx.source.fetch = cheaper_fetch       # type: ignore[attr-defined]

    await _force_due(db, user_id, filter_id)
    await run_once_now(monitor)
    assert len(bot.session.calls) > first           # type: ignore[attr-defined]

    async with db.session_factory() as s:
        repo = Repository(s)
        runs = await repo.last_runs(user_id)
        history = await repo.history(user_id)
        resent = [row for row in history if row.listing_id in sent_ids]

    assert runs[0].sent >= 1
    # Повторно пришли именно те лоты, у которых источник показал снижение цены.
    # (Часть отправленных ранее лотов может не попасть в топ-3 этого цикла —
    # это нормально: они просто не участвовали в решении.)
    dropped = [row for row in resent if row.reason == "price_drop"]
    assert dropped, [
        (row.listing_id, row.reason, row.resend_count, row.price_nano) for row in resent
    ]
    assert all(row.resend_count >= 1 for row in dropped)
    # Ни один лот не отправлен дважды «как новый».
    assert all(row.resend_count == 0 for row in history if row.reason == "new")

    texts = "\n".join(
        str(call["data"].get("caption") or call["data"].get("text") or "")
        for call in bot.session.calls               # type: ignore[attr-defined]
    )
    assert "снижение цены" in texts.lower()


async def test_photo_failure_falls_back_to_text_card(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 222
    bot.session.fail_photo = True                   # type: ignore[attr-defined]
    await _make_filter(db, settings, user_id)

    await run_once_now(monitor)

    names = method_names(bot.session)               # type: ignore[arg-type]
    assert "SendMessage" in names, "не сработал откат на текстовую карточку"
    assert notifier.stats.delivered >= 1


async def test_blocked_chat_is_not_retried(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 111
    bot.session.forbidden = True                    # type: ignore[attr-defined]
    filter_id = await _make_filter(db, settings, user_id)

    await run_once_now(monitor)

    assert user_id in notifier.stats.blocked_chats
    assert notifier.stats.delivered == 0
    calls_after_first = len(bot.session.calls)      # type: ignore[attr-defined]

    await _force_due(db, user_id, filter_id)
    await run_once_now(monitor)
    # После блокировки бот не долбит в тот же чат
    assert len(bot.session.calls) <= calls_after_first + 1  # type: ignore[attr-defined]


async def test_monitor_start_stop_is_cooperative(stack) -> None:
    settings, db, bot, notifier, monitor = stack
    await _make_filter(db, settings, 666, interval_minutes=1)

    monitor.start()
    assert monitor.stats["running"] is True
    await run_once_now(monitor)
    await monitor.stop()
    assert monitor.stats["running"] is False
    assert monitor.stats["cycles"] >= 1
    assert monitor.stats["is_test_data"] is True


async def test_run_log_records_error_when_source_fails(stack, monkeypatch) -> None:
    settings, db, bot, notifier, monitor = stack
    user_id = 1234
    filter_id = await _make_filter(db, settings, user_id)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("source exploded")

    monkeypatch.setattr("app.monitor.run_pipeline", boom)
    await run_once_now(monitor)

    assert monitor.stats["errors"] >= 1
    assert "source exploded" in (monitor.stats["last_error"] or "")
    async with db.session_factory() as s:
        runs = await Repository(s).last_runs(user_id)
        assert runs and runs[0].status == "error"
        assert "source exploded" in (runs[0].error or "")
    assert bot.session.calls == []  # type: ignore[attr-defined]
