#!/usr/bin/env python3
"""Точка входа: запуск Telegram-бота мониторинга предложений MRKT.

    python main.py            # запуск
    python main.py --check    # только диагностика источника и выход
    python main.py --token    # получить токен MRKT через служебную Telegram-сессию
    python main.py --token --probe-token "eyJ..."   # проверить уже имеющийся токен

Корректная остановка: SIGINT/SIGTERM -> dp.stop_polling -> shutdown-хуки
(мониторинг завершает текущий цикл, закрываются HTTP-сессии, движок БД).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Возможность запуска из корня проекта без установки пакета.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings  # noqa: E402
from app.db.db import init_database  # noqa: E402
from app.sources.factory import build_source, close_all  # noqa: E402


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # Сторонние библиотеки шумят на INFO — прибираем.
    for noisy in ("aiohttp.access", "asyncio", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def token_flow(args: argparse.Namespace) -> int:
    """Получить/проверить токен MRKT. Возвращает код выхода (0 = успех)."""
    from app.sources.token import (
        TokenError,
        auto_login_readiness,
        mask_token,
        obtain_token,
        probe_token,
        write_env_token,
    )

    settings = get_settings()
    token = (args.probe_token or "").strip()
    exit_code = 0

    if token:
        print(f"Проверяю токен {mask_token(token)}…")
    else:
        ready, why = auto_login_readiness(settings)
        if not ready:
            print("Авто-вход недоступен:", why)
            print("")
            print("Вариант без telethon — взять токен вручную:")
            print("  1. Telegram Desktop → @mrkt → запустить мини-приложение.")
            print("  2. В окне мини-приложения: правый клик → Inspect "
                  "(или Ctrl+Shift+I / Cmd+Opt+I) → вкладка Network.")
            print("  3. Любой запрос к api.tgmrkt.io → Request Headers → Authorization.")
            print("  4. Скопировать значение целиком в .env: MRKT_TOKEN=...")
            print('  5. Проверить: python main.py --token --probe-token "ВСТАВЬТЕ_ТОКЕН"')
            return 2
        print("Авто-вход: открываю мини-приложение @mrkt служебной сессией…")
        print("Если сессия создаётся впервые, telethon спросит телефон и код из Telegram.")
        try:
            token = await obtain_token(settings)
        except TokenError as exc:
            print(f"Не удалось получить токен: {exc}")
            return 1
        print(f"Токен получен: {mask_token(token)}")

    probe = await probe_token(token, settings)
    if probe.ok:
        print(f"✅ Токен принят: HTTP {probe.status}, схема «{probe.scheme}». {probe.message}")
    else:
        print(f"❌ Токен не работает: {probe.message}")
        if not args.write_env:
            print("В .env ничего не записано.")
            return 1
        exit_code = 1

    if args.write_env and probe.ok:
        env_path = write_env_token(token, args.env_file, source="mrkt")
        print(f"Записано в {env_path}: MRKT_TOKEN=… и SOURCE=mrkt "
              f"(права 600). Токен в консоль не выводится целиком.")
        print("Дальше: python main.py --check")
    elif probe.ok and not args.write_env:
        print("Чтобы сохранить токен в .env, повторите с флагом --write-env.")

    return exit_code


async def check_source() -> int:
    """Диагностика источника без запуска бота. Код возврата = число проблем."""
    settings = get_settings()
    source, changes, images = build_source(settings)
    problems = 0
    try:
        status = await source.status()
        print(f"Источник:        {status.name} ({status.kind.value})")
        print(f"Доступен:        {'да' if status.available else 'НЕТ'}")
        print(f"Тестовые данные: {'да' if status.is_test_data else 'нет'}")
        print(f"Сообщение:       {status.message}")
        if status.detail:
            print(f"Подробности:     {status.detail}")
        if status.last_error:
            print(f"Ошибка:          {status.last_error}")
        if not status.available:
            problems += 1

        from app.domain import FetchRequest

        result = await source.fetch(FetchRequest(limit=3))
        print(f"Пробная выдача:  {len(result.listings)} лот(ов), полная={result.complete}")
        for item in result.listings:
            price = item.price_nano / 1_000_000_000
            print(
                f"  - {item.collection_name} | {item.model_name or '-'} | "
                f"{item.backdrop_name or '-'} | {price:.2f} {item.currency} | id={item.id}"
            )
        if not result.listings and status.available:
            print("ВНИМАНИЕ: источник доступен, но лотов не вернул.")
            problems += 1

        if changes is not None:
            health = await changes.health()
            print(f"@GiftChanges:    {'доступен' if health.get('ok') else 'НЕдоступен'} "
                  f"{health.get('error', '')}")
            if not health.get("ok"):
                problems += 1
    finally:
        await close_all(source, changes)
    return problems


async def run_bot() -> None:
    settings = get_settings()
    db = await init_database(settings.database_url, echo=settings.db_echo)
    bundle = build_source(settings)
    source, changes, images = bundle

    from app.bot.app import App

    app = App(settings=settings, db=db, source=source, changes=changes, images=images)
    bot = await app.build()
    assert app.dp is not None

    logging.getLogger(__name__).info(
        "Запуск polling (источник=%s, БД=%s)", settings.source, settings.database_url.split("://")[0]
    )
    try:
        await app.dp.start_polling(bot, allowed_updates=app.dp.resolve_used_update_types())
    finally:
        # start_polling сам вызывает shutdown-хук, здесь — страховка.
        await close_all(source, changes)


def main() -> int:
    parser = argparse.ArgumentParser(description="MRKT Gift Monitor Bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить источник данных и выйти (бот не запускается)",
    )
    parser.add_argument(
        "--token",
        action="store_true",
        help="получить токен MRKT через служебную Telegram-сессию (нужны TG_API_ID/TG_API_HASH и telethon)",
    )
    parser.add_argument(
        "--probe-token",
        metavar="TOKEN",
        default="",
        help="проверить уже имеющийся токен MRKT (вместо получения нового)",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="записать полученный/проверенный токен в .env (MRKT_TOKEN и SOURCE=mrkt)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="путь к env-файлу для --write-env (по умолчанию .env)",
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    if args.token or args.probe_token:
        return asyncio.run(token_flow(args))

    if args.check:
        problems = asyncio.run(check_source())
        if problems:
            print(f"\nОбнаружено проблем: {problems}. Подробнее — README, раздел 5.")
            return 1
        print("\nИсточник данных в порядке.")
        return 0

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем")
        return 0
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
