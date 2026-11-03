import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeBitbucket, Origin, add_reply, make_config, make_pr, with_comments

from gigareview.bitbucket import PullRequest
from gigareview.config import Config
from gigareview.daemon import Daemon
from gigareview.errors import BitbucketError
from gigareview.pipeline import RecheckWatch, Reviewer, new_run
from gigareview.pipeline import recheck as recheck_module
from gigareview.pipeline.reuse import reused_reports
from gigareview.statuses import EventLevel, Outcome, ReviewStatus, RunStatus, Trigger
from gigareview.store import RunFilter, ScenarioRecord, Store

_TASKS = '{"tasks": [{"text": "one"}]}'


class Harness:
    """Прогон с упавшим ИИ-сценарием и сверка ответов «/recheck» на его статусный комментарий."""

    def __init__(self, tmp_path: Path, store: Store, origin: Origin, **daemon: Any) -> None:
        self.flag = tmp_path / "ai-works"
        self.lint_log = tmp_path / "lint.log"
        self.store = store
        self.config = self.make_config(tmp_path, origin, **daemon)
        self.api = FakeBitbucket(prs=[with_comments(make_pr(commit=origin.feature), 1)])
        self.queued: list[int] = []

    def make_config(self, tmp_path: Path, origin: Origin, **daemon: Any) -> Config:
        ai_run = f"if [ -f {self.flag} ]; then echo '{_TASKS}'; exit 1; else exit 2; fi"
        scenarios: dict[str, Any] = {
            "lint": {"run": f"echo run >> {self.lint_log}"},
            "ai": {"kind": "ai", "run": ai_run},
        }
        return make_config(tmp_path / "home", origin.url, scenarios=scenarios, daemon_extra=daemon)

    @property
    def pr(self) -> PullRequest:
        return self.api.prs[0]

    @property
    def summary_id(self) -> int:
        return self.api.summaries[0].id

    def review(self, trigger: Trigger = Trigger.AUTO, base_run_id: int | None = None) -> int:
        fingerprint = self.config.fingerprint(self.config.repos[0])
        run = new_run(
            self.pr,
            trigger,
            fingerprint=fingerprint,
            dry_run=False,
            base_run_id=base_run_id,
        )
        run_id = self.store.runs.create(run)
        asyncio.run(Reviewer(self.config, self.store, self.api).review(run_id, self.pr))
        return run_id

    def enqueue(self, pr: PullRequest, base_run_id: int) -> int:
        fingerprint = self.config.fingerprint(self.config.repos[0])
        run = new_run(
            pr,
            Trigger.RECHECK,
            fingerprint=fingerprint,
            dry_run=False,
            base_run_id=base_run_id,
        )
        run_id = self.store.runs.create(run)
        self.queued.append(run_id)
        return run_id

    def reply(self, text: str = "/recheck", **fields: Any) -> int:
        reply_id = add_reply(self.api, self.summary_id, text, **fields)
        count = self.pr.comment_count or 0
        self.api.prs = [with_comments(self.pr, count + 1)]
        return reply_id

    def check(self) -> None:
        watch = RecheckWatch(self.store, self.api)
        asyncio.run(watch.check([self.pr], self.config, self.enqueue))

    def finish_queued(self) -> None:
        run_id = self.queued[-1]
        asyncio.run(Reviewer(self.config, self.store, self.api).review(run_id, self.pr))


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _notes(store: Store) -> list[str]:
    return [event.message for event in store.events.list(EventLevel.NOTE, limit=200)]


@pytest.fixture
def harness(tmp_path: Path, store: Store, origin: Origin) -> Harness:
    reviewed = Harness(tmp_path, store, origin)
    assert store.runs.get(reviewed.review()).status is RunStatus.FAILED  # type: ignore[union-attr]
    return reviewed


