"""Инкрементальное ревью: коммит прошлого ревью сценария, его открытые таски и их судьба."""

from dataclasses import dataclass, replace
from http import HTTPStatus
from pathlib import Path

from gigareview.bitbucket import BitbucketApi, PullRequest
from gigareview.config import Scenario
from gigareview.errors import BitbucketError
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
    api: BitbucketApi,
    store: Store,
    pr: PullRequest,
    scenario: Scenario,
    repo_dir: Path,
) -> History:
    """История сценария в PR; коммит не предок текущего (force-push) — ревью с нуля.

    Таски сверяются с Bitbucket: закрытые или удалённые вручную в историю не входят. У
    инкрементального сценария сверяются и закрытые: открытая заново таска снова в истории.
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
    records = (store.comments.tasks if scenario.incremental else store.comments.open_tasks)(
        pr.project,
        pr.repo,
        pr.id,
        scenario.name,
    )
    tasks = [
        record
        for record in records
        # Немного запросов, а ответ каждого нужно разобрать отдельно.
        if await _still_open(api, store, pr, record)  # noqa: WPS476
    ]
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


async def _still_open(
    api: BitbucketApi,
    store: Store,
    pr: PullRequest,
    record: CommentRecord,
) -> bool:
    """Таска открыта в Bitbucket; база принимает её состояние оттуда.

    Пользователь закрывает таски, с которыми не согласен: их нельзя держать за замечания.
    Открытая им заново таска — снова замечание. Удалённая таска закрыта навсегда.
    """
    try:
        comment = await api.get_comment(pr, record.comment_id)
    except BitbucketError as error:
        if error.status_code != HTTPStatus.NOT_FOUND:
            raise
        is_open = False
    else:
        is_open = comment.state != "RESOLVED"
    if is_open and record.resolved:
        store.comments.mark_open(record.id)
    elif not is_open and not record.resolved:
        store.comments.mark_resolved(record.id)
    return is_open


def _open_task(record: CommentRecord) -> OpenTask:
    """Открытая таска для сценария."""
    details = record.details
    return OpenTask(id=record.comment_id, text=details.text, path=details.path, line=details.line)
