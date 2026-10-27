"""Фоновый планировщик мониторинга.

Один asyncio-таск, тик каждые `POLL_TICK_SECONDS`:
* берёт активных (не на паузе) пользователей и их включённые фильтры;
* для каждого фильтра проверяет, наступил ли срок (`last_run_at + interval`);
* выполняет конвейер в отдельной сессии БД и отправляет найденное через sink;
* пишет журнал цикла в `monitor_runs` и статус в `filters.last_status`.

Ошибки одного пользователя не влияют на остальных. Остановка — кооперативная:
по флагу `asyncio.Event`, с ожиданием текущего цикла (без обрыва на полуслове).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable, Protocol

from app.config import Settings
from app.db.repo import Repository
from app.domain import ScoredListing, ensure_aware, utcnow
from app.filters.matching import FilterCriteria
from app.pipeline import PipelineContext, record_sent_batch, run_pipeline
from app.ranking import RankingWeights
from app.sources.base import ListingSource

logger = logging.getLogger(__name__)

NotifyFn = Callable[[int, ScoredListing, dict[str, Any]], Awaitable[bool]]


class SessionFactory(Protocol):
    def __call__(self) -> Any: ...


class Monitor:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        ctx: PipelineContext,
        notify: NotifyFn,
    ) -> None:
        self._settings = settings
        self._db = db
        self._ctx = ctx
        self._notify = notify
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._tick = asyncio.Event()
        self._cycles = 0
        self._errors = 0
        self._last_cycle_at = None
        self._last_error: str | None = None
        self._in_cycle = asyncio.Lock()

    # ------------------------------------------------------------------ запуск
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="mrkt-monitor")
        logger.info(
            "Мониторинг запущен: источник=%s, тик=%d c",
            self._ctx.source.name,
            self._settings.poll_tick_seconds,
        )

    async def stop(self) -> None:
        self._stop.set()
        self._tick.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=25)
        except asyncio.TimeoutError:
            logger.warning("Мониторинг не остановился за 25 c — отменяем принудительно")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        finally:
            self._task = None
            logger.info("Мониторинг остановлен (циклов: %d, ошибок: %d)", self._cycles, self._errors)

    def request_tick(self) -> None:
        """Разбудить планировщик досрочно (например, после /search или смены настроек)."""
        self._tick.set()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "cycles": self._cycles,
            "errors": self._errors,
            "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "last_error": self._last_error,
            "source": self._ctx.source.name,
            "is_test_data": self._ctx.source.is_test_data,
        }

    # -------------------------------------------------------------------- цикл
    async def _loop(self) -> None:
        tick = max(5, int(self._settings.poll_tick_seconds))
        while not self._stop.is_set():
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self._last_error = repr(exc)
                logger.exception("Сбой цикла мониторинга")
            try:
                await asyncio.wait_for(self._tick.wait(), timeout=tick)
                self._tick.clear()
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break

    async def _cycle(self) -> None:
        if self._in_cycle.locked():
            return
        async with self._in_cycle:
            self._cycles += 1
            self._last_cycle_at = utcnow()
            due = await self._collect_due_filters()
            if not due:
                return
            logger.info("Цикл мониторинга: %d фильтр(ов) по расписанию", len(due))
            for user_id, row in due:
                if self._stop.is_set():
                    break
                await self._run_one(user_id, row)

    async def _collect_due_filters(self) -> list[tuple[int, Any]]:
        due: list[tuple[int, Any]] = []
        now = utcnow()
        min_interval = max(1, int(self._settings.min_interval_minutes))
        async with self._db.session_factory() as session:
            repo = Repository(session)
            users = await repo.active_users()
            for user in users:
                if user.id == 0:  # служебная запись состояния источника
                    continue
                filters = await repo.list_filters(user.id, enabled_only=True)
                for row in filters:
                    if not row.notify_enabled:
                        continue
                    interval = max(min_interval, int(row.interval_minutes or min_interval))
                    last = ensure_aware(row.last_run_at)
                    if last is None or now - last >= timedelta(minutes=interval):
                        due.append((user.id, row))
        return due

    async def _run_one(self, user_id: int, row: Any) -> None:
        started = utcnow()
        filter_id = int(row.id)
        async with self._db.session_factory() as session:
            repo = Repository(session)
            criteria = FilterCriteria.from_row(row, defaults=self._defaults())
            weights = await self._weights_for(repo, user_id)
            try:
                outcome = await run_pipeline(
                    self._ctx,
                    repo,
                    user_id=user_id,
                    filter_row=row,
                    criteria=criteria,
                    weights=weights,
                )
            except Exception as exc:  # noqa: BLE001
                self._errors += 1
                self._last_error = repr(exc)
                logger.exception("Цикл для фильтра %s пользователя %s упал", filter_id, user_id)
                await repo.mark_filter_run(user_id, filter_id, status="error", message=repr(exc)[:400])
                await repo.log_run(
                    user_id=user_id,
                    filter_id=filter_id,
                    started_at=started,
                    finished_at=utcnow(),
                    fetched=0,
                    passed_filters=0,
                    sent=0,
                    skipped_duplicates=0,
                    status="error",
                    error=repr(exc)[:400],
                    source=self._ctx.source.name,
                )
                return

            sent_items: list[ScoredListing] = []
            for item in outcome.results:
                meta = {
                    "filter_id": filter_id,
                    "filter_name": row.name,
                    "is_test_data": outcome.is_test_data or item.listing.is_test_data,
                    "reason": item.listing.extra.get("send_reason", "new"),
                    "reason_detail": item.listing.extra.get("send_detail", ""),
                    "outcome": outcome.summary(),
                }
                try:
                    delivered = await self._notify(user_id, item, meta)
                except Exception as exc:  # noqa: BLE001
                    delivered = False
                    logger.warning("Не удалось отправить уведомление пользователю %s: %r", user_id, exc)
                if delivered:
                    sent_items.append(item)
                    await asyncio.sleep(max(0.0, float(self._settings.send_delay_seconds)))

            if sent_items:
                await record_sent_batch(
                    repo, user_id=user_id, filter_id=filter_id, items=sent_items
                )

            status = "ok" if not outcome.errors else "partial"
            message = (
                f"{outcome.summary()}"
                + (f"; ошибки: {'; '.join(outcome.errors[:2])}" if outcome.errors else "")
            )
            await repo.mark_filter_run(user_id, filter_id, status=status, message=message)
            await repo.log_run(
                user_id=user_id,
                filter_id=filter_id,
                started_at=started,
                finished_at=utcnow(),
                fetched=outcome.fetched,
                passed_filters=outcome.passed_all_filters,
                sent=len(sent_items),
                skipped_duplicates=outcome.duplicates_skipped,
                status=status,
                error="; ".join(outcome.errors)[:400],
                source=self._ctx.source.name,
            )
            logger.info(
                "Фильтр %s (user %s): %s", filter_id, user_id, message[:300]
            )

    def _defaults(self) -> dict[str, Any]:
        return {
            "mono_mode": self._settings.mono_mode_default,
            "mono_max_saturation": self._settings.mono_max_saturation,
            "mono_min_score": self._settings.mono_min_score,
            "mono_allow_name_heuristic": self._settings.mono_allow_name_heuristic,
            "min_relevance": self._settings.default_min_relevance,
        }

    async def _weights_for(self, repo: Repository, user_id: int) -> RankingWeights:
        user = await repo.get_user(user_id)
        if user is None:
            return RankingWeights.from_settings(self._settings)
        stored = user.weights()
        if not stored:
            return RankingWeights.from_settings(self._settings)
        base = RankingWeights.from_settings(self._settings).as_dict()
        return RankingWeights.from_mapping({**base, **stored})


async def run_once_now(monitor: Monitor) -> None:
    """Служебная функция: принудительно выполнить один цикл (для /search и тестов)."""
    await monitor._cycle()
