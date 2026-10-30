import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeBitbucket, Origin, make_config, make_pr, with_status

from gigareview.config import SAMPLE_CONFIG
from gigareview.daemon import Daemon
from gigareview.errors import BitbucketError, ConfigError, GigaReviewError
from gigareview.pipeline import new_run
from gigareview.statuses import EventLevel, ReviewStatus, RunStatus, Trigger
from gigareview.store import CommentKind, RunFilter, Store, TaskDetails


def _daemon(
    tmp_path: Path,
    store: Store,
    origin_url: str,
    api: FakeBitbucket,
    *,
    config_path: Path | None = None,
    interval: float = 1,
) -> Daemon:
    config = make_config(tmp_path / "home", origin_url, daemon_extra={"interval_minutes": interval})
    return Daemon(config, store, api, config_path=config_path)


def test_poll_enqueues_configured_unreviewed_prs(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(prs=[make_pr(1), make_pr(2, repo="unknown")])
    daemon = _daemon(tmp_path, store, "unused", api)
    assert asyncio.run(daemon.poll()) == 1
    assert asyncio.run(daemon.poll()) == 0
    runs = store.queries.runs(RunFilter())
    assert [(run.pr_id, run.status, run.trigger) for run in runs] == [
        (1, RunStatus.QUEUED, Trigger.AUTO),
    ]
    assert runs[0].fingerprint
    assert daemon.status().queue_size == 1
    assert daemon.status().last_poll is not None


def test_poll_summary_is_logged_when_it_changes(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(prs=[make_pr(1), make_pr(2, repo="unknown")])
    daemon = _daemon(tmp_path, store, "unused", api)
    asyncio.run(daemon.poll())
    asyncio.run(daemon.poll())
    debug = [event.message for event in store.events.list(EventLevel.DEBUG)]
    assert "PROJ/repo#1: прогон #1 (queued, запуск: auto) — статус поставит он" in debug
    summaries = [message for message in debug if message.startswith("Опрос")]
    assert summaries == [
        (
            "Опрос Bitbucket: PR 2; вне конфигурации 1: PROJ/unknown (1); в очередь 0; "
            "уже проверены или проверяются 1"
        ),
        (
            "Опрос Bitbucket: PR 2; вне конфигурации 1: PROJ/unknown (1); в очередь 1; "
            "уже проверены или проверяются 0"
        ),
    ]


def test_poll_error_is_reported(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(list_error=BitbucketError("down"))
    daemon = _daemon(tmp_path, store, "unused", api)
    assert asyncio.run(daemon.poll()) == 0
    assert "down" in daemon.status().last_error
    assert store.events.count(EventLevel.ERROR) == 1
    api.list_error = None
    asyncio.run(daemon.poll())
    assert not daemon.status().last_error


def test_enqueue_unknown_repo_is_error(tmp_path: Path, store: Store) -> None:
    daemon = _daemon(tmp_path, store, "unused", FakeBitbucket())
    with pytest.raises(GigaReviewError, match="не описан"):
        daemon.enqueue(make_pr(repo="nope"), Trigger.MANUAL)


def test_rerun(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(prs=[make_pr(1), make_pr(2, state="MERGED")])
    daemon = _daemon(tmp_path, store, "unused", api)
    run_id = asyncio.run(daemon.rerun("PROJ", "repo", 1))
    run = store.runs.get(run_id)
    assert run is not None
    assert run.trigger is Trigger.MANUAL
    with pytest.raises(GigaReviewError, match="MERGED"):
        asyncio.run(daemon.rerun("PROJ", "repo", 2))


def test_pause_and_resume(tmp_path: Path, store: Store) -> None:
    daemon = _daemon(tmp_path, store, "unused", FakeBitbucket())
    assert not daemon.status().paused
    daemon.set_paused(paused=True)
    assert daemon.status().paused
    daemon.set_paused(paused=False)
    assert not daemon.status().paused
    assert store.events.count() == 2


def test_reload_config(tmp_path: Path, store: Store) -> None:
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    daemon = _daemon(tmp_path, store, "unused", FakeBitbucket(), config_path=path)
    daemon.reload_config()
    assert daemon.config.repos[0].slug == "backend"
    path.write_text("broken = [", encoding="utf-8")
    with pytest.raises(ConfigError):
        daemon.reload_config()
    assert daemon.config.repos[0].slug == "backend"


def test_reload_warns_about_restart_only_settings(tmp_path: Path, store: Store) -> None:
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    daemon = _daemon(tmp_path, store, "unused", FakeBitbucket(), config_path=path)
    daemon.reload_config()
    warnings = [event.message for event in store.events.list(EventLevel.WARNING)]
    assert any("перезапуска" in message for message in warnings)
    assert daemon.config.bitbucket.token_env == "GR_TEST_TOKEN"
    assert daemon.config.bitbucket.url == "https://bb.example.com"
    assert daemon.config.repos[0].slug == "backend"


def test_reload_without_path_is_error(tmp_path: Path, store: Store) -> None:
    daemon = _daemon(tmp_path, store, "unused", FakeBitbucket())
    with pytest.raises(GigaReviewError, match="без файла"):
        daemon.reload_config()


def test_daemon_runs_reviews_end_to_end(tmp_path: Path, store: Store, origin: Origin) -> None:
    stale = store.runs.create(
        new_run(make_pr(9), Trigger.AUTO, fingerprint="", dry_run=False),
    )
    api = FakeBitbucket(prs=[make_pr(1, commit=origin.feature)])
    daemon = _daemon(tmp_path, store, origin.url, api, interval=0.002)

    async def scenario() -> None:
        await daemon.start()
        assert daemon.status().running
        await asyncio.sleep(0.2)
        await asyncio.wait_for(daemon.wait_idle(), timeout=30)
        daemon.poll_now()
        await asyncio.sleep(0.1)
        await daemon.stop()

    asyncio.run(scenario())
    runs = store.queries.runs(RunFilter(pr_id=1))
    assert [run.status for run in runs] == [RunStatus.PASSED]
    assert api.statuses
    stale_run = store.runs.get(stale)
    assert stale_run is not None
    assert stale_run.status is RunStatus.INTERRUPTED
    assert not daemon.status().running
    assert daemon.status().next_poll is not None
    messages = [event.message for event in store.events.list()]
    assert "Демон остановлен" in messages
    assert any("Прерванных" in message for message in messages)


def test_poll_loop_survives_unexpected_errors(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(list_error=RuntimeError("bad payload"))
    daemon = _daemon(tmp_path, store, "unused", api, interval=0.002)

    async def scenario() -> None:
        await daemon.start()
        await asyncio.sleep(0.3)
        assert daemon.status().running
        await daemon.stop()

    asyncio.run(scenario())
    assert "bad payload" in daemon.status().last_error
    assert store.events.count(EventLevel.ERROR) >= 2


def test_worker_survives_store_failure(
    tmp_path: Path,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(run_id: int) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store.runs, "mark_running", broken)
    daemon = _daemon(tmp_path, store, "unused", FakeBitbucket())

    async def scenario() -> None:
        await daemon.start()
        daemon.set_paused(paused=True)
        daemon.enqueue(make_pr(1), Trigger.MANUAL)
        daemon.enqueue(make_pr(2), Trigger.MANUAL)
        await asyncio.wait_for(daemon.wait_idle(), timeout=5)
        assert daemon.status().active_runs == ()
        await daemon.stop()

    asyncio.run(scenario())
    errors = [event.message for event in store.events.list(EventLevel.ERROR)]
    assert sum("disk I/O error" in message for message in errors) == 2


def test_paused_daemon_does_not_poll(tmp_path: Path, store: Store) -> None:
    api = FakeBitbucket(prs=[make_pr(1)])
    daemon = _daemon(tmp_path, store, "unused", api, interval=0.002)
    daemon.set_paused(paused=True)

    async def scenario() -> None:
        await daemon.start()
        await asyncio.sleep(0.3)
        await daemon.stop()

    asyncio.run(scenario())
    assert store.queries.count(RunFilter()) == 0


def _published_issues(store: Store, pr_id: int) -> None:
    """Прогон с замечаниями, опубликованный на коммите PR, и его таска."""
    run_id = store.runs.create(new_run(make_pr(pr_id), Trigger.AUTO, fingerprint="", dry_run=False))
    store.runs.finish(run_id, RunStatus.ISSUES)
    store.runs.mark_published(run_id)
    store.comments.add(run_id, 500 + pr_id, 0, CommentKind.TASK, "ai", TaskDetails("t"))


def test_poll_hands_skipped_prs_to_task_watch(tmp_path: Path, store: Store) -> None:
    _published_issues(store, 1)
    _published_issues(store, 2)
    api = FakeBitbucket(
        prs=[with_status(make_pr(1), "NEEDS_WORK"), with_status(make_pr(2), "NEEDS_WORK")],
    )
    config = make_config(tmp_path / "home", "unused", daemon_extra={"resolve_delay_minutes": 0})
    daemon = Daemon(config, store, api)
    api.tasks_error = BitbucketError("denied", status_code=403)
    assert asyncio.run(daemon.poll()) == 0
    warnings = [event.message for event in store.events.list(EventLevel.WARNING)]
    assert sum("denied" in message for message in warnings) == 2
    api.tasks_error = None
    store.watch.save(replace(store.watch.get("PROJ", "repo", 1, 1), retry_after=None))
    asyncio.run(daemon.poll())
    assert api.statuses == [(1, ReviewStatus.APPROVED)]
