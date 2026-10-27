# syntax=docker/dockerfile:1
# ============================================================================
#  MRKT Gift Monitor Bot
#  Сборка:   docker build -t mrkt-gift-bot .
#  Запуск:   docker compose up -d       (см. docker-compose.yml)
# ============================================================================

FROM python:3.12-slim AS base

# Python не буферизует вывод (логи сразу в docker logs), не пишет .pyc
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential — на случай сборки wheel'ов из исходников
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала только зависимости — слой переиспользуется, если код менялся,
# а requirements.txt нет.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем код
COPY app/ ./app/
COPY main.py pytest.ini ./
COPY tests/ ./tests/

# Данные (SQLite, кэш картинок) и сессии Telegram — в отдельные volume
RUN mkdir -p /app/data /app/data/cache /app/sessions \
    && useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

VOLUME ["/app/data", "/app/sessions"]

# Токен и настройки приходят только из окружения / .env — в образ ничего не вшито.
ENV DATABASE_URL=sqlite+aiosqlite:////app/data/bot.db \
    DATA_DIR=/app/data \
    CACHE_DIR=/app/data/cache \
    TG_SESSION_WORKDIR=/app/sessions

# Бот работает через long polling — входящие порты не нужны.
# HEALTHCHECK не используется: процесс однозадачный, состояние видно
# в docker logs и в команде /status (там же — «🩺 Проверить источник»).

# stop_grace_period в compose даёт мониторингу доделать текущий цикл
STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