def test_recheck_reruns_only_failed_scenarios(harness: Harness, store: Store) -> None:
    hint = harness.api.status_texts[-1]
    assert "Ответьте на этот комментарий `/recheck`" in hint
    assert "осталось перезапусков: 3 из 3" in hint
    reply_id = harness.reply()
    harness.check()
    assert harness.api.reactions == [(reply_id, "thumbsup")]
    assert len(harness.queued) == 1
    run_id = harness.queued[0]
    run = store.runs.get(run_id)
    assert run is not None
    assert run.trigger is Trigger.RECHECK
    assert run.base_run_id == 1
    assert any(
        "/recheck от Author: перезапуск упавших сценариев прогона #1" in note
        for note in _notes(store)
    )
    harness.flag.touch()
    harness.finish_queued()
    assert _lines(harness.lint_log) == 1
    records = store.runs.scenarios(run_id)
    assert [(record.name, record.outcome, record.reused_from) for record in records] == [
        ("lint", Outcome.PASSED, 1),
        ("ai", Outcome.ISSUES, None),
    ]
    assert store.runs.get(run_id).status is RunStatus.ISSUES  # type: ignore[union-attr]
    assert [task.text for task in harness.api.tasks] == ["**gigareview · ai**\n\none"]
    assert harness.api.statuses == [(1, ReviewStatus.NEEDS_WORK)]
    summary = harness.api.status_texts[-1]
    assert "✅ пройден (из прогона #1)" in summary
    assert "/recheck` —" not in summary
    assert len(harness.api.summaries) == 1


def test_reused_tasks_stay_open_and_chain_to_origin(
    tmp_path: Path,
    store: Store,
    origin: Origin,
) -> None:
    reviewed = Harness(tmp_path, store, origin)
    reviewed.flag.touch()
    config = reviewed.config.model_copy(deep=True)
    config.scenarios["broken"] = config.scenarios["lint"].model_copy(
        update={"name": "broken", "run": "exit 2"},
    )
    reviewed.config = config
    base = reviewed.review()
    assert store.runs.get(base).status is RunStatus.ISSUES  # type: ignore[union-attr]
    tasks = [task.id for task in reviewed.api.tasks]
    first = reviewed.review(Trigger.RECHECK, base)
    second = reviewed.review(Trigger.RECHECK, first)
    assert not reviewed.api.resolved
    assert [task.id for task in reviewed.api.tasks] == tasks
    reused = {record.name: record.reused_from for record in store.runs.scenarios(second)}
    assert reused == {"lint": base, "ai": base, "broken": None}
    assert store.runs.count_rechecks("PROJ", "repo", 1, reviewed.pr.commit) == 2


def test_changed_configuration_reruns_everything(harness: Harness, store: Store) -> None:
    harness.config = harness.config.model_copy(
        update={"daemon": harness.config.daemon.model_copy(update={"approve": False})},
    )
    lint = harness.config.scenarios["lint"]
    harness.config.scenarios["lint"] = lint.model_copy(update={"run": f"{lint.run}; true"})
    run_id = harness.review(Trigger.RECHECK, 1)
    assert _lines(harness.lint_log) == 2
    assert all(record.reused_from is None for record in store.runs.scenarios(run_id))


@pytest.mark.parametrize(
    ("author", "text"),
    [("stranger", "/recheck"), ("me", "/recheck"), ("author", "спасибо"), ("author", "")],
)
def test_other_replies_are_ignored(harness: Harness, author: str, text: str) -> None:
    harness.reply(text, author=author)
    harness.check()
    assert not harness.queued
    assert not harness.api.reactions


def test_reviewer_may_recheck_and_command_case_is_ignored(harness: Harness) -> None:
    reviewers = [
        {"user": {"name": "me", "slug": "me"}},
        {"user": {"name": "colleague", "slug": "colleague"}},
    ]
    dumped = harness.pr.model_dump(by_alias=True)
    harness.api.prs = [PullRequest.model_validate({**dumped, "reviewers": reviewers})]
    harness.reply("/ReCheck please", author="colleague")
    harness.check()
    assert len(harness.queued) == 1


def test_command_is_answered_once(harness: Harness) -> None:
    harness.reply()
    harness.check()
    harness.check()
    harness.api.prs = [with_comments(harness.pr, 10)]
    harness.check()
    assert len(harness.queued) == 1
    assert len(harness.api.reactions) == 1


