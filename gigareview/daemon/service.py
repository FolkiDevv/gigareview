"""Демон ревью: цикл опроса, очередь прогонов и воркеры."""

import asyncio
import contextlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from gigareview.bitbucket import BitbucketApi, PullRequest
from gigareview.config import Config, load_config
from gigareview.errors import GigaReviewError
from gigareview.pipeline import Reviewer, TaskWatch, new_run
from gigareview.statuses import EventLevel, Trigger
from gigareview.store import Store
from gigareview.store.clock import stamp, utc_now

__all__ = ["Daemon", "DaemonStatus"]

_PAUSED = "paused"
_LAST_POLL = "last_poll"
_POLL_KEY = "poll"


@dataclass(frozen=True, slots=True)
class DaemonStatus:
    """Снимок состояния демона для Web Dashboard."""

    running: bool
    paused: bool
    last_poll: datetime | None
    next_poll: datetime | None
    last_error: str
    queue_size: int
    active_runs: tuple[int, ...]
    interval_minutes: float
    dry_run: bool


@dataclass(frozen=True, slots=True)
class _Job:
    """Прогон в очереди."""

    run_id: int
    pr: PullRequest


class Daemon:
    """Опрашивает Bitbucket и выполняет прогоны; методы управления вызывает Web Dashboard."""

    def __init__(
        self,
        config: Config,
        store: Store,
        api: BitbucketApi,
        *,
        config_path: Path | None = None,
    ) -> None:
        """Демон над базой ``store`` и Bitbucket ``api``; ``config_path`` — для перечитывания."""
        self._store = store
        self._api = api
        self._config_path = config_path
        self._reviewer = Reviewer(config, store, api)
        self._watch = TaskWatch(store, api)
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._active: set[int] = set()
        self._last_error = ""
        self._next_poll: datetime | None = None

    @property
    def config(self) -> Config:
        """Текущая конфигурация."""
        return self._reviewer.config

    async def start(self) -> None:
        """Помечает прерванные прошлым запуском прогоны и запускает опрос и воркеров."""
        interrupted = self._store.runs.interrupt_active()
        if interrupted:
            self._event(EventLevel.WARNING, f"Прерванных прошлым запуском прогонов: {interrupted}")
        workers = [self._worker() for _ in range(self.config.daemon.max_parallel)]
        self._tasks = [asyncio.create_task(job) for job in (self._poll_loop(), *workers)]
        self._event(EventLevel.NOTE, "Демон запущен")

    async def stop(self) -> None:
        """Останавливает опрос и воркеров; выполняемые прогоны помечаются прерванными."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._store.runs.interrupt_active()
        self._event(EventLevel.NOTE, "Демон остановлен")

    async def poll(self) -> int:
        """Забирает PR из Bitbucket и ставит в очередь непроверенные коммиты; сколько поставлено.

        PR без нового коммита — наблюдению: не закрыли ли вручную все таски демона.
        """
        try:
            prs = await self._api.list_review_prs()
        except GigaReviewError as error:
            self._last_error = f"Опрос Bitbucket: {error}"
            self._event(EventLevel.ERROR, self._last_error)
            return 0
        self._last_error = ""
        self._store.state.set(_LAST_POLL, stamp())
        queued = 0
        skipped = []
        unknown: Counter[str] = Counter()
        for pr in prs:
            if self.config.find_repo(pr.project, pr.repo) is None:
                unknown[f"{pr.project}/{pr.repo}"] += 1
                continue
            if self._store.runs.should_skip(
                pr.project,
                pr.repo,
                pr.id,
                pr.commit,
                dry_run=self.config.daemon.dry_run,
            ):
                skipped.append(pr)
                continue
            self.enqueue(pr, Trigger.AUTO)
            queued += 1
        # Сводка — только когда она изменилась: время опроса и так видно на Overview.
        self._event(
            EventLevel.DEBUG,
            _poll_summary(len(prs), unknown, queued, len(skipped)),
            key=_POLL_KEY,
        )
        await self._watch.check(skipped, self.config)
        return queued

    def poll_now(self) -> None:
        """Опросить Bitbucket сейчас, не дожидаясь периода."""
        self._wake.set()

    def set_paused(self, *, paused: bool) -> None:
        """Приостанавливает или возобновляет опрос (ручные прогоны работают всегда)."""
        self._store.state.set(_PAUSED, "1" if paused else "")
        self._event(EventLevel.NOTE, "Опрос приостановлен" if paused else "Опрос возобновлён")
        if not paused:
            self._wake.set()

    def enqueue(self, pr: PullRequest, trigger: Trigger) -> int:
        """Ставит прогон ``pr`` в очередь; репозиторий должен быть описан в конфигурации."""
        repo = self.config.find_repo(pr.project, pr.repo)
        if repo is None:
            raise GigaReviewError(f"Репозиторий {pr.project}/{pr.repo} не описан в конфигурации")
        run = new_run(
            pr,
            trigger,
            fingerprint=self.config.fingerprint(repo),
            dry_run=self.config.daemon.dry_run,
        )
        run_id = self._store.runs.create(run)
        self._queue.put_nowait(_Job(run_id, pr))
        self._event(EventLevel.NOTE, f"{repo.key}#{pr.id} поставлен в очередь ({trigger})", run_id)
        return run_id

    async def rerun(self, project: str, repo: str, pr_id: int) -> int:
        """Повторяет проверку PR на его текущем коммите (кнопка в Web Dashboard)."""
        pr = await self._api.get_pull_request(project, repo, pr_id)
        if pr.state != "OPEN":
            raise GigaReviewError(f"{project}/{repo}#{pr_id} уже не открыт ({pr.state})")
        return self.enqueue(pr, Trigger.MANUAL)

    async def wait_idle(self) -> None:
        """Ждёт, пока очередь опустеет и все взятые прогоны завершатся."""
        await self._queue.join()

    def reload_config(self) -> None:
        """Перечитывает конфигурацию: сценарии, репозитории, период опроса.

        Подключение к Bitbucket (адрес, пользователь, токен), адрес дашборда и параллелизм
        меняются только перезапуском: клиент и воркеры уже созданы.
        """
        if self._config_path is None:
            raise GigaReviewError("Демон запущен без файла конфигурации")
        fresh = load_config(self._config_path)
        old = self.config
        # Подключение и параллелизм остаются прежними до перезапуска: в том числе
        # token_env, по которому токен скрывается из окружения сценариев.
        kept_daemon = fresh.daemon.model_copy(update={"max_parallel": old.daemon.max_parallel})
        self._reviewer.config = fresh.model_copy(
            update={"bitbucket": old.bitbucket, "dashboard": old.dashboard, "daemon": kept_daemon},
        )
        self._event(EventLevel.NOTE, f"Конфигурация перечитана из {self._config_path}")
        restart_needed = (
            fresh.bitbucket != old.bitbucket
            or fresh.dashboard != old.dashboard
            or fresh.daemon.max_parallel != old.daemon.max_parallel
        )
        if restart_needed:
            self._event(
                EventLevel.WARNING,
                "Bitbucket, дашборд и max_parallel изменятся только после перезапуска демона",
            )
        self._wake.set()

    def status(self) -> DaemonStatus:
        """Снимок состояния для Web Dashboard."""
        last_poll = self._store.state.get(_LAST_POLL)
        return DaemonStatus(
            running=any(not task.done() for task in self._tasks),
            paused=bool(self._store.state.get(_PAUSED)),
            last_poll=datetime.fromisoformat(last_poll) if last_poll else None,
            next_poll=self._next_poll,
            last_error=self._last_error,
            queue_size=self._queue.qsize(),
            active_runs=tuple(sorted(self._active)),
            interval_minutes=self.config.daemon.interval_minutes,
            dry_run=self.config.daemon.dry_run,
        )

    async def _poll_loop(self) -> None:
        """Опрос каждые ``interval_minutes`` (или раньше — по :meth:`poll_now`)."""
        while True:
            self._wake.clear()
            try:
                if not self._store.state.get(_PAUSED):
                    await self.poll()
            # Битый ответ Bitbucket или сбой базы не должны навсегда остановить опрос.
            except Exception as error:
                self._last_error = f"Опрос Bitbucket: {error!r}"
                self._event(EventLevel.ERROR, self._last_error)
            interval = self.config.daemon.interval_seconds
            self._next_poll = utc_now() + timedelta(seconds=interval)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=interval)

    async def _worker(self) -> None:
        """Берёт прогоны из очереди; прогоны одного репозитория идут по очереди."""
        while True:
            job = await self._queue.get()
            pr = job.pr
            async with self._locks[f"{pr.project}/{pr.repo}".casefold()]:
                self._active.add(job.run_id)
                try:
                    await self._reviewer.review(job.run_id, pr)
                # Сбой вне прогона (например, базы) не должен убивать воркер.
                except Exception as error:
                    self._event(EventLevel.ERROR, f"Воркер: {error!r}", job.run_id)
                self._active.discard(job.run_id)
            self._queue.task_done()

    def _event(
        self,
        level: EventLevel,
        message: str,
        run_id: int | None = None,
        *,
        key: str = "",
    ) -> None:
        self._store.events.add(level, message, run_id, key=key)


def _poll_summary(total: int, unknown: Counter[str], queued: int, skipped: int) -> str:
    """Сводка опроса для журнала: сколько PR и что с ними сделано."""
    parts = [f"Опрос Bitbucket: PR {total}"]
    if unknown:
        repos = ", ".join(f"{repo} ({count})" for repo, count in sorted(unknown.items()))
        parts.append(f"вне конфигурации {unknown.total()}: {repos}")
    parts.extend((f"в очередь {queued}", f"уже проверены или проверяются {skipped}"))
    return "; ".join(parts)
