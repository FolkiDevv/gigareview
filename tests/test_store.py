import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from gigareview.statuses import EventLevel, Outcome, RunStatus, Trigger
from gigareview.store import (
    CommentKind,
    NewRun,
    RunFilter,
    ScenarioRecord,
    Store,
    TaskDetails,
    TaskWatchRecord,
)
from gigareview.store.clock import utc_now
from gigareview.store.schema import MIGRATIONS


def _new(pr_id: int = 1, commit: str = "abc", repo: str = "repo") -> NewRun:
    return NewRun(
        project="PROJ",
        repo=repo,
        pr_id=pr_id,
        pr_title="Title",
        pr_author="Author",
        pr_url="https://bb/pr",
        source_branch="feature",
        target_branch="master",
        commit_id=commit,
        trigger=Trigger.AUTO,
        fingerprint="fp",
    )


def _scenario(name: str = "lint", outcome: Outcome = Outcome.PASSED) -> ScenarioRecord:
    return ScenarioRecord(
        name=name,
        title=name.upper(),
        kind="simple",
        outcome=outcome,
        run_exit=0,
        run_output="ok",
        duration=1.5,
        tasks=("first", "second") if outcome is Outcome.ISSUES else (),
    )


def test_run_lifecycle(store: Store) -> None:
    run_id = store.runs.create(_new())
    run = store.runs.get(run_id)
    assert run is not None
    assert run.status is RunStatus.QUEUED
    assert run.status.is_active
    assert run.duration is None
    store.runs.mark_running(run_id)
    store.runs.append_setup_log(run_id, "git fetch\n")
    store.runs.append_setup_log(run_id, "uv sync\n")
    store.runs.finish(run_id, RunStatus.ISSUES, "note")
    run = store.runs.get(run_id)
    assert run is not None
    assert run.status is RunStatus.ISSUES
    assert not run.status.is_active
    assert run.error == "note"
    assert run.setup_log == "git fetch\nuv sync\n"
    assert run.duration is not None
    assert run.repo_key == "PROJ/repo"
    assert run.short_commit == "abc"
    assert run.trigger is Trigger.AUTO
    assert store.runs.get(run_id + 100) is None


def test_scenario_results_roundtrip(store: Store) -> None:
    run_id = store.runs.create(_new())
    store.runs.add_scenario(run_id, 1, _scenario("second", Outcome.ISSUES))
    store.runs.add_scenario(run_id, 0, _scenario("first"))
    first, second = store.runs.scenarios(run_id)
    assert first.name == "first"
    assert first.tasks == ()
    assert second.outcome is Outcome.ISSUES
    assert second.tasks == ("first", "second")


def test_should_skip(store: Store) -> None:
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=False)
    interrupted = store.runs.create(_new())
    store.runs.finish(interrupted, RunStatus.INTERRUPTED)
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=False)
    for _ in range(2):
        store.runs.finish(store.runs.create(_new()), RunStatus.ERROR)
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=False)
    store.runs.finish(store.runs.create(_new()), RunStatus.ERROR)
    assert store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=False)
    store.runs.create(_new(commit="def"))
    assert store.runs.should_skip("PROJ", "repo", 1, "def", dry_run=False)
    assert not store.runs.should_skip("PROJ", "repo", 2, "def", dry_run=False)


def test_manual_errors_do_not_use_auto_attempts(store: Store) -> None:
    for trigger in (Trigger.MANUAL, Trigger.CLI, Trigger.MANUAL):
        store.runs.finish(store.runs.create(replace(_new(), trigger=trigger)), RunStatus.ERROR)
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=False)


def test_dry_runs_do_not_block_real_runs(store: Store) -> None:
    run_id = store.runs.create(replace(_new(), dry_run=True))
    store.runs.finish(run_id, RunStatus.ISSUES)
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=False)
    assert store.runs.should_skip("PROJ", "repo", 1, "abc", dry_run=True)


