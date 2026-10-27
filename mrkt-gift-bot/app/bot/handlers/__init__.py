"""Роутеры обработчиков."""

from app.bot.handlers.basic import router as basic_router
from app.bot.handlers.filters_ui import router as filters_router
from app.bot.handlers.status import router as status_router

routers = (basic_router, filters_router, status_router)

__all__ = ["routers", "basic_router", "filters_router", "status_router"]
