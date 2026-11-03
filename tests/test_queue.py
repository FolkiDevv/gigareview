import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from fakes import FakeBitbucket, Origin, make_config, make_pr

from gigareview.daemon import Daemon
from gigareview.errors import GigaReviewError
from gigareview.statuses import RunStatus, Trigger
from gigareview.store import RunRecord, Store


def _daemon(tmp_path: Path, store: Store, api: FakeBitbucket, origin_url: str = "unused") -> Daemon:
    config = make_config(
        tmp_path / "home",
        origin_url,
        scenarios={"slow": {"run": "sleep 30"}},
        daemon_extra={"max_parallel": 2},
    )
    daemon = Daemon(config, store, api)
    daemon.set_paused(paused=True)
    return daemon


def _run(store: Store, run_id: int) -> RunRecord:
    run = store.runs.get(run_id)
    assert run is not None
    return run


async def _until(check: Callable[[], bool], attempts: int = 400) -> None:
    """Ждёт условия до 20 секунд, сверяясь каждые 50 мс."""
    if check():
        return
    if not attempts:
        pytest.fail("Условие не выполнилось за 20 секунд")
    await asyncio.sleep(0.05)
    await _until(check, attempts - 1)


async def _taken(daemon: Daemon, api: FakeBitbucket, run_ids: tuple[int, ...]) -> None:
    """Прогоны взяты воркерами, первый уже написал в PR и выполняет сценарий."""
    await _until(lambda: daemon.status().active_runs == run_ids)
    await _until(lambda: bool(api.comments))
    await asyncio.sleep(0.3)


def test_prioritize_and_cancel_queued(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(prs=[make_pr(1)])
    daemon = _daemon(tmp_path, store, api)
    first, second, third = (daemon.enqueue(make_pr(pr_id), Trigger.MANUAL) for pr_id in (1, 2, 3))
    daemon.queue.prioritize(third)
    assert daemon.status().queued_runs == (third, first, second)
    assert daemon.queue.cancel(first)
    assert daemon.status().queued_runs == (third, second)
    assert daemon.status().queue_size == 2
    cancelled = _run(store, first)
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert "отменён" in cancelled.error
    with pytest.raises(GigaReviewError, match="не в очереди"):
        daemon.queue.cancel(first)
    with pytest.raises(GigaReviewError, match="не в очереди"):
        daemon.queue.prioritize(999)
    assert daemon.queue.cancel_queued() == 2
    assert not daemon.status().queued_runs
    assert _run(store, second).status is RunStatus.CANCELLED
    asyncio.run(asyncio.wait_for(daemon.wait_idle(), timeout=1))
    # Отменённый вручную коммит опрос заново не ставит.
    daemon.set_paused(paused=False)
    assert asyncio.run(daemon.poll()) == 0


def test_cancel_running_and_waiting_runs(tmp_path: Path, store: Store, origin: Origin) -> None:
    pr = make_pr(1, commit=origin.feature)
    api = FakeBitbucket(prs=[pr])
    daemon = _daemon(tmp_path, store, api, origin.url)

    async def scenario() -> tuple[int, int]:
        await daemon.start()
        running = daemon.enqueue(pr, Trigger.MANUAL)
        waiting = daemon.enqueue(pr, Trigger.MANUAL)
        # Второй прогон того же репозитория взят воркером, но ждёт первый.
        await _taken(daemon, api, (running, waiting))
        assert not daemon.queue.cancel(waiting)
        await _until(lambda: _run(store, waiting).status is RunStatus.CANCELLED)
        assert _run(store, running).status is RunStatus.RUNNING
        assert not daemon.queue.cancel(running)
        await asyncio.wait_for(daemon.wait_idle(), timeout=20)
        assert not daemon.status().active_runs
        await daemon.stop()
        return running, waiting

    running, waiting = asyncio.run(scenario())
    assert _run(store, waiting).started_at is None
    cancelled = _run(store, running)
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert "прервана" in api.comments[-1].text


def test_stopping_daemon_still_interrupts(tmp_path: Path, store: Store, origin: Origin) -> None:
    pr = make_pr(1, commit=origin.feature)
    daemon = _daemon(tmp_path, store, FakeBitbucket(prs=[pr]), origin.url)

    async def scenario() -> int:
        await daemon.start()
        run_id = daemon.enqueue(pr, Trigger.MANUAL)
        await _until(lambda: _run(store, run_id).status is RunStatus.RUNNING)
        await daemon.stop()
        return run_id

    run_id = asyncio.run(scenario())
    assert _run(store, run_id).status is RunStatus.INTERRUPTED
