"""Инструменты для быстрого получения и проверки токена MRKT.

Три сценария (по возрастанию автоматизации):

1. `python main.py --token` — служебная Telegram-сессия сама открывает
   мини-приложение @mrkt, обменивает initData на токен и (по желанию) пишет
   его в `.env`. Нужны `TG_API_ID` / `TG_API_HASH` и установленный telethon.
2. Вручную из браузера/Telegram Desktop DevTools — скопировать значение
   заголовка `Authorization` любого запроса к `api.tgmrkt.io` и вставить в
   `.env` как `MRKT_TOKEN=...`.
3. `MRKT_TOKEN` уже есть — проверить его: `probe_token()` (или
   `python main.py --check` после `SOURCE=mrkt`).

Функции ниже ничего не печатают и не читают stdin — их можно использовать
и из кода, и из тестов.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from app.config import Settings
from app.sources.mrkt_auth import (
    exchange_init_data,
    request_init_data,
    session_credentials_ready,
    telethon_available,
)

logger = logging.getLogger(__name__)

TOKEN_ENV_KEY = "MRKT_TOKEN"
SOURCE_ENV_KEY = "SOURCE"


class TokenError(RuntimeError):
    """Ошибка получения/проверки токена с человекочитаемым объяснением."""


@dataclass(slots=True)
class TokenProbe:
    """Результат проверки токена пробным запросом."""

    ok: bool
    status: int | None = None
    scheme: str = ""
    message: str = ""
    listings_total: int | None = None


def mask_token(token: str) -> str:
    """Показать токен в логах так, чтобы его нельзя было украсть из скриншота."""
    raw = (token or "").strip()
    if not raw:
        return "<пусто>"
    if len(raw) <= 12:
        return "*" * len(raw)
    return f"{raw[:6]}…{raw[-4:]} (длина {len(raw)})"


# ----------------------------------------------------------------------------
#  Проверка токена
# ----------------------------------------------------------------------------

def _probe_headers(token: str, scheme: str, settings: Settings) -> dict[str, str]:
    value = f"Bearer {token}" if scheme == "bearer" else token
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://cdn.tgmrkt.io",
        "Referer": settings.mrkt_referer,
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Authorization": value,
        "Cookie": f"access_token={token}",
    }


async def probe_token(
    token: str,
    settings: Settings,
    *,
    session: Any | None = None,
    schemes: tuple[str, ...] = ("raw", "bearer"),
) -> TokenProbe:
    """Проверить токен минимальным запросом `POST /gifts/saling` (count=1).

    Без валидного токена MRKT отвечает на закрытые маршруты 401/403/404,
    поэтому любой другой исход (200, 400 с валидацией тела, 429) считаем
    признаком живого токена. Схема авторизации перебирается: разные
    сборки API принимали токен то без префикса, то с `Bearer`.
    """
    raw = (token or "").strip()
    if not raw:
        return TokenProbe(ok=False, message="токен пустой")

    url = f"{settings.mrkt_base_url.rstrip('/')}/gifts/saling"
    # Все четыре массива обязательны: без любого из них API отвечает HTTP 400
    # с валидацией полей (проверено на живом MRKT 27.10.2026).
    payload = {
        "collectionNames": [],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "count": 1,
        "cursor": "",
    }
    timeout = aiohttp.ClientTimeout(total=settings.mrkt_timeout)
    own_session = session is None
    http: Any = session or aiohttp.ClientSession(timeout=timeout)
    last_status: int | None = None
    try:
        for scheme in schemes:
            try:
                async with http.request(
                    "POST", url, json=payload, headers=_probe_headers(raw, scheme, settings)
                ) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    body = await response.text()
            except (aiohttp.ClientError, TimeoutError) as exc:
                return TokenProbe(ok=False, message=f"сеть недоступна: {exc!r}")

            last_status = status
            if status in (401, 403, 404):
                continue
            if status == 400:
                # Дошли до валидации тела — значит маршрут доступен и токен принят.
                return TokenProbe(
                    ok=True,
                    status=status,
                    scheme=scheme,
                    message=(
                        "токен принят (API дошёл до валидации запроса). "
                        f"Замечание: {body[:120]}"
                    ),
                )
            if status == 429:
                return TokenProbe(
                    ok=True,
                    status=status,
                    scheme=scheme,
                    message=(
                        "токен принят (маршрут существует), но площадка ограничила "
                        "частоту запросов — подождите и повторите"
                    ),
                )
            if 200 <= status < 300:
                total: int | None = None
                try:
                    data = json.loads(body or "{}")
                    if isinstance(data, dict):
                        gifts = data.get("gifts")
                        total = len(gifts) if isinstance(gifts, list) else None
                except ValueError:
                    pass
                return TokenProbe(
                    ok=True,
                    status=status,
                    scheme=scheme,
                    message="токен рабочий, выдача получена",
                    listings_total=total,
                )
            return TokenProbe(
                ok=False,
                status=status,
                scheme=scheme,
                message=f"неожиданный ответ HTTP {status}: {body[:160]}",
            )
        return TokenProbe(
            ok=False,
            status=last_status,
            message=(
                f"токен отклонён (HTTP {last_status}). Скорее всего он истёк "
                "или скопирован не полностью."
            ),
        )
    finally:
        if own_session:
            await http.close()


# ----------------------------------------------------------------------------
#  Получение токена через служебную Telegram-сессию
# ----------------------------------------------------------------------------

def auto_login_readiness(settings: Settings) -> tuple[bool, str]:
    """Готов ли авто-вход. Возвращает (готово, пояснение для пользователя)."""
    if not telethon_available():
        return False, (
            "не установлен telethon. Выполните: pip install telethon "
            "(или раскомментируйте строку в requirements.txt)."
        )
    if not session_credentials_ready(settings):
        return False, (
            "не заданы TG_API_ID / TG_API_HASH. Получите их на "
            "https://my.telegram.org → API development tools и пропишите в .env."
        )
    return True, "авто-вход готов"


async def obtain_token(settings: Settings) -> str:
    """initData из Telegram → POST /auth → токен. Бросает TokenError с причиной."""
    ready, why = auto_login_readiness(settings)
    if not ready:
        raise TokenError(why)
    try:
        init_data = await request_init_data(settings)
    except Exception as exc:  # noqa: BLE001
        raise TokenError(
            f"не удалось открыть мини-приложение @mrkt: {exc!r}. "
            "Проверьте, что служебный аккаунт залогинен (первый запуск попросит "
            "телефон и код из Telegram) и что у него есть доступ к @mrkt."
        ) from exc
    try:
        token = await exchange_init_data(init_data, settings)
    except Exception as exc:  # noqa: BLE001
        raise TokenError(
            f"MRKT /auth не выдал токен: {exc!r}. Возможно, площадка изменила "
            "протокол авторизации или заблокировала автоматический вход."
        ) from exc
    if not token.strip():
        raise TokenError("MRKT /auth вернул пустой токен")
    return token.strip()


# ----------------------------------------------------------------------------
#  Запись в .env
# ----------------------------------------------------------------------------

def write_env_token(
    token: str,
    path: str | Path = ".env",
    *,
    source: str | None = "mrkt",
    create_from_example: bool = True,
) -> Path:
    """Записать `MRKT_TOKEN` (и, по желанию, `SOURCE`) в env-файл.

    Файл создаётся из `.env.example`, если его ещё нет. Существующие значения
    заменяются построчно, комментарии и порядок остальных строк сохраняются.
    """
    env_path = Path(path)
    if not env_path.exists() and create_from_example:
        example = env_path.parent / ".env.example"
        if not example.exists():
            example = Path(__file__).resolve().parent.parent.parent / ".env.example"
        if example.exists():
            env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            env_path.write_text("", encoding="utf-8")

    updates: dict[str, str] = {TOKEN_ENV_KEY: token.strip()}
    if source:
        updates[SOURCE_ENV_KEY] = source

    original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = original.splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines[index] = f"{key}={updates[key]}"
            seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    text = "\n".join(lines)
    if original.endswith("\n") or not original:
        text += "\n"
    env_path.write_text(text, encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:  # pragma: no cover - не все ФС поддерживают chmod
        pass
    return env_path


__all__ = [
    "TokenError",
    "TokenProbe",
    "auto_login_readiness",
    "mask_token",
    "obtain_token",
    "probe_token",
    "write_env_token",
]