def test_interrupt_active(store: Store) -> None:
    queued = store.runs.create(_new())
    running = store.runs.create(_new(pr_id=2))
    store.runs.mark_running(running)
    done = store.runs.create(_new(pr_id=3))
    store.runs.finish(done, RunStatus.PASSED)
    assert store.runs.interrupt_active() == 2
    statuses = [store.runs.get(run_id) for run_id in (queued, running, done)]
    assert [run.status for run in statuses if run] == [
        RunStatus.INTERRUPTED,
        RunStatus.INTERRUPTED,
        RunStatus.PASSED,
    ]


def test_queries_filters_and_pagination(store: Store) -> None:
    first = store.runs.create(_new(pr_id=1))
    store.runs.add_scenario(first, 0, _scenario("lint", Outcome.ISSUES))
    store.runs.finish(first, RunStatus.ISSUES)
    second = store.runs.create(_new(pr_id=2, repo="other"))
    store.runs.add_scenario(second, 0, _scenario("tests"))
    store.runs.finish(second, RunStatus.PASSED)
    queries = store.queries
    assert [run.id for run in queries.runs(RunFilter())] == [second, first]
    assert [run.id for run in queries.runs(RunFilter(), limit=1, offset=1)] == [first]
    assert queries.count(RunFilter(status=RunStatus.PASSED)) == 1
    assert queries.count(RunFilter(repo="PROJ/other")) == 1
    assert queries.count(RunFilter(pr_id=1)) == 1
    assert queries.count(RunFilter(scenario="lint")) == 1
    assert queries.count(RunFilter(outcome=Outcome.ISSUES)) == 1
    assert queries.count(RunFilter(scenario="tests", outcome=Outcome.ISSUES)) == 0
    assert queries.count(RunFilter(since=utc_now() + timedelta(hours=1))) == 0
    assert queries.count(RunFilter(since=utc_now() - timedelta(hours=1))) == 2
    assert queries.repos() == ["PROJ/other", "PROJ/repo"]
    assert queries.scenario_names() == ["lint", "tests"]


def test_stats_and_pull_requests(store: Store) -> None:
    for status in (RunStatus.PASSED, RunStatus.ISSUES, RunStatus.ISSUES):
        run_id = store.runs.create(_new())
        store.runs.mark_running(run_id)
        store.runs.finish(run_id, status)
    store.runs.create(_new(pr_id=7))
    stats = store.queries.stats()
    assert stats.total == 4
    assert stats.count(RunStatus.ISSUES) == 2
    assert stats.count(RunStatus.ERROR) == 0
    assert stats.average_duration is not None
    assert store.queries.stats(since=utc_now() + timedelta(days=1)).total == 0
    prs = store.queries.pull_requests()
    assert [(summary.last_run.pr_id, summary.runs_count) for summary in prs] == [(7, 1), (1, 3)]


def test_comments(store: Store) -> None:
    run_id = store.runs.create(_new())
    store.comments.add(run_id, 10, 0, CommentKind.TASK, "lint")
    store.comments.add(run_id, 11, 0, CommentKind.SUMMARY)
    other = store.runs.create(_new(pr_id=2))
    store.comments.add(other, 12, 0, CommentKind.TASK, "lint")
    tasks = store.comments.open_tasks("PROJ", "repo", 1)
    assert [task.comment_id for task in tasks] == [10]
    store.comments.mark_resolved(tasks[0].id)
    assert store.comments.open_tasks("PROJ", "repo", 1) == []
    assert [task.resolved for task in store.comments.tasks("PROJ", "repo", 1)] == [True]
    published = store.comments.for_run(run_id)
    assert [(comment.kind, comment.resolved) for comment in published] == [
        (CommentKind.TASK, True),
        (CommentKind.SUMMARY, False),
    ]
    store.comments.mark_open(tasks[0].id)
    assert len(store.comments.open_tasks("PROJ", "repo", 1)) == 1