def test_unchanged_comment_count_makes_no_requests(harness: Harness) -> None:
    harness.check()
    harness.api.calls.clear()
    harness.check()
    assert "get_comment" not in harness.api.calls


def test_active_run_rejects_command(harness: Harness, store: Store) -> None:
    harness.reply()
    harness.reply()
    harness.check()
    assert [emoticon for _, emoticon in harness.api.reactions] == ["thumbsup", "thumbsdown"]
    assert len(harness.queued) == 1
    assert any(f"проверка уже идёт (прогон #{harness.queued[0]})" in note for note in _notes(store))


def test_limit_rejects_command(tmp_path: Path, store: Store, origin: Origin) -> None:
    reviewed = Harness(tmp_path, store, origin, max_rechecks=1)
    reviewed.review()
    reviewed.reply()
    reviewed.check()
    reviewed.finish_queued()
    assert "лимит перезапусков упавших сценариев для этого коммита исчерпан (1)" in (
        reviewed.api.status_texts[-1].casefold()
    )
    reviewed.reply()
    reviewed.check()
    assert len(reviewed.queued) == 1
    assert reviewed.api.reactions[-1][1] == "thumbsdown"
    assert any("лимит перезапусков коммита исчерпан (1)" in note for note in _notes(store))


def test_nothing_failed_rejects_command(tmp_path: Path, store: Store, origin: Origin) -> None:
    reviewed = Harness(tmp_path, store, origin)
    reviewed.flag.touch()
    reviewed.review()
    reviewed.reply()
    reviewed.check()
    assert not reviewed.queued
    assert reviewed.api.reactions[-1][1] == "thumbsdown"
    assert any("в прогоне #1 нет упавших сценариев" in note for note in _notes(store))


def test_new_commit_without_runs_rejects_command(harness: Harness, store: Store) -> None:
    harness.api.prs = [
        with_comments(make_pr(commit="new"), harness.pr.comment_count or 0),
    ]
    harness.reply()
    harness.check()
    assert harness.api.reactions[-1][1] == "thumbsdown"
    assert any("текущий коммит ещё не проверялся" in note for note in _notes(store))


def test_crashed_run_is_rerun_fully(harness: Harness, store: Store) -> None:
    crashed = harness.store.runs.create(
        new_run(harness.pr, Trigger.AUTO, fingerprint="fp", dry_run=False),
    )
    store.runs.finish(crashed, RunStatus.ERROR, "git упал")
    harness.reply()
    harness.check()
    run = store.runs.get(harness.queued[0])
    assert run is not None
    assert run.base_run_id == crashed
    harness.finish_queued()
    assert _lines(harness.lint_log) == 2


def test_command_older_than_latest_run_is_skipped(harness: Harness) -> None:
    harness.reply(created=datetime(2020, 1, 1, tzinfo=UTC).timestamp())
    harness.check()
    assert not harness.queued
    assert not harness.api.reactions


def test_reaction_failure_is_only_warned(harness: Harness, store: Store) -> None:
    harness.api.reaction_error = BitbucketError("no plugin", status_code=404)
    harness.reply()
    harness.check()
    assert len(harness.queued) == 1
    warnings = [event.message for event in store.events.list(EventLevel.WARNING)]
    assert any("не удалось поставить реакцию" in warning for warning in warnings)


def test_deleted_summary_is_forgotten(harness: Harness, store: Store) -> None:
    harness.reply()
    harness.api.deleted.add(harness.summary_id)
    harness.check()
    assert not harness.queued
    assert store.watch.recheck("PROJ", "repo", 1).comment_count == harness.pr.comment_count


