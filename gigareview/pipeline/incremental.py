"""Инкрементальное ревью: коммит прошлого ревью сценария, его открытые таски и их судьба."""

from collections.abc import Sequence
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
    """Прошлые ревью PR сценарием: коммит последнего (пусто — ревью с нуля) и его таски.

    ``open_tasks`` — открытые таски, ``dismissed`` — закрытые людьми как ложные срабатывания.
    """

    previous: str
    open_tasks: tuple[CommentRecord, ...]
    dismissed: tuple[CommentRecord, ...] = ()


async def scenario_history(
    store: Store,
    pr: PullRequest,
    scenario: Scenario,
    repo_dir: Path,
) -> History:
    """История сценария в PR; коммит не предок текущего (force-push) — ревью с нуля.

    Состояние тасок — из базы: перед этапом с инкрементальным сценарием и при каждой
    публикации её сверяет снимок Bitbucket (``tracking.fetch_task_states``).
    """
    previous = store.runs.last_reviewed_commit(
        pr.project,
        pr.repo,
        pr.id,
        scenario.name,
        other_than=pr.commit,
    )
    if previous and not await GitRepo(repo_dir, "").is_ancestor(previous, pr.commit):
        previous = None
    key = (pr.project, pr.repo, pr.id, scenario.name)
    return History(
        previous or "",
        tuple(store.comments.open_tasks(*key)),
        tuple(store.comments.dismissed_tasks(*key)),
    )


def history_env(history: History, artifacts: Path, scenario: Scenario) -> dict[str, str]:
    """Переменные сценария: ``GR_PREV_COMMIT`` и файлы тасок.

    ``GR_OPEN_TASKS`` — открытые таски, ``GR_DISMISSED_TASKS`` — закрытые людьми.
    """
    open_path = artifacts / f"{scenario.name}.open-tasks.json"
    dismissed_path = artifacts / f"{scenario.name}.dismissed-tasks.json"
    _write_tasks(open_path, history.open_tasks)
    _write_tasks(dismissed_path, history.dismissed)
    return {
        "GR_PREV_COMMIT": history.previous,
        "GR_OPEN_TASKS": str(open_path),
        "GR_DISMISSED_TASKS": str(dismissed_path),
    }


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


def _write_tasks(path: Path, records: Sequence[CommentRecord]) -> None:
    """Файл тасок сценария: JSON-список ``[{"id", "text", "path", "line"}]``."""
    path.write_text(open_tasks_json([_open_task(record) for record in records]), encoding="utf-8")


def _open_task(record: CommentRecord) -> OpenTask:
    """Открытая таска для сценария."""
    details = record.details
    return OpenTask(id=record.comment_id, text=details.text, path=details.path, line=details.line)
