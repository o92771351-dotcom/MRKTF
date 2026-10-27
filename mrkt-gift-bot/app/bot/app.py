"""Сборка приложения: бот, диспетчер, зависимости, планировщик, жизненный цикл."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.deps import setup_middlewares
from app.bot.handlers import routers
from app.bot.notify import Notifier
from app.config import Settings
from app.db.db import Database
from app.filters.monochrome import MonochromeDetector
from app.monitor import Monitor
from app.pipeline import PipelineContext
from app.sources.base import ListingSource
from app.sources.factory import ImageProvider, ChangesTGClient

logger = logging.getLogger(__name__)


class App:
    """Контейнер зависимостей. Создаётся один раз и прокидывается в workflow_data."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        source: ListingSource,
        changes: ChangesTGClient | None,
        images: ImageProvider,
    ) -> None:
        self.settings = settings
        self.db = db
        self.source = source
        self.changes = changes
        self.images = images
        self.detector = MonochromeDetector(changes, image_timeout=settings.mono_image_analysis_timeout)
        self.ctx = PipelineContext(
            settings=settings, source=source, images=images, detector=self.detector
        )
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.notifier: Notifier | None = None
        self.monitor: Monitor | None = None

    # ------------------------------------------------------------------ сборка
    async def build(self) -> Bot:
        if not self.settings.bot_token:
            raise RuntimeError(
                "BOT_TOKEN не задан. Получите токен у @BotFather и пропишите его в .env "
                "(см. README, раздел 4)."
            )
        bot = Bot(
            token=self.settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.bot = bot
        self.notifier = Notifier(bot, settings=self.settings, images=self.images)

        dp = Dispatcher(
            storage=MemoryStorage(),
            settings=self.settings,
            db=self.db,
            ctx=self.ctx,
            notifier=self.notifier,
            monitor=None,  # подставим ниже, после создания Monitor
            app=self,
        )
        self.dp = dp

        setup_middlewares(dp, db=self.db, settings=self.settings)
        for router in routers:
            dp.include_router(router)

        self.monitor = Monitor(
            settings=self.settings,
            db=self.db,
            ctx=self.ctx,
            notify=self.notifier.notify,
        )
        dp["monitor"] = self.monitor

        dp.startup.register(self._on_startup)
        dp.shutdown.register(self._on_shutdown)
        return bot

    # ------------------------------------------------------- жизненный цикл
    async def _on_startup(self, *args: Any, **kwargs: Any) -> None:
        me = None
        try:
            me = await self.bot.get_me()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось проверить BOT_TOKEN: %r", exc)
            raise
        logger.info("Бот @%s (id=%s) запущен", me.username, me.id)

        status = await self.ctx.source.status()
        try:
            async with self.db.session_factory() as session:
                from app.db.repo import Repository

                await Repository(session).save_source_status(status)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось сохранить статус источника: %r", exc)

        logger.info(
            "Источник данных: %s · доступен=%s · тестовые данные=%s · %s",
            status.name,
            status.available,
            status.is_test_data,
            status.message,
        )
        if status.is_test_data:
            logger.warning(
                "Работает ДЕМО-источник: все лоты тестовые и помечаются в карточках. "
                "Для реальных данных настройте SOURCE=mrkt и MRKT_TOKEN."
            )
        if not status.available and not status.is_test_data:
            logger.warning(
                "Реальный источник недоступен: %s Бот не будет выдумывать данные.",
                status.detail or status.message,
            )

        if self.monitor is not None:
            self.monitor.start()

    async def _on_shutdown(self, *args: Any, **kwargs: Any) -> None:
        logger.info("Остановка…")
        if self.monitor is not None:
            await self.monitor.stop()
        if self.changes is not None:
            await self.changes.close()
        await self.source.close()
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Закрытие сессии бота: %r", exc)
        await self.db.close()
        logger.info("Остановлено корректно")


async def create_app(settings: Settings, db: Database, source_bundle: tuple[Any, Any, ImageProvider]) -> App:
    source, changes, images = source_bundle
    app = App(settings=settings, db=db, source=source, changes=changes, images=images)
    await app.build()
    return app
