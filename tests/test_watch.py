import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeBitbucket, Origin, make_config, make_pr, with_counts, with_status

from gigareview.bitbucket import PullRequest
from gigareview.config import Config
from gigareview.errors import BitbucketError
from gigareview.pipeline import Reviewer, TaskWatch, new_run
from gigareview.pipeline import watch as watch_module
from gigareview.statuses import EventLevel, ReviewStatus, RunStatus, Trigger
from gigareview.store import Store

_TASKS = '{"tasks": [{"text": "one"}, {"text": "two"}]}'
_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(watch_module, "utc_now", fake)
    return fake


def _config(tmp_path: Path, origin: Origin, **daemon: Any) -> Config:
    scenarios: dict[str, Any] = {
        "ai": {"kind": "ai", "incremental": True, "run": f"echo '{_TASKS}'; exit 1"},
    }
    if daemon.pop("broken", False):
        scenarios["broken"] = {"run": "exit 2"}
    return make_config(tmp_path / "home", origin.url, scenarios=scenarios, daemon_extra=daemon)


def _reviewed(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    **daemon: Any,
) -> tuple[FakeBitbucket, Config]:
    """Прогон с двумя тасками ИИ-ревью: PR в NEEDS_WORK."""
    config = _config(tmp_path, origin, **daemon)
    pr = make_pr(commit=origin.feature)
    api = FakeBitbucket(prs=[pr])
    run_id = store.runs.create(new_run(pr, Trigger.AUTO, fingerprint="fp", dry_run=False))
    status = asyncio.run(Reviewer(config, store, api).review(run_id, pr))
    assert status is RunStatus.ISSUES
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    api.calls.clear()
    return api, config


def _close_all(api: FakeBitbucket) -> None:
    for task in api.tasks:
        api.states[task.id] = "RESOLVED"


def _check(store: Store, api: FakeBitbucket, config: Config, pr: PullRequest | None = None) -> None:
    asyncio.run(TaskWatch(store, api).check([pr or api.prs[0]], config))


def _messages(store: Store, level: EventLevel | None = None) -> list[str]:
    return [event.message for event in store.events.list(level, limit=100)]


def test_closed_tasks_approve_after_quiet_period(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin)
    _close_all(api)
    _check(store, api, config)
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    clock.advance(10)
    _check(store, api, config)
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    clock.advance(6)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)
    assert "Итог: ✅ замечания закрыты вручную — PR одобрен" in api.status_texts[-1]
    assert "| ai | ❌ замечания: новых 2 |" in api.status_texts[-1]
    assert "**Как работать со мной**" in api.status_texts[-1]
    assert any("PR одобрен" in message for message in _messages(store))
    dismissed = store.comments.dismissed_tasks("PROJ", "repo", 1, "ai")
    assert len(dismissed) == 2


def test_settled_pr_is_not_touched_again(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    api.prs = [with_counts(api.prs[0], 0, 2)]
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)
    api.calls.clear()
    clock.advance(60)
    _check(store, api, config)
    assert api.calls == []
    assert len(api.statuses) == 2


def test_zero_open_tasks_by_counts_skips_listing(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    api.prs = [with_counts(api.prs[0], 0, 2)]
    _check(store, api, config)
    assert "list_tasks" not in api.calls
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)


def test_unchanged_counts_make_no_requests(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin)
    api.prs = [with_counts(api.prs[0], 2, 0)]
    _check(store, api, config)
    assert api.calls == ["list_tasks"]
    for _ in range(10):
        clock.advance(30)
        _check(store, api, config)
    assert api.calls == ["list_tasks"]
    clock.advance(60)
    _check(store, api, config)
    assert api.calls == ["list_tasks", "list_tasks"]


def test_without_counts_rechecks_on_schedule(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin)
    clock.now = datetime.now(UTC)
    _check(store, api, config)
    clock.advance(1)
    _check(store, api, config)
    assert api.calls.count("list_tasks") == 1
    clock.advance(5)
    _check(store, api, config)
    assert api.calls.count("list_tasks") == 2


def test_open_task_keeps_needs_work(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    api.states[api.tasks[0].id] = "RESOLVED"
    _check(store, api, config)
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]


def test_manual_task_of_user_blocks(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    api.add_task(1, "своё замечание")
    api.add_task(1, "чужое замечание", author="someone")
    _check(store, api, config)
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]


def test_task_reopened_in_quiet_period_restarts_it(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin)
    _close_all(api)
    _check(store, api, config)
    clock.advance(10)
    api.states[api.tasks[0].id] = "OPEN"
    _check(store, api, config)
    api.states[api.tasks[0].id] = "RESOLVED"
    clock.advance(10)
    _check(store, api, config)
    clock.advance(10)
    _check(store, api, config)
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    clock.advance(6)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)


def test_task_reopened_after_approval_needs_work_again(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)
    api.states[api.tasks[1].id] = "OPEN"
    clock.advance(10)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.NEEDS_WORK)
    assert "Итог: ❌ есть замечания" in api.status_texts[-1]
    _close_all(api)
    clock.advance(10)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)


def test_status_changed_by_hand_is_left_alone(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    api.prs = [with_status(api.prs[0], "UNAPPROVED")]
    _check(store, api, config)
    assert api.calls == []
    assert len(api.statuses) == 1


@pytest.mark.parametrize(
    "daemon",
    [{"approve": False}, {"broken": True}],
    ids=["without-approve", "failed-check"],
)
def test_lifts_needs_work_without_approval(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
    daemon: dict[str, Any],
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0, **daemon)
    _close_all(api)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.UNAPPROVED)
    assert "«Требуются изменения» снят" in api.status_texts[-1]


def test_new_commit_is_left_to_the_next_run(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    pushed = with_status(make_pr(commit="new"), "NEEDS_WORK")
    _check(store, api, config, pushed)
    assert api.calls == []


def test_pr_pushed_before_approval_is_not_approved(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    listed = api.prs[0]
    api.overrides[1] = with_status(make_pr(commit="new"), "NEEDS_WORK")
    _check(store, api, config, listed)
    assert len(api.statuses) == 1


def test_active_run_and_dry_run_are_skipped(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    dry = config.model_copy(update={"daemon": config.daemon.model_copy(update={"dry_run": True})})
    _check(store, api, dry)
    queued = store.runs.create(new_run(api.prs[0], Trigger.MANUAL, fingerprint="", dry_run=False))
    _check(store, api, config)
    assert api.calls == []
    store.runs.finish(queued, RunStatus.INTERRUPTED)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)


def test_bitbucket_error_is_logged_and_paused(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    api.tasks_error = BitbucketError("denied", status_code=403)
    _check(store, api, config)
    assert any("denied" in message for message in _messages(store, EventLevel.WARNING))
    api.tasks_error = None
    clock.advance(1)
    _check(store, api, config)
    assert len(api.statuses) == 1
    clock.advance(5)
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)


def test_deleted_summary_is_not_posted_again(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    clock: Clock,
) -> None:
    api, config = _reviewed(tmp_path, store, origin, resolve_delay_minutes=0)
    _close_all(api)
    summaries = len(api.summaries)
    api.comments = [comment for comment in api.comments if comment.task]
    _check(store, api, config)
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)
    assert len(api.summaries) == 0
    assert summaries == 1
