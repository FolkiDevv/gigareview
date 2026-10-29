import asyncio
import json
import os
import sqlite3
import tomllib
from pathlib import Path
from typing import Any, override

import pytest
from fakes import FakeBitbucket, Origin, git, make_config, make_pr, with_status

from gigareview.bitbucket import PullRequest
from gigareview.config import SAMPLE_CONFIG
from gigareview.errors import BitbucketError
from gigareview.pipeline import Reviewer, new_run
from gigareview.pipeline.workspace import RepoLock
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


def test_passing_run_without_approve_keeps_status(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    run, api = _review(tmp_path, store, origin, daemon_extra={"approve": False})
    assert run.status is RunStatus.PASSED
    assert run.published
    assert len(api.summaries) == 1
    assert "Итог: ✅ замечаний нет\n" in api.summaries[0].text
    assert "PR одобрен" not in api.summaries[0].text
    assert "и одобрит PR" not in api.summaries[0].text
    assert api.statuses == []


class _ReloadingBitbucket(FakeBitbucket):
    """Перед публикацией подменяет конфигурацию рецензента — как перечитывание посреди прогона."""

    reviewer: Reviewer | None = None

    @override
    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequest:
        if self.reviewer is not None:
            daemon = self.reviewer.config.daemon.model_copy(update={"approve": False})
            self.reviewer.config = self.reviewer.config.model_copy(update={"daemon": daemon})
        return await super().get_pull_request(project, repo, pr_id)


def test_reload_mid_run_keeps_approve_decision(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    pr = make_pr(commit=origin.feature)
    api = _ReloadingBitbucket(prs=[pr])
    reviewer = Reviewer(make_config(tmp_path / "home", origin.url), store, api)
    api.reviewer = reviewer
    run_id = store.runs.create(new_run(pr, Trigger.AUTO, fingerprint="fp", dry_run=False))
    asyncio.run(reviewer.review(run_id, pr))
    assert api.statuses == [(1, ReviewStatus.APPROVED)]
    assert "PR одобрен" in api.summaries[0].text


def test_issues_without_approve_still_request_changes(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    run, api = _review(
        tmp_path,
        store,
        origin,
        scenarios={"lint": {"run": "echo bad", "validate": "exit 1"}},
        daemon_extra={"approve": False},
    )
    assert run.status is RunStatus.ISSUES
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]


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
    assert "проверка не удалась (подробности — в gigareview)" in api.summaries[0].text


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
    assert api.tasks == []
    assert api.statuses == []
    assert "⏳ проверки начаты" in api.summaries[0].text
    assert not run.published


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
        '&& test "$GR_REPO_DIR" = "$PWD" && test "$PR_PROJECT/$PR_REPO" = PROJ/repo '
        '&& test "$PR_TITLE" = "PR 1" && test "$PR_AUTHOR" = Автор && test -z "$PR_DESCRIPTION" '
        '&& "$GIGAREVIEW" --version && test -z "$GR_PREV_COMMIT" '
        '&& test "$(cat "$GR_OPEN_TASKS")" = "[]"'
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
    assert api.tasks == []
    assert "прогон не удался" in api.summaries[-1].text
    assert len(api.summaries) == 1
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


def test_busy_checkout_is_error(tmp_path: Path, store: Store, origin: Origin) -> None:
    config = make_config(tmp_path / "home", origin.url)
    repo = config.repos[0]
    with RepoLock(config.daemon.home, repo):
        run, api = _review(tmp_path, store, origin)
    assert run.status is RunStatus.ERROR
    assert "занята" in run.error
    assert api.comments == []
    with RepoLock(config.daemon.home, repo) as first, RepoLock(tmp_path / "other", repo) as second:
        assert first is not second


def test_fork_pr_flag() -> None:
    pr = make_pr()
    fork_repository = make_pr(repo="fork").to_ref.repository
    fork_ref = pr.from_ref.model_copy(update={"repository": fork_repository})
    fork = pr.model_copy(update={"from_ref": fork_ref})
    assert not pr.is_fork
    assert fork.is_fork


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


def _incremental(run: str) -> dict[str, Any]:
    return {"ai": {"kind": "ai", "incremental": True, "run": run}}


def test_incremental_scenario_keeps_tasks_until_resolved(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    first = '{"tasks": [{"text": "one", "path": "app.py", "line": 1}, {"text": "two"}]}'
    _, api = _review(tmp_path, store, origin, scenarios=_incremental(f"echo '{first}'; exit 1"))
    one, two = (task.id for task in api.tasks)
    open_tasks = store.comments.open_tasks("PROJ", "repo", 1, "ai")
    assert [(task.details.text, task.details.path, task.details.line) for task in open_tasks] == [
        ("one", "app.py", 1),
        ("two", None, None),
    ]
    check = f'grep -q \'"id": {one}\' "$GR_OPEN_TASKS" || exit 2'
    second = f'{check}; echo \'{{"tasks": [], "resolved": [{one}, 999]}}\''
    run, api = _review(tmp_path, store, origin, api=api, scenarios=_incremental(second))
    assert run.status is RunStatus.ISSUES
    assert api.resolved == [one]
    assert api.statuses[-1] == (1, ReviewStatus.NEEDS_WORK)
    assert "открыто прежних 1, закрыто 1" in api.summaries[-1].text
    third = f"echo '{{\"resolved\": [{two}]}}'"
    run, api = _review(tmp_path, store, origin, api=api, scenarios=_incremental(third))
    assert run.status is RunStatus.PASSED
    assert api.resolved == [one, two]
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)


def test_tasks_closed_by_user_do_not_need_work(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    first = '{"tasks": [{"text": "one"}, {"text": "two"}, {"text": "three"}]}'
    _, api = _review(tmp_path, store, origin, scenarios=_incremental(f"echo '{first}'; exit 1"))
    one, two, three = (task.id for task in api.tasks)
    api.states[two] = "RESOLVED"
    api.deleted.add(three)
    check = f'grep -q \'"id": {two}\' "$GR_OPEN_TASKS" && exit 2'
    second = f'{check}; echo \'{{"tasks": [], "resolved": [{one}]}}\''
    run, api = _review(tmp_path, store, origin, api=api, scenarios=_incremental(second))
    assert run.status is RunStatus.PASSED
    assert api.resolved == [one]
    assert api.statuses[-1] == (1, ReviewStatus.APPROVED)
    assert store.comments.open_tasks("PROJ", "repo", 1) == []


def test_tasks_left_open_by_user_still_need_work(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    first = '{"tasks": [{"text": "one"}, {"text": "two"}, {"text": "three"}]}'
    _, api = _review(tmp_path, store, origin, scenarios=_incremental(f"echo '{first}'; exit 1"))
    one, two, three = (task.id for task in api.tasks)
    api.states[two] = "RESOLVED"
    second = f'echo \'{{"tasks": [], "resolved": [{one}]}}\''
    run, api = _review(tmp_path, store, origin, api=api, scenarios=_incremental(second))
    assert run.status is RunStatus.ISSUES
    assert api.statuses[-1] == (1, ReviewStatus.NEEDS_WORK)
    assert [task.comment_id for task in store.comments.open_tasks("PROJ", "repo", 1)] == [three]


def test_task_reopened_by_user_needs_work_again(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    first = '{"tasks": [{"text": "one"}]}'
    _, api = _review(tmp_path, store, origin, scenarios=_incremental(f"echo '{first}'; exit 1"))
    task = api.tasks[0].id
    api.states[task] = "RESOLVED"
    quiet = _incremental("echo '{\"tasks\": []}'")
    run, api = _review(tmp_path, store, origin, api=api, scenarios=quiet)
    assert run.status is RunStatus.PASSED
    api.states[task] = "OPEN"
    check = _incremental(f'grep -q \'"id": {task}\' "$GR_OPEN_TASKS" || exit 2; echo "[]"')
    run, api = _review(tmp_path, store, origin, api=api, scenarios=check)
    assert run.status is RunStatus.ISSUES
    assert api.statuses[-1] == (1, ReviewStatus.NEEDS_WORK)
    assert [record.comment_id for record in store.comments.open_tasks("PROJ", "repo", 1)] == [
        task,
    ]


def test_task_state_error_fails_run(tmp_path: Path, store: Store, origin: Origin) -> None:
    first = _incremental('echo \'[{"text": "a"}]\'; exit 1')
    _, api = _review(tmp_path, store, origin, scenarios=first)
    api.tasks_error = BitbucketError("denied", status_code=403)
    run, api = _review(tmp_path, store, origin, api=api, scenarios=first)
    assert run.status is RunStatus.ERROR
    assert "denied" in run.error
    assert len(store.comments.open_tasks("PROJ", "repo", 1)) == 1


def test_failed_incremental_scenario_resolves_nothing(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    first = _incremental('echo \'[{"text": "a"}]\'; exit 1')
    _, api = _review(tmp_path, store, origin, scenarios=first)
    broken = "echo '{\"resolved\": [100]}'; exit 2"
    run, api = _review(tmp_path, store, origin, api=api, scenarios=_incremental(broken))
    assert run.status is RunStatus.FAILED
    assert api.resolved == []
    assert len(store.comments.open_tasks("PROJ", "repo", 1)) == 1


def _push(origin: Origin, text: str, *, amend: bool = False) -> str:
    (origin.work / "app.py").write_text(text)
    if amend:
        git("commit", "-q", "--amend", "-am", "rewritten", cwd=origin.work)
    else:
        git("commit", "-q", "-am", "next", cwd=origin.work)
    git("push", "-q", "--force", origin.url, "feature", cwd=origin.work)
    return git("rev-parse", "HEAD", cwd=origin.work)


def test_next_commit_gets_previous_review_commit(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    scenarios = _incremental(r'echo "prev=$GR_PREV_COMMIT"; echo "{\"tasks\": []}"')
    run, api = _review(tmp_path, store, origin, scenarios=scenarios)
    assert "prev=\n" in store.runs.scenarios(run.id)[0].run_output
    second = _push(origin, "print('v2')\n")
    api.prs = [make_pr(commit=second)]
    run, api = _review(tmp_path, store, origin, api=api, commit=second, scenarios=scenarios)
    assert f"prev={origin.feature}\n" in store.runs.scenarios(run.id)[0].run_output
    rewritten = _push(origin, "print('v3')\n", amend=True)
    api.prs = [make_pr(commit=rewritten)]
    run, _ = _review(tmp_path, store, origin, api=api, commit=rewritten, scenarios=scenarios)
    assert "prev=\n" in store.runs.scenarios(run.id)[0].run_output


_FAKE_GIGACREW = """\
#!/bin/bash
case "$*" in *"Full review"*) ;; *) echo "unexpected prompt"; exit 1 ;; esac
echo "● read_file app.py [1-1]"
cat <<'ANSWER'
Готово.
```json
{"findings": [
  {"path": "app.py", "line": 1, "severity": "critical", "title": "Сломан вывод",
   "body": "Печатает не то", "suggestion": "Вернуть master", "confidence": 95},
  {"path": "app.py", "line": 1, "severity": "minor", "title": "Мелочь", "confidence": 99}
], "resolved": []}
```
ANSWER
"""


def test_sample_ai_review_with_fake_gigacrew(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gigacrew"
    fake.write_text(_FAKE_GIGACREW)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    sample = tomllib.loads(SAMPLE_CONFIG)["scenarios"]["ai-review"]
    run, api = _review(tmp_path, store, origin, scenarios={"ai-review": sample})
    records = store.runs.scenarios(run.id)
    assert run.status is RunStatus.ISSUES, records[0]
    assert len(api.tasks) == 1
    task = api.tasks[0]
    assert "**🔴 Критично: Сломан вывод**" in task.text
    assert "**Как исправить:** Вернуть master" in task.text
    assert task.anchor is not None
    assert (task.anchor.path, task.anchor.line) == ("app.py", 1)
    assert "к публикации: 1" in records[0].validate_output


def _ai(run: str) -> dict[str, Any]:
    return {"kind": "ai", "run": run}


def test_status_comment_follows_phases(tmp_path: Path, store: Store, origin: Origin) -> None:
    scenarios = {
        "smart": _ai('test -n "$(cat "$GR_OPEN_TASKS")" && echo \'[{"text": "ai"}]\'; exit 1'),
        "lint": {"run": "echo bad; exit 1"},
    }
    run, api = _review(tmp_path, store, origin, scenarios=scenarios)
    assert run.status is RunStatus.ISSUES
    assert [record.name for record in store.runs.scenarios(run.id)] == ["lint", "smart"]
    started, simple_done, finished = api.status_texts
    assert "⏳ проверки начаты" in started
    assert started.count("⏳ ожидает") == 2
    assert "| lint | ❌ замечания: 1 |" in simple_done
    assert "| smart | ⏳ ожидает |" in simple_done
    assert "идут ИИ-сценарии" in simple_done
    assert "| smart | ❌ замечания: 1 |" in finished
    assert "нужны изменения" in finished
    assert len(api.summaries) == 1
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    simple_task, ai_task = api.tasks
    assert "bad" in simple_task.text
    assert ai_task.text.endswith("ai")
    assert run.published


def test_new_commit_updates_same_status_comment(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    _, api = _review(tmp_path, store, origin)
    second = _push(origin, "print('v2')\n")
    api.prs = [make_pr(commit=second)]
    _, api = _review(tmp_path, store, origin, api=api, commit=second)
    assert len(api.summaries) == 1
    assert second[:10] in api.summaries[0].text
    assert "⏳ проверки начаты" in api.status_texts[2]
    assert second[:10] in api.status_texts[2]
    status_id = api.summaries[0].id
    assert api.edits == [status_id, status_id, status_id]


def test_deleted_status_comment_is_posted_again(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    _, api = _review(tmp_path, store, origin)
    api.comments.clear()
    run, api = _review(tmp_path, store, origin, api=api)
    assert run.status is RunStatus.PASSED
    assert len(api.summaries) == 1
    assert "PR одобрен" in api.summaries[0].text


def test_status_comment_error_is_run_error(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    _, api = _review(tmp_path, store, origin)
    api.update_error = BitbucketError("denied", status_code=403)
    run, _ = _review(tmp_path, store, origin, api=api)
    assert run.status is RunStatus.ERROR
    assert "denied" in run.error


def test_pr_changed_after_simple_phase_skips_ai(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    api = FakeBitbucket(overrides={1: make_pr(commit="newer")})
    scenarios = {"lint": {"run": "true"}, "smart": _ai("echo '[]'")}
    run, api = _review(tmp_path, store, origin, api=api, scenarios=scenarios)
    assert "оставшиеся сценарии не запускались" in run.error
    assert [record.name for record in store.runs.scenarios(run.id)] == ["lint"]
    assert api.statuses == []


def _review_pr(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    api: FakeBitbucket,
    **config_options: Any,
) -> RunRecord:
    """Прогон по PR из ``api.prs`` (с его статусом ревьюера и счётчиками)."""
    config = make_config(tmp_path / "home", origin.url, **config_options)
    pr = api.prs[0]
    run_id = store.runs.create(new_run(pr, Trigger.AUTO, fingerprint="fp", dry_run=False))
    asyncio.run(Reviewer(config, store, api).review(run_id, pr))
    run = store.runs.get(run_id)
    assert run is not None
    return run


def test_first_run_does_not_list_tasks(tmp_path: Path, store: Store, origin: Origin) -> None:
    _, api = _review(tmp_path, store, origin, scenarios=_incremental("echo '[]'"))
    assert "list_tasks" not in api.calls


def test_one_task_listing_per_phase_for_many_old_tasks(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    many = json.dumps({"tasks": [{"text": f"t{index}"} for index in range(8)]})
    scenarios = {"lint": {"run": "echo bad", "validate": "exit 1"}}
    scenarios.update(_incremental(f"echo '{many}'; exit 1"))
    _, api = _review(tmp_path, store, origin, scenarios=scenarios)
    api.calls.clear()
    _review(tmp_path, store, origin, api=api, scenarios=scenarios)
    assert api.calls.count("list_tasks") == 2


def test_status_is_not_set_again(tmp_path: Path, store: Store, origin: Origin) -> None:
    scenarios = {"lint": {"run": "echo bad", "validate": "exit 1"}}
    _, api = _review(tmp_path, store, origin, scenarios=scenarios)
    _review(tmp_path, store, origin, api=api, scenarios=scenarios)
    assert api.statuses == [(1, ReviewStatus.NEEDS_WORK)]


def test_clean_run_without_approve_lifts_needs_work(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    api = FakeBitbucket(prs=[with_status(make_pr(commit=origin.feature), "NEEDS_WORK")])
    run = _review_pr(tmp_path, store, origin, api, daemon_extra={"approve": False})
    assert run.status is RunStatus.PASSED
    assert api.statuses == [(1, ReviewStatus.UNAPPROVED)]


def test_new_commit_withdraws_approval_until_checked(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    scenarios = {"lint": {"run": "echo bad", "validate": "exit 1"}}
    api = FakeBitbucket(prs=[with_status(make_pr(commit=origin.feature), "APPROVED")])
    _review_pr(tmp_path, store, origin, api, scenarios=scenarios)
    assert api.statuses == [(1, ReviewStatus.UNAPPROVED), (1, ReviewStatus.NEEDS_WORK)]
    events = [event.message for event in store.events.list(None, limit=50)]
    assert any("одобрение снято" in message for message in events)


def test_rerun_of_reviewed_commit_keeps_approval(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    _, api = _review(tmp_path, store, origin)
    assert api.statuses == [(1, ReviewStatus.APPROVED)]
    _review_pr(tmp_path, store, origin, api)
    assert api.statuses == [(1, ReviewStatus.APPROVED)]


def test_dry_run_keeps_approval(tmp_path: Path, store: Store, origin: Origin) -> None:
    api = FakeBitbucket(prs=[with_status(make_pr(commit=origin.feature), "APPROVED")])
    _review(tmp_path, store, origin, api=api, dry_run=True)
    assert api.statuses == []


def test_dismissed_tasks_go_to_their_own_file(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    first = '{"tasks": [{"text": "one"}, {"text": "two"}]}'
    _, api = _review(tmp_path, store, origin, scenarios=_incremental(f"echo '{first}'; exit 1"))
    one, two = (task.id for task in api.tasks)
    api.states[two] = "RESOLVED"
    check = (
        f'grep -q \'"id": {two}\' "$GR_DISMISSED_TASKS" || exit 2; '
        f'grep -q \'"id": {two}\' "$GR_OPEN_TASKS" && exit 2; '
        f'grep -q \'"id": {one}\' "$GR_OPEN_TASKS" || exit 2; '
        f"echo '{{\"resolved\": [{one}]}}'"
    )
    run, api = _review(tmp_path, store, origin, api=api, scenarios=_incremental(check))
    assert run.status is RunStatus.PASSED
    dismissed = store.comments.dismissed_tasks("PROJ", "repo", 1, "ai")
    assert [record.comment_id for record in dismissed] == [two]