def test_events(store: Store) -> None:
    run_id = store.runs.create(_new())
    store.events.add(EventLevel.NOTE, "hello")
    store.events.add(EventLevel.ERROR, "boom", run_id)
    assert store.events.count() == 2
    assert store.events.count(EventLevel.ERROR) == 1
    errors = store.events.list(EventLevel.ERROR)
    assert [(event.message, event.run_id) for event in errors] == [("boom", run_id)]
    assert [event.message for event in store.events.list(limit=1, offset=1)] == ["hello"]


def test_keyed_events_are_not_repeated(store: Store) -> None:
    store.events.add(EventLevel.DEBUG, "ждём", key="pr-1")
    store.events.add(EventLevel.DEBUG, "ждём", key="pr-1")
    store.events.add(EventLevel.DEBUG, "ждём", key="pr-2")
    store.events.add(EventLevel.NOTE, "ждём")
    store.events.add(EventLevel.NOTE, "ждём")
    assert store.events.count() == 4
    store.events.add(EventLevel.NOTE, "готово", key="pr-1")
    store.events.add(EventLevel.DEBUG, "ждём", key="pr-1")
    assert [event.message for event in store.events.list(limit=2)] == ["ждём", "готово"]
    assert store.events.count(EventLevel.DEBUG) == 3


def test_state(store: Store) -> None:
    assert not store.state.get("paused")
    assert store.state.get("paused", "no") == "no"
    store.state.set("paused", "1")
    store.state.set("paused", "2")
    assert store.state.get("paused") == "2"


def test_reopen_keeps_data_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    with Store(path) as first:
        run_id = first.runs.create(_new())
    with Store(path) as second:
        assert second.runs.get(run_id) is not None


def test_in_memory_database() -> None:
    with Store(Path(":memory:")) as memory:
        assert memory.runs.create(_new()) == 1


def test_comment_details_and_scenario_filter(store: Store) -> None:
    run_id = store.runs.create(_new())
    store.comments.add(run_id, 10, 0, CommentKind.TASK, "ai", TaskDetails("text", "a.py", 3))
    store.comments.add(run_id, 11, 0, CommentKind.TASK, "lint")
    ai_tasks = store.comments.open_tasks("PROJ", "repo", 1, "ai")
    assert [task.details for task in ai_tasks] == [TaskDetails("text", "a.py", 3)]
    assert len(store.comments.open_tasks("PROJ", "repo", 1)) == 2
    lint = store.comments.open_tasks("PROJ", "repo", 1, "lint")[0]
    assert lint.details == TaskDetails("")


def _reviewed(
    store: Store,
    commit: str,
    outcome: Outcome,
    *,
    published: bool = True,
    partial: bool = False,
) -> None:
    run_id = store.runs.create(_new(commit=commit))
    store.runs.add_scenario(run_id, 0, replace(_scenario("ai", outcome), partial=partial))
    if published:
        store.runs.mark_published(run_id)


def test_last_reviewed_commit(store: Store) -> None:
    def last(current: str = "now", scenario: str = "ai") -> str | None:
        return store.runs.last_reviewed_commit("PROJ", "repo", 1, scenario, other_than=current)

    assert last() is None
    _reviewed(store, "first", Outcome.ISSUES)
    _reviewed(store, "second", Outcome.PASSED)
    _reviewed(store, "broken", Outcome.ERROR)
    _reviewed(store, "dry", Outcome.PASSED, published=False)
    _reviewed(store, "now", Outcome.PASSED)
    assert last() == "second"
    assert last("other") == "now"
    assert last(scenario="lint") is None
    _reviewed(store, "interrupted", Outcome.ISSUES, partial=True)
    assert last("other") == "now"
    assert store.runs.scenarios(5)[0].partial is False
    assert store.runs.scenarios(6)[0].partial is True