def test_bitbucket_error_postpones_check(
    harness: Harness,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(recheck_module, "utc_now", lambda: now)
    harness.reply()
    harness.api.comment_error = BitbucketError("down", status_code=500)
    harness.check()
    assert store.watch.recheck("PROJ", "repo", 1).retry_after == now + timedelta(minutes=5)
    harness.api.comment_error = None
    harness.check()
    assert not harness.queued
    now += timedelta(minutes=5)
    harness.check()
    assert len(harness.queued) == 1


def test_without_comment_count_checks_on_schedule(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(recheck_module, "utc_now", lambda: now)
    harness.api.prs = [make_pr(commit=harness.pr.commit)]
    harness.check()
    add_reply(harness.api, harness.summary_id, "/recheck")
    harness.api.calls.clear()
    harness.check()
    assert "get_comment" not in harness.api.calls
    now += timedelta(minutes=5)
    harness.check()
    assert len(harness.queued) == 1


@pytest.mark.parametrize("daemon", [{"dry_run": True}, {"max_rechecks": 0}])
def test_disabled_recheck_does_nothing(
    tmp_path: Path,
    store: Store,
    origin: Origin,
    daemon: dict[str, Any],
) -> None:
    reviewed = Harness(tmp_path, store, origin)
    reviewed.review()
    reviewed.config = reviewed.make_config(tmp_path, origin, **daemon)
    reviewed.reply()
    reviewed.api.calls.clear()
    reviewed.check()
    assert not reviewed.queued
    assert "get_comment" not in reviewed.api.calls


def test_daemon_poll_enqueues_recheck(harness: Harness, store: Store) -> None:
    harness.reply()
    daemon = Daemon(harness.config, store, harness.api)
    assert asyncio.run(daemon.poll()) == 0
    runs = store.queries.runs(RunFilter())
    assert [(run.trigger, run.status, run.base_run_id) for run in runs] == [
        (Trigger.RECHECK, RunStatus.QUEUED, 1),
        (Trigger.AUTO, RunStatus.FAILED, None),
    ]
    assert daemon.status().queue_size == 1


def test_base_run_of_other_commit_is_not_reused(harness: Harness, store: Store) -> None:
    fingerprint = harness.config.fingerprint(harness.config.repos[0])
    pr = make_pr(commit="other")
    run = new_run(pr, Trigger.MANUAL, fingerprint=fingerprint, dry_run=False, base_run_id=1)
    record = store.runs.get(store.runs.create(run))
    assert record is not None
    scenarios = harness.config.scenarios_for(harness.config.repos[0])
    assert reused_reports(store, record, scenarios, fingerprint=fingerprint) == ()
    same = replace(run, commit_id=harness.pr.commit)
    same_record = store.runs.get(store.runs.create(same))
    assert same_record is not None
    reused = reused_reports(store, same_record, scenarios, fingerprint=fingerprint)
    assert [report.scenario.name for report in reused] == ["lint"]


def test_partial_review_is_not_reused(harness: Harness, store: Store) -> None:
    pr = harness.pr
    base = store.runs.create(new_run(pr, Trigger.AUTO, fingerprint="fp", dry_run=False))
    for position, (name, outcome, partial) in enumerate((
        ("lint", Outcome.PASSED, False),
        ("ai", Outcome.ISSUES, True),
    )):
        record = ScenarioRecord(name, name, "simple", outcome, 0, "", 1.0, partial=partial)
        store.runs.add_scenario(base, position, record)
    store.runs.mark_published(base)
    run = store.runs.get(
        store.runs.create(
            new_run(pr, Trigger.RECHECK, fingerprint="fp", dry_run=False, base_run_id=base),
        ),
    )
    assert run is not None
    scenarios = harness.config.scenarios_for(harness.config.repos[0])
    reused = reused_reports(store, run, scenarios, fingerprint="fp")
    assert [report.scenario.name for report in reused] == ["lint"]


def test_config_reloaded_while_queued_reruns_everything(harness: Harness, store: Store) -> None:
    harness.reply()
    harness.check()
    lint = harness.config.scenarios["lint"]
    harness.config.scenarios["lint"] = lint.model_copy(update={"run": f"{lint.run}; true"})
    harness.finish_queued()
    assert _lines(harness.lint_log) == 2
    assert all(record.reused_from is None for record in store.runs.scenarios(harness.queued[0]))
