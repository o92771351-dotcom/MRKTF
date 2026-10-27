"""Необязательное авто-обновление токена MRKT через пользовательскую сессию Telegram.

Как это работает (протокол подтверждён открытыми клиентами amrkt / mrkts и
неофициальной документацией boostNT/MRKT-API):

1. Реальный Telegram-аккаунт открывает мини-приложение @mrkt (short_name="app")
   через MTProto-вызов `messages.requestAppWebView`.
2. Telegram возвращает URL с подписанным `tgWebAppData` (это initData).
3. `POST https://api.tgmrkt.io/api/v1/auth` с телом `{"data": <initData>}`
   возвращает `{"token": "..."}`.
4. Токен передаётся в заголовке `Authorization` (и в cookie `access_token`).

Срок жизни токена публично не документирован; по наблюдениям сообщества —
более суток. Поэтому токен кэшируется в памяти и обновляется только когда API
отвечает 401/403/404.

Модуль не обязателен: если telethon не установлен, `build_token_provider`
вернёт None, и бот будет работать с токеном из `MRKT_TOKEN` (или на демо-источнике).

ВНИМАНИЕ. Это вход в MRKT от имени вашего личного аккаунта. Используйте
отдельный («служебный») аккаунт, храните файл сессии в секрете и соблюдайте
условия использования площадки. Бот не совершает покупок и не изменяет лоты:
используются только чтения (`/gifts/saling`, `/gifts/collections`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

import aiohttp

from app.config import Settings

logger = logging.getLogger(__name__)

MRKT_BOT_USERNAME = "mrkt"
MRKT_APP_SHORT_NAME = "app"


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def session_credentials_ready(settings: Settings) -> bool:
    return bool(settings.tg_api_id and settings.tg_api_hash and settings.tg_session_name)


async def request_init_data(settings: Settings) -> str:
    """Открыть мини-приложение @mrkt служебным аккаунтом и вернуть initData."""
    from telethon import TelegramClient
    from telethon.tl.functions.messages import RequestAppWebViewRequest
    from telethon.tl.types import InputBotAppShortName

    client = TelegramClient(
        settings.tg_session_name,
        int(settings.tg_api_id),
        settings.tg_api_hash,
        base_dir=settings.tg_session_workdir or None,
    )
    await client.start()
    try:
        entity = await client.get_entity(MRKT_BOT_USERNAME)
        app = InputBotAppShortName(bot_id=entity, short_name=MRKT_APP_SHORT_NAME)
        result: Any = await client(
            RequestAppWebViewRequest(peer=entity, app=app, platform="android", write_allowed=True)
        )
        url = getattr(result, "url", "") or ""
        if "tgWebAppData=" not in url:
            raise RuntimeError("в ответе Telegram нет tgWebAppData")
        raw = url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0]
        init_data = unquote(raw)
        if not init_data:
            raise RuntimeError("пустой initData")
        return init_data
    finally:
        await client.disconnect()


async def exchange_init_data(init_data: str, settings: Settings) -> str:
    """Обменять initData на токен MRKT (POST /auth)."""
    url = f"{settings.mrkt_base_url.rstrip('/')}/auth"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://cdn.tgmrkt.io",
        "Referer": settings.mrkt_referer,
    }
    timeout = aiohttp.ClientTimeout(total=settings.mrkt_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json={"data": init_data}, headers=headers) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"MRKT /auth вернул HTTP {response.status}: {body[:160]}")
            try:
                import json

                data = json.loads(body)
            except ValueError as exc:
                raise RuntimeError(f"MRKT /auth вернул не JSON: {body[:160]}") from exc
    token = (data or {}).get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError(f"в ответе MRKT /auth нет поля token: {str(data)[:200]}")
    return str(token)


async def fetch_token_via_session(settings: Settings) -> str:
    """Полный цикл: initData из Telegram -> токен MRKT."""
    init_data = await request_init_data(settings)
    return await exchange_init_data(init_data, settings)


def build_token_provider(settings: Settings) -> Callable[[], Awaitable[str | None]] | None:
    """Провайдер токена для MrktSource, или None если авто-вход недоступен.

    Токен кэшируется: повторные вызовы в течение `token_ttl` отдают кэш,
    чтобы не дёргать Telegram на каждую ошибку.
    """
    if not session_credentials_ready(settings):
        logger.info(
            "Авто-вход в MRKT выключен: не заданы TG_API_ID / TG_API_HASH / TG_SESSION_NAME."
        )
        return None
    if not telethon_available():
        logger.warning(
            "Авто-вход в MRKT настроен, но пакет telethon не установлен. "
            "Установите его (pip install telethon) или задайте MRKT_TOKEN вручную."
        )
        return None

    lock = asyncio.Lock()
    last_token: dict[str, Any] = {"value": None}

    async def provider() -> str | None:
        """Один одновременный вход; результат запоминается для логов/диагностики."""
        async with lock:
            try:
                token = await fetch_token_via_session(settings)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось получить токен MRKT через сессию: %r", exc)
                return None
            last_token["value"] = token
            logger.info("Получен новый токен MRKT через служебную сессию")
            return token

    return provider