def test_migration_adds_task_details_and_published(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as old:
        old.executescript(
            f"{MIGRATIONS[0]}\n"
            "INSERT INTO runs (project, repo, pr_id, source_branch, target_branch, commit_id, "
            "trigger, status, created_at) VALUES "
            "('PROJ', 'repo', 1, 'f', 'm', 'a', 'auto', 'passed', '2026-01-01T00:00:00+00:00'), "
            "('PROJ', 'repo', 1, 'f', 'm', 'b', 'auto', 'passed', '2026-01-01T00:00:00+00:00');\n"
            "INSERT INTO bb_comments (run_id, project, repo, pr_id, comment_id, version, kind) "
            "VALUES (1, 'PROJ', 'repo', 1, 4, 0, 'summary');\n"
            "PRAGMA user_version = 1;",
        )
    with Store(path) as store:
        published = store.runs.get(1)
        unpublished = store.runs.get(2)
        assert published is not None
        assert unpublished is not None
        assert published.published
        assert not unpublished.published
        store.comments.add(1, 5, 0, CommentKind.TASK, "ai", TaskDetails("t", None, 2))
        assert store.comments.open_tasks("PROJ", "repo", 1)[0].details.line == 2
        summary = store.comments.latest_summary("PROJ", "repo", 1)
        assert summary is not None
        assert summary.comment_id == 4
        store.comments.update(summary.id, 3, "table")
        refreshed = store.comments.latest_summary("PROJ", "repo", 1)
        assert refreshed is not None
        assert refreshed.version == 3
        assert refreshed.details.text == "table"


def test_dismissed_tasks(store: Store) -> None:
    run_id = store.runs.create(_new())
    first = store.comments.add(run_id, 5, 0, CommentKind.TASK, "ai", TaskDetails("a"))
    second = store.comments.add(run_id, 6, 0, CommentKind.TASK, "ai", TaskDetails("b"))
    store.comments.mark_dismissed(first)
    store.comments.mark_resolved(second)
    assert [
        record.comment_id for record in store.comments.dismissed_tasks("PROJ", "repo", 1, "ai")
    ] == [5]
    assert store.comments.open_tasks("PROJ", "repo", 1) == []
    store.comments.mark_open(first)
    assert store.comments.dismissed_tasks("PROJ", "repo", 1, "ai") == []
    assert [record.comment_id for record in store.comments.open_tasks("PROJ", "repo", 1)] == [5]


def test_watch_runs_queries(store: Store) -> None:
    assert store.watch.last_published("PROJ", "repo", 1) is None
    first = store.runs.create(_new(commit="one"))
    store.runs.finish(first, RunStatus.ISSUES)
    store.runs.mark_published(first)
    second = store.runs.create(_new(commit="two"))
    active = store.watch.active_run("PROJ", "repo", 1)
    assert active is not None
    assert active.id == second
    store.runs.finish(second, RunStatus.ERROR)
    assert store.watch.active_run("PROJ", "repo", 1) is None
    latest = store.watch.latest_on_commit("PROJ", "repo", 1, "two")
    assert latest is not None
    assert (latest.id, latest.status) == (second, RunStatus.ERROR)
    assert store.watch.latest_on_commit("PROJ", "repo", 1, "three") is None
    published = store.watch.last_published("PROJ", "repo", 1)
    assert published is not None
    assert published.id == first
    assert store.watch.is_published("PROJ", "repo", 1, "one")
    assert not store.watch.is_published("PROJ", "repo", 1, "two")


def test_watch_record_roundtrip(store: Store) -> None:
    run_id = store.runs.create(_new())
    other = store.runs.create(_new(commit="other"))
    fresh = store.watch.get("PROJ", "repo", 1, run_id)
    assert fresh == TaskWatchRecord("PROJ", "repo", 1, run_id)
    moment = utc_now()
    saved = replace(
        fresh,
        task_counts="1/2",
        checked_at=moment,
        clear_since=moment,
        settled_status="APPROVED",
    )
    store.watch.save(saved)
    assert store.watch.get("PROJ", "repo", 1, run_id) == saved
    assert store.watch.get("PROJ", "repo", 1, other) == TaskWatchRecord("PROJ", "repo", 1, other)
