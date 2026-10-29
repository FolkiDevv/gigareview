"""Публикация итогов прогона в Bitbucket: таски сценариев и статус ревью."""

from collections.abc import Callable, Sequence
from http import HTTPStatus

from gigareview.bitbucket import Anchor, BitbucketApi, PullRequest
from gigareview.errors import BitbucketError
from gigareview.pipeline.tracking import TaskStates
from gigareview.scenarios import ScenarioReport, task_text
from gigareview.statuses import EventLevel, Outcome, ReviewStatus, RunStatus
from gigareview.store import CommentKind, Store, TaskDetails

__all__ = ["Publisher", "review_decision", "withdraw_approval"]


def review_decision(
    status: RunStatus,
    current: str | None,
    *,
    approve: bool,
) -> ReviewStatus | None:
    """Какой статус ревью поставить по итогу ``status`` при текущем ``current``.

    Замечания — NEEDS_WORK; без замечаний — APPROVED, а без ``approve`` снимается только
    своё NEEDS_WORK; часть проверок не удалась — статус не меняется. ``None`` — менять
    нечего (в том числе статус уже такой: лишний запрос не нужен).
    """
    target: ReviewStatus | None = None
    if status is RunStatus.ISSUES:
        target = ReviewStatus.NEEDS_WORK
    elif status is RunStatus.PASSED and approve:
        target = ReviewStatus.APPROVED
    elif status is RunStatus.PASSED and current == ReviewStatus.NEEDS_WORK:
        target = ReviewStatus.UNAPPROVED
    return None if target == current else target


async def withdraw_approval(api: BitbucketApi, store: Store, pr: PullRequest) -> bool:
    """Новый коммит PR: одобрение снимается до конца его проверки.

    Одобрение относится к проверенному коммиту. Коммит, итоги которого уже опубликованы
    (ручной повтор), одобрения не теряет. ``pr`` мог устареть, пока прогон ждал очереди:
    перед записью статус проверяется по свежему PR (запрос — только если есть что снимать).
    """
    if store.watch.is_published(pr.project, pr.repo, pr.id, pr.commit):
        return False
    slug = await api.current_slug()
    if pr.reviewer_status(slug) != ReviewStatus.APPROVED:
        return False
    fresh = await api.get_pull_request(pr.project, pr.repo, pr.id)
    if fresh.state != "OPEN" or fresh.reviewer_status(slug) != ReviewStatus.APPROVED:
        return False
    await api.set_review_status(fresh, ReviewStatus.UNAPPROVED)
    return True


class Publisher:
    """Пишет результаты одного прогона в PR и запоминает опубликованное в базе."""

    def __init__(self, api: BitbucketApi, store: Store, run_id: int, states: TaskStates) -> None:
        """Публикация прогона ``run_id`` через ``api``; ``states`` — свежий снимок тасок PR."""
        self._api = api
        self._store = store
        self._run_id = run_id
        self._states = states

    async def publish_tasks(self, pr: PullRequest, reports: Sequence[ScenarioReport]) -> None:
        """Таски сценариев ``reports``: их прошлые таски закрываются, новые публикуются."""
        names = {report.scenario.name for report in reports}
        await self._resolve_previous(pr, reports, lambda scenario: scenario in names)
        for report in reports:
            if report.outcome is Outcome.ISSUES:
                # Таски публикуются по порядку сценариев: так их и читают в PR.
                await self._post_tasks(pr, report)  # noqa: WPS476

    async def finish(
        self,
        pr: PullRequest,
        reports: Sequence[ScenarioReport],
        status: RunStatus,
        *,
        approve: bool = True,
    ) -> None:
        """Все сценарии завершены: закрыть таски убранных сценариев, поставить статус ревью.

        Статус — по :func:`review_decision`: без ``approve`` APPROVED не ставится.
        """
        names = {report.scenario.name for report in reports}
        await self._resolve_previous(pr, (), lambda scenario: scenario not in names)
        current = pr.reviewer_status(await self._api.current_slug())
        review_status = review_decision(status, current, approve=approve)
        if review_status is not None:
            await self._api.set_review_status(pr, review_status)

    async def _post_tasks(self, pr: PullRequest, report: ScenarioReport) -> None:
        """Таски сценария с замечаниями."""
        for task in report.tasks:
            anchor = None if task.path is None else Anchor(task.path, task.line)
            # Таски сценария — по порядку, как он их выдал.
            comment = await self._api.add_comment(  # noqa: WPS476
                pr,
                task_text(report, task),
                task=True,
                anchor=anchor,
            )
            self._store.comments.add(
                self._run_id,
                comment.id,
                comment.version,
                CommentKind.TASK,
                report.scenario.name,
                TaskDetails(task.text, task.path, task.line),
            )

    async def _resolve_previous(
        self,
        pr: PullRequest,
        reports: Sequence[ScenarioReport],
        selected: Callable[[str], bool],
    ) -> None:
        """Закрывает открытые таски сценариев ``selected``; удалённая вручную — забыта.

        Таски инкрементальных сценариев остаются открытыми, кроме тех, что сценарий закрыл
        (``resolved``): он ревьюит только новые изменения и не повторяет прежние замечания.
        Версия таски — из снимка: таску, которую трогали в Bitbucket, не нужно перечитывать.
        """
        incremental = [report for report in reports if report.scenario.incremental]
        kept = {report.scenario.name for report in incremental}
        resolved = {task_id for report in incremental for task_id in report.resolved}
        for record in self._store.comments.open_tasks(pr.project, pr.repo, pr.id):
            if not selected(record.scenario):
                continue
            if record.scenario in kept and record.comment_id not in resolved:
                continue
            try:
                # Немного запросов, а ошибку каждого нужно разобрать отдельно.
                await self._api.resolve_task(  # noqa: WPS476
                    pr,
                    record.comment_id,
                    self._states.versions.get(record.comment_id, record.version),
                )
            except BitbucketError as error:
                if error.status_code != HTTPStatus.NOT_FOUND:
                    self._store.events.add(
                        EventLevel.WARNING,
                        f"Не удалось закрыть таску #{record.comment_id}: {error}",
                        self._run_id,
                    )
                    continue
            self._store.comments.mark_resolved(record.id)
