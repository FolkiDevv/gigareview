"""Очередь прогонов демона: порядок, приоритет, отмена ожидающих и выполняющихся."""

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from gigareview.bitbucket import PullRequest
from gigareview.errors import GigaReviewError
from gigareview.statuses import EventLevel, RunStatus
from gigareview.store import Store

__all__ = ["Job", "RunQueue"]

#: Причина в прогоне, отменённом пользователем.
CANCELLED_NOTE: Final = "Прогон отменён из Web Dashboard"

#: Выполнение прогона: ``review(run_id, pr)``.
type Review = Callable[[int, PullRequest], Coroutine[Any, Any, object]]


@dataclass(frozen=True, slots=True)
class Job:
    """Прогон в очереди."""

    run_id: int
    pr: PullRequest


class RunQueue:
    """Очередь прогонов: воркеры берут из начала, Web Dashboard переставляет и отменяет.

    Прогоны одного репозитория выполняются по очереди. Отменённый прогон получает статус
    ``cancelled``: опрос его коммит больше не ставит, повторить можно вручную.
    """

    def __init__(self, store: Store) -> None:
        """Пустая очередь; статусы отменённых прогонов пишутся в ``store``."""
        self._store = store
        self._jobs: deque[Job] = deque()
        self._ready = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._unfinished = 0
        self._taken: dict[int, asyncio.Task[object]] = {}
        self._stopping: set[int] = set()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def queued_ids(self) -> tuple[int, ...]:
        """Номера ожидающих прогонов в порядке очереди."""
        return tuple(job.run_id for job in self._jobs)

    def taken_ids(self) -> tuple[int, ...]:
        """Номера прогонов, взятых воркерами (выполняются или ждут свой репозиторий)."""
        return tuple(sorted(self._taken))

    def put(self, job: Job) -> None:
        """Ставит прогон в конец очереди."""
        self._jobs.append(job)
        self._unfinished += 1
        self._idle.clear()
        self._ready.set()

    async def get(self) -> Job:
        """Следующий прогон (ждёт, пока он появится)."""
        while not self._jobs:
            self._ready.clear()
            await self._ready.wait()
        return self._jobs.popleft()

    def done(self) -> None:
        """Взятый прогон завершён (для :meth:`join`)."""
        self._unfinished -= 1
        if not self._unfinished:
            self._idle.set()

    async def join(self) -> None:
        """Ждёт, пока очередь опустеет и все взятые прогоны завершатся."""
        await self._idle.wait()

    async def execute(self, job: Job, review: Review) -> None:
        """Выполняет взятый прогон ``review`` отдельной задачей, которую можно отменить.

        Ожидание своего репозитория — часть задачи: отменяется и прогон, который ещё ждёт.
        Остановка самого воркера отменяет задачу и дожидается её (статус ставит пайплайн).
        """
        task = asyncio.create_task(self._locked(job, review))
        self._taken[job.run_id] = task
        try:
            await asyncio.wait([task])
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._taken.pop(job.run_id, None)
            self._stopping.discard(job.run_id)
        if task.cancelled():
            self._mark_cancelled(job.run_id)
            return
        task.result()

    def prioritize(self, run_id: int) -> None:
        """Переносит ожидающий прогон в начало очереди."""
        job = self._find(run_id)
        self._jobs.remove(job)
        self._jobs.appendleft(job)
        self._store.events.add(EventLevel.NOTE, f"Прогон #{run_id} — в начало очереди", run_id)

    def cancel(self, run_id: int) -> bool:
        """Отменяет прогон; ``True`` — снят с очереди, ``False`` — выполняющийся прерывается.

        Прерывание завершается позже: статус ``cancelled`` ставит :meth:`execute`.
        """
        task = self._taken.get(run_id)
        if task is not None and not task.done():
            # Повторная отмена прервала бы уборку первой (запись в PR «проверка прервана»).
            if run_id in self._stopping:
                return False
            self._stopping.add(run_id)
            task.cancel()
            self._store.events.add(EventLevel.NOTE, f"Прогон #{run_id}: запрошена отмена", run_id)
            return False
        self._remove(self._find(run_id))
        return True

    def cancel_queued(self) -> int:
        """Отменяет все ожидающие прогоны; возвращает, сколько отменено."""
        jobs = list(self._jobs)
        for job in jobs:
            self._remove(job)
        return len(jobs)

    async def _locked(self, job: Job, review: Review) -> None:
        """Прогон под блокировкой своего репозитория."""
        pr = job.pr
        async with self._locks[f"{pr.project}/{pr.repo}".casefold()]:
            await review(job.run_id, pr)

    def _find(self, run_id: int) -> Job:
        """Ожидающий прогон по номеру."""
        for job in self._jobs:
            if job.run_id == run_id:
                return job
        raise GigaReviewError(f"Прогон #{run_id} не в очереди и не выполняется")

    def _remove(self, job: Job) -> None:
        """Снимает ожидающий прогон с очереди со статусом ``cancelled``."""
        self._jobs.remove(job)
        self.done()
        self._mark_cancelled(job.run_id)

    def _mark_cancelled(self, run_id: int) -> None:
        self._store.runs.finish(run_id, RunStatus.CANCELLED, CANCELLED_NOTE)
        self._store.events.add(EventLevel.NOTE, CANCELLED_NOTE, run_id)
