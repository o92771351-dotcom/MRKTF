"""Тесты получения/проверки токена MRKT и записи его в .env.

Сеть подменяется фейковой сессией: проверяются логика перебора схем
авторизации, интерпретация кодов ответа и безопасная работа с .env.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import aiohttp
import pytest

import app.sources.token as token_module
from app.config import Settings
from app.sources.token import (
    TokenError,
    auto_login_readiness,
    mask_token,
    obtain_token,
    probe_token,
    write_env_token,
)

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


class FakeResponse:
    def __init__(self, status: int, payload: Any = "") -> None:
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeHttp:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            return FakeResponse(500, "no canned response")
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    async def close(self) -> None:
        self.closed = True


def make_settings(**kwargs: Any) -> Settings:
    defaults: dict[str, Any] = {"bot_token": "x", "source": "mrkt", "mrkt_timeout": 5}
    defaults.update(kwargs)
    return Settings(_env_file=None, **defaults)


# ----------------------------------------------------------------------------
#  Маскирование
# ----------------------------------------------------------------------------

def test_mask_token_hides_the_secret() -> None:
    assert mask_token("") == "<пусто>"
    assert mask_token("short") == "*****"
    masked = mask_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    assert "eyJhbG" in masked and "U9" not in masked.split("…")[0]
    assert "длина" in masked
    # Полный токен нигде не показывается
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in masked


# ----------------------------------------------------------------------------
#  Проверка токена
# ----------------------------------------------------------------------------

async def test_probe_accepts_raw_scheme() -> None:
    http = FakeHttp([FakeResponse(200, {"gifts": [{"id": "1"}]})])
    probe = await probe_token("tok", make_settings(), session=http)
    assert probe.ok is True
    assert probe.scheme == "raw"
    assert probe.status == 200
    assert probe.listings_total == 1
    assert http.calls[0]["headers"]["Authorization"] == "tok"
    assert http.calls[0]["headers"]["Cookie"] == "access_token=tok"
    assert http.calls[0]["url"].endswith("/gifts/saling")


async def test_probe_falls_back_to_bearer_scheme() -> None:
    http = FakeHttp([FakeResponse(401, ""), FakeResponse(200, {"gifts": []})])
    probe = await probe_token("tok", make_settings(), session=http)
    assert probe.ok is True and probe.scheme == "bearer"
    assert http.calls[1]["headers"]["Authorization"] == "Bearer tok"


async def test_probe_treats_400_validation_as_accepted_token() -> None:
    """HTTP 400 с валидацией тела = маршрут доступен, токен принят."""
    body = {"errors": {"ModelNames": ["The ModelNames field is required."]}}
    http = FakeHttp([FakeResponse(400, body)])
    probe = await probe_token("tok", make_settings(), session=http)
    assert probe.ok is True
    assert probe.status == 400
    assert "валидаци" in probe.message
    assert len(http.calls) == 1


async def test_probe_sends_all_four_required_arrays() -> None:
    http = FakeHttp([FakeResponse(200, {"gifts": []})])
    await probe_token("tok", make_settings(), session=http)
    payload = http.calls[0]["json"]
    for key in ("collectionNames", "modelNames", "backdropNames", "symbolNames"):
        assert key in payload, key


async def test_probe_reports_rejected_token() -> None:
    http = FakeHttp([FakeResponse(404, "")])
    probe = await probe_token("dead", make_settings(), session=http)
    assert probe.ok is False
    assert probe.status == 404
    assert "истёк" in probe.message or "отклонён" in probe.message
    assert len(http.calls) == 2          # обе схемы попробовали


async def test_probe_treats_429_as_accepted_but_limited() -> None:
    http = FakeHttp([FakeResponse(429, "slow down")])
    probe = await probe_token("tok", make_settings(), session=http)
    assert probe.ok is True
    assert probe.status == 429
    assert "частот" in probe.message


async def test_probe_reports_network_problem() -> None:
    class BrokenHttp(FakeHttp):
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            raise aiohttp.ClientConnectionError("no route to host")

    probe = await probe_token("tok", make_settings(), session=BrokenHttp([]))
    assert probe.ok is False
    assert "сеть недоступна" in probe.message


async def test_probe_rejects_empty_token_without_request() -> None:
    http = FakeHttp([])
    probe = await probe_token("   ", make_settings(), session=http)
    assert probe.ok is False and probe.message == "токен пустой"
    assert http.calls == []


async def test_probe_survives_non_json_body() -> None:
    http = FakeHttp([FakeResponse(200, "<html>ok</html>")])
    probe = await probe_token("tok", make_settings(), session=http)
    assert probe.ok is True
    assert probe.listings_total is None


# ----------------------------------------------------------------------------
#  Готовность авто-входа
# ----------------------------------------------------------------------------

def test_readiness_requires_telethon_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_module, "telethon_available", lambda: False)
    ready, why = auto_login_readiness(make_settings(tg_api_id=1, tg_api_hash="h"))
    assert ready is False and "telethon" in why

    monkeypatch.setattr(token_module, "telethon_available", lambda: True)
    ready, why = auto_login_readiness(make_settings())
    assert ready is False and "TG_API_ID" in why

    ready, why = auto_login_readiness(make_settings(tg_api_id=12345, tg_api_hash="hash"))
    assert ready is True and "готов" in why


async def test_obtain_token_explains_missing_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_module, "telethon_available", lambda: False)
    with pytest.raises(TokenError) as info:
        await obtain_token(make_settings(tg_api_id=1, tg_api_hash="h"))
    assert "telethon" in str(info.value)


async def test_obtain_token_wraps_init_data_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(settings: Settings) -> str:
        raise RuntimeError("no session")

    monkeypatch.setattr(token_module, "auto_login_readiness", lambda s: (True, "готов"))
    monkeypatch.setattr(token_module, "request_init_data", boom)
    with pytest.raises(TokenError) as info:
        await obtain_token(make_settings(tg_api_id=1, tg_api_hash="h"))
    assert "@mrkt" in str(info.value)


async def test_obtain_token_wraps_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def init_ok(settings: Settings) -> str:
        return "query_id=...&user=..."

    async def auth_fail(init_data: str, settings: Settings) -> str:
        raise RuntimeError("MRKT /auth вернул HTTP 403: Forbid 1")

    monkeypatch.setattr(token_module, "auto_login_readiness", lambda s: (True, "готов"))
    monkeypatch.setattr(token_module, "request_init_data", init_ok)
    monkeypatch.setattr(token_module, "exchange_init_data", auth_fail)
    with pytest.raises(TokenError) as info:
        await obtain_token(make_settings(tg_api_id=1, tg_api_hash="h"))
    assert "/auth" in str(info.value)


# ----------------------------------------------------------------------------
#  Запись .env
# ----------------------------------------------------------------------------

def test_write_env_creates_file_from_example(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    env_path = write_env_token("SECRET-TOKEN", tmp_path / ".env")
    text = env_path.read_text(encoding="utf-8")
    assert "MRKT_TOKEN=SECRET-TOKEN" in text
    assert "SOURCE=mrkt" in text
    assert "BOT_TOKEN=1234567890:AAA-replace-me" in text      # остальные строки не тронуты
    assert "# Токен от @BotFather" in text                    # комментарии сохранены
    assert text.count("MRKT_TOKEN=") == 1


def test_write_env_replaces_existing_value_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# мой комментарий\nBOT_TOKEN=abc\nSOURCE=demo\nMRKT_TOKEN=old\nLOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    write_env_token("new-token", env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# мой комментарий"
    assert "MRKT_TOKEN=new-token" in lines
    assert lines.index("MRKT_TOKEN=new-token") == 3           # позиция сохранена
    assert "SOURCE=mrkt" in lines
    assert not any("old" in line for line in lines)


def test_write_env_appends_missing_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("BOT_TOKEN=abc\n", encoding="utf-8")
    write_env_token("tok", env_path)
    text = env_path.read_text(encoding="utf-8")
    assert text.startswith("BOT_TOKEN=abc\n")
    assert "MRKT_TOKEN=tok" in text and "SOURCE=mrkt" in text


def test_write_env_can_leave_source_untouched(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("SOURCE=demo\n", encoding="utf-8")
    write_env_token("tok", env_path, source=None)
    text = env_path.read_text(encoding="utf-8")
    assert "SOURCE=demo" in text
    assert "MRKT_TOKEN=tok" in text


def test_written_env_is_loadable_and_protected(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    env_path = write_env_token("SECRET-TOKEN", tmp_path / ".env")

    settings = Settings(_env_file=env_path)
    assert settings.mrkt_token == "SECRET-TOKEN"
    assert settings.source == "mrkt"

    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f"ожидали 0600, получили {oct(mode)}"


def test_written_token_never_leaks_into_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_path = write_env_token("SUPER-SECRET-VALUE", tmp_path / ".env")
    captured = capsys.readouterr()
    assert "SUPER-SECRET-VALUE" not in captured.out + captured.err
    assert env_path.exists()
