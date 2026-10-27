"""Инкрементальное ревью: коммит прошлого ревью сценария, его открытые таски и их судьба."""

from dataclasses import dataclass, replace
from pathlib import Path

from gigareview.bitbucket import PullRequest
from gigareview.config import Scenario
from gigareview.gitops import GitRepo
from gigareview.scenarios import OpenTask, ScenarioReport, open_tasks_json
from gigareview.statuses import Outcome
from gigareview.store import CommentRecord, Store

__all__ = ["History", "history_env", "scenario_history", "settle"]


@dataclass(frozen=True, slots=True)
class History:
    """Прошлые ревью PR сценарием: коммит последнего (пусто — ревью с нуля) и открытые таски."""

    previous: str
    open_tasks: tuple[CommentRecord, ...]


async def scenario_history(
    store: Store,
    pr: PullRequest,
    scenario: Scenario,
    repo_dir: Path,
) -> History:
    """История сценария в PR; коммит не предок текущего (force-push) — ревью с нуля."""
    previous = store.runs.last_reviewed_commit(
        pr.project,
        pr.repo,
        pr.id,
        scenario.name,
        other_than=pr.commit,
    )
    if previous and not await GitRepo(repo_dir, "").is_ancestor(previous, pr.commit):
        previous = None
    tasks = store.comments.open_tasks(pr.project, pr.repo, pr.id, scenario.name)
    return History(previous or "", tuple(tasks))


def history_env(history: History, artifacts: Path, scenario: Scenario) -> dict[str, str]:
    """Переменные сценария: ``GR_PREV_COMMIT`` и файл открытых тасок ``GR_OPEN_TASKS``."""
    path = artifacts / f"{scenario.name}.open-tasks.json"
    tasks = [_open_task(record) for record in history.open_tasks]
    path.write_text(open_tasks_json(tasks), encoding="utf-8")
    return {"GR_PREV_COMMIT": history.previous, "GR_OPEN_TASKS": str(path)}


def settle(report: ScenarioReport, history: History) -> ScenarioReport:
    """Итог инкрементального сценария с его прошлыми тасками.

    Закрытыми считаются только его же открытые таски; упавший сценарий ничего не закрывает.
    Открытые прошлые таски — замечания: PR с ними не одобряется.
    """
    if not report.scenario.incremental:
        return report
    open_ids = {record.comment_id for record in history.open_tasks}
    resolved: tuple[int, ...] = ()
    if report.outcome is not Outcome.ERROR:
        resolved = tuple(task_id for task_id in report.resolved if task_id in open_ids)
    kept = len(open_ids) - len(resolved)
    outcome = report.outcome
    if outcome is Outcome.PASSED and kept:
        outcome = Outcome.ISSUES
    return replace(report, outcome=outcome, resolved=resolved, kept=kept)


def _open_task(record: CommentRecord) -> OpenTask:
    """Открытая таска для сценария."""
    details = record.details
    return OpenTask(id=record.comment_id, text=details.text, path=details.path, line=details.line)
