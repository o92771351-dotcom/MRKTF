"""Смоук-тест сборки приложения: диспетчер, роутеры, команды, middleware.

Здесь не проверяется работа с Telegram — только то, что приложение
собирается без ошибок импорта/регистрации и что обязательные тексты
(атрибуция @GiftChanges, дисклеймеры) действительно существуют.
"""

from __future__ import annotations

import pytest
from aiogram.filters.command import CommandStart

from app.bot import i18n
from app.bot.app import App, create_app
from app.bot.handlers import routers
from app.config import Settings
from app.db.db import Database
from app.sources.factory import build_source

FAKE_TOKEN = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake00"

EXPECTED_COMMANDS = {"start", "help", "filters", "search", "status", "pause", "resume"}


def _registered_commands() -> set[str]:
    """Собрать все команды, зарегистрированные в роутерах (без запуска бота)."""
    found: set[str] = set()
    for router in routers:
        for entry in list(router.message.handlers):
            for item in getattr(entry, "filters", []) or []:
                callback = getattr(item, "callback", None)
                if isinstance(callback, CommandStart):
                    found.add("start")
                commands = getattr(callback, "commands", None)
                if commands:
                    found.update(str(command) for command in commands)
    return found


def test_expected_command_surface_is_registered() -> None:
    """Спецификация требует /start /filters /search /status /pause /resume /help."""
    registered = _registered_commands()
    missing = EXPECTED_COMMANDS - registered
    assert not missing, f"не зарегистрированы команды: {missing} (найдено: {sorted(registered)})"


def test_extra_commands_are_documented() -> None:
    """Дополнительные команды должны быть упомянуты в /help, иначе их никто не найдёт."""
    extra = _registered_commands() - EXPECTED_COMMANDS
    for command in sorted(extra):
        assert f"/{command}" in i18n.HELP_TEXT, f"/{command} не описана в /help"


def test_routers_have_unique_names() -> None:
    names = [router.name for router in routers]
    assert len(names) == len(set(names)), names
    assert len(routers) >= 3


def test_i18n_contains_mandatory_texts() -> None:
    """Атрибуция @GiftChanges обязательна по правилам api.changes.tg."""
    assert "GiftChanges" in i18n.CREDITS_LINE
    assert "api.changes.tg" in i18n.CREDITS_LINE
    assert "GiftChanges" in i18n.HELP_TEXT
    assert "гарант" in i18n.DISCLAIMER.lower() or "прибыл" in i18n.DISCLAIMER.lower()
    assert "тестов" in i18n.DEMO_BANNER.lower()
    assert "не подкл" in i18n.MRKT_NOT_CONNECTED.lower() or "mrkt" in i18n.MRKT_NOT_CONNECTED.lower()


def test_help_mentions_no_autobuy_and_monitoring_scope() -> None:
    assert "/start" in i18n.HELP_TEXT and "/filters" in i18n.HELP_TEXT


async def test_app_builds_dispatcher_with_all_dependencies(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        bot_token=FAKE_TOKEN,
        source="demo",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        changes_api_enabled=False,
    )
    db = Database(settings.database_url)
    await db.create_all()
    bundle = build_source(settings)
    app = await create_app(settings, db, bundle)
    try:
        assert isinstance(app, App)
        assert app.bot is not None and app.dp is not None
        assert app.monitor is not None and app.notifier is not None

        dp = app.dp
        # Зависимости прокинуты в workflow_data и доступны хендлерам.
        for key in ("settings", "db", "ctx", "notifier", "monitor", "app"):
            assert key in dp.workflow_data, key
        assert dp.workflow_data["monitor"] is app.monitor

        # Роутеры подключены, middleware регистрации выполнены.
        assert len(dp.sub_routers) == len(routers)
        # Middleware висят на update-наблюдателе диспетчера и применяются ко всем
        # вложенным роутерам: доступ, сессия БД/репозиторий, флаг администратора.
        registered = [type(item).__name__ for item in dp.update.middleware._middlewares]
        assert "AccessMiddleware" in registered
        assert "RepositoryMiddleware" in registered
        assert "AdminMiddleware" in registered

        # Источник — демо, и он честно помечает данные как тестовые.
        status = await app.ctx.source.status()
        assert status.is_test_data is True
        assert status.available is True
    finally:
        await app.bot.session.close()
        await app.source.close()
        await db.close()


async def test_app_requires_bot_token() -> None:
    settings = Settings(_env_file=None, bot_token="", source="demo")
    db = Database(settings.database_url)
    source, changes, images = build_source(settings)
    app = App(settings=settings, db=db, source=source, changes=changes, images=images)
    try:
        with pytest.raises(RuntimeError) as info:
            await app.build()
        assert "BOT_TOKEN" in str(info.value)
    finally:
        await source.close()
        await db.close()
