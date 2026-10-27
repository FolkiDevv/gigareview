from datetime import timedelta
from pathlib import Path

from gigareview.statuses import EventLevel, Outcome, RunStatus, Trigger
from gigareview.store import CommentKind, NewRun, RunFilter, ScenarioRecord, Store
from gigareview.store.clock import utc_now


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
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc")
    interrupted = store.runs.create(_new())
    store.runs.finish(interrupted, RunStatus.INTERRUPTED)
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc")
    for _ in range(2):
        store.runs.finish(store.runs.create(_new()), RunStatus.ERROR)
    assert not store.runs.should_skip("PROJ", "repo", 1, "abc")
    store.runs.finish(store.runs.create(_new()), RunStatus.ERROR)
    assert store.runs.should_skip("PROJ", "repo", 1, "abc")
    store.runs.create(_new(commit="def"))
    assert store.runs.should_skip("PROJ", "repo", 1, "def")
    assert not store.runs.should_skip("PROJ", "repo", 2, "def")


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
    published = store.comments.for_run(run_id)
    assert [(comment.kind, comment.resolved) for comment in published] == [
        (CommentKind.TASK, True),
        (CommentKind.SUMMARY, False),
    ]


def test_events(store: Store) -> None:
    run_id = store.runs.create(_new())
    store.events.add(EventLevel.NOTE, "hello")
    store.events.add(EventLevel.ERROR, "boom", run_id)
    assert store.events.count() == 2
    assert store.events.count(EventLevel.ERROR) == 1
    errors = store.events.list(EventLevel.ERROR)
    assert [(event.message, event.run_id) for event in errors] == [("boom", run_id)]
    assert [event.message for event in store.events.list(limit=1, offset=1)] == ["hello"]


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
