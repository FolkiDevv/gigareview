import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeBitbucket, Origin, make_config, make_pr

from gigareview.errors import BitbucketError
from gigareview.pipeline import Reviewer, new_run
from gigareview.statuses import EventLevel, Outcome, ReviewStatus, RunStatus, Trigger
from gigareview.store import RunRecord, Store


def _review(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    *,
    api: FakeBitbucket | None = None,
    dry_run: bool = False,
    commit: str = "",
    **config_options: Any,
) -> tuple[RunRecord, FakeBitbucket]:
    config = make_config(tmp_path / "home", origin.url, **config_options)
    pr = make_pr(commit=commit or origin.feature)
    bitbucket = api or FakeBitbucket(prs=[pr])
    run_id = store.runs.create(new_run(pr, Trigger.AUTO, fingerprint="fp", dry_run=dry_run))
    asyncio.run(Reviewer(config, store, bitbucket).review(run_id, pr))
    run = store.runs.get(run_id)
    assert run is not None
    return run, bitbucket


def test_passing_run_approves(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, api = _review(
        tmp_path,
        store,
        origin,
        scenarios={"lint": {"run": "grep -q feature app.py"}},
    )
    assert run.status is RunStatus.PASSED
    assert not run.error
    assert "git switch" in run.setup_log
    assert api.tasks == []
    assert len(api.summaries) == 1
    assert "✅ пройден" in api.summaries[0].text
    assert f"/runs/{run.id})" in api.summaries[0].text
    assert api.statuses == [(1, ReviewStatus.APPROVED)]
    assert [record.outcome for record in store.runs.scenarios(run.id)] == [Outcome.PASSED]
    assert not list((tmp_path / "home" / "artifacts").iterdir())


def test_issues_post_tasks_and_request_changes(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    ai_tasks = [{"text": "one", "path": "app.py", "line": 1}, {"text": "two"}]
    scenarios = {
        "ok": {"run": "true"},
        "ai": {
            "kind": "ai",
            "run": f"echo '{json.dumps(ai_tasks)}'",
            "validate": "exit 1",
        },
    }
    run, api = _review(tmp_path, store, origin, scenarios=scenarios)
    assert run.status is RunStatus.ISSUES
    assert [task.text.rsplit("\n", 1)[-1] for task in api.tasks] == ["one", "two"]
    assert api.tasks[0].anchor is not None
    assert api.tasks[0].anchor.path == "app.py"
    assert api.tasks[1].anchor is None
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    records = store.runs.scenarios(run.id)
    assert records[1].tasks == ("one", "two")
    assert len(store.comments.open_tasks("PROJ", "repo", 1)) == 2


def test_next_run_resolves_previous_tasks(tmp_path: Path, store: Store, origin: Origin) -> None:
    failing = {"lint": {"run": "echo bad; exit 1"}}
    _, api = _review(tmp_path, store, origin, scenarios=failing)
    first_task = api.tasks[0].id
    run, api = _review(tmp_path, store, origin, api=api, scenarios={"lint": {"run": "true"}})
    assert run.status is RunStatus.PASSED
    assert api.resolved == [first_task]
    assert store.comments.open_tasks("PROJ", "repo", 1) == []


def test_deleted_task_is_forgotten(tmp_path: Path, store: Store, origin: Origin) -> None:
    _, api = _review(tmp_path, store, origin, scenarios={"lint": {"run": "exit 1"}})
    api.resolve_error = BitbucketError("gone", status_code=404)
    _review(tmp_path, store, origin, api=api)
    assert store.comments.open_tasks("PROJ", "repo", 1) == []


def test_resolve_failure_is_logged_and_kept(tmp_path: Path, store: Store, origin: Origin) -> None:
    _, api = _review(tmp_path, store, origin, scenarios={"lint": {"run": "exit 1"}})
    api.resolve_error = BitbucketError("denied", status_code=403)
    run, _ = _review(tmp_path, store, origin, api=api)
    assert run.status is RunStatus.PASSED
    assert len(store.comments.open_tasks("PROJ", "repo", 1)) == 1
    warnings = store.events.list(EventLevel.WARNING)
    assert "denied" in warnings[0].message


def test_scenario_errors_do_not_change_review(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, api = _review(tmp_path, store, origin, scenarios={"lint": {"run": "exit 2"}})
    assert run.status is RunStatus.FAILED
    assert api.statuses == []
    assert "ошибка сценария" in api.summaries[0].text


def test_unknown_user_fails_before_publishing(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    api = FakeBitbucket(prs=[make_pr(commit=origin.feature)], slug_error=BitbucketError("who?"))
    run, api = _review(tmp_path, store, origin, api=api, scenarios={"lint": {"run": "exit 1"}})
    assert run.status is RunStatus.ERROR
    assert "who?" in run.error
    assert api.comments == []


def test_dry_run_publishes_nothing(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, api = _review(tmp_path, store, origin, dry_run=True, scenarios={"lint": {"run": "exit 1"}})
    assert run.status is RunStatus.ISSUES
    assert run.dry_run
    assert "Сухой режим" in run.error
    assert api.comments == []
    assert api.statuses == []


def test_pr_changed_during_run_is_not_published(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    api = FakeBitbucket(overrides={1: make_pr(commit="newer")})
    run, api = _review(tmp_path, store, origin, api=api)
    assert run.status is RunStatus.PASSED
    assert "не опубликованы" in run.error
    assert api.comments == []


def test_scenario_environment(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GR_TEST_TOKEN", "secret")
    check = (
        'test -z "${GR_TEST_TOKEN:-}" && test "$TARGET_BRANCH" = master '
        f'&& test "$MERGE_BASE" = {origin.master} && test "$PR_ID" = 1 '
        '&& test "$SOURCE_BRANCH" = feature && test -d "$GR_ARTIFACTS" '
        '&& test "$GR_REPO_DIR" = "$PWD" && test "$PR_PROJECT/$PR_REPO" = PROJ/repo'
    )
    run, _ = _review(tmp_path, store, origin, scenarios={"env": {"run": check}})
    assert run.status is RunStatus.PASSED, store.runs.scenarios(run.id)[0].run_output


def test_init_runs_and_failure_is_error(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, _ = _review(tmp_path, store, origin, repo_extra={"init": "touch .venv-made"})
    assert run.status is RunStatus.PASSED
    assert "touch .venv-made" in run.setup_log
    run, api = _review(tmp_path, store, origin, repo_extra={"init": "echo broken; exit 3"})
    assert run.status is RunStatus.ERROR
    assert "init" in run.error
    assert "broken" in run.setup_log
    assert api.comments == []
    assert "упал" in store.events.list(EventLevel.ERROR)[0].message


def test_git_failure_is_error(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, _ = _review(tmp_path, store, origin, commit="0" * 40)
    assert run.status is RunStatus.ERROR
    assert "git switch" in run.error


def test_unknown_repo_is_error(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, _ = _review(tmp_path, store, origin, repo_extra={"slug": "other"})
    assert run.status is RunStatus.ERROR
    assert "не описан" in run.error


def test_no_scenarios_is_error(tmp_path: Path, store: Store, origin: Origin) -> None:
    run, _ = _review(tmp_path, store, origin, scenarios={})
    assert run.status is RunStatus.ERROR
    assert "ни одного сценария" in run.error


def test_unexpected_exception_is_error(tmp_path: Path, store: Store, origin: Origin) -> None:
    api = FakeBitbucket(get_error=RuntimeError("kaboom"))
    run, _ = _review(tmp_path, store, origin, api=api)
    assert run.status is RunStatus.ERROR
    assert "kaboom" in run.error


def test_mark_running_failure_is_recorded(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(run_id: int) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.runs, "mark_running", broken)
    run, api = _review(tmp_path, store, origin)
    assert run.status is RunStatus.ERROR
    assert "database is locked" in run.error
    assert api.comments == []


def test_cancellation_marks_interrupted(tmp_path: Path, store: Store, origin: Origin) -> None:
    config = make_config(tmp_path / "home", origin.url, scenarios={"slow": {"run": "sleep 30"}})
    pr = make_pr(commit=origin.feature)
    run_id = store.runs.create(new_run(pr, Trigger.MANUAL, fingerprint="", dry_run=False))

    async def scenario() -> None:
        task = asyncio.create_task(
            Reviewer(config, store, FakeBitbucket(prs=[pr])).review(run_id, pr),
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    run = store.runs.get(run_id)
    assert run is not None
    assert run.status is RunStatus.INTERRUPTED
