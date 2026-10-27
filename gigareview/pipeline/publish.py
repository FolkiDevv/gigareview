"""Публикация итогов прогона в Bitbucket: таски, итоговый комментарий, статус ревью."""

from collections.abc import Sequence
from http import HTTPStatus
from types import MappingProxyType

from gigareview.bitbucket import Anchor, BitbucketApi, PullRequest
from gigareview.errors import BitbucketError
from gigareview.scenarios import ScenarioReport, summary_text, task_text
from gigareview.statuses import EventLevel, Outcome, ReviewStatus, RunStatus
from gigareview.store import CommentKind, Store

__all__ = ["Publisher"]

_REVIEW_STATUSES = MappingProxyType({
    RunStatus.PASSED: ReviewStatus.APPROVED,
    RunStatus.ISSUES: ReviewStatus.NEEDS_WORK,
})


class Publisher:
    """Пишет результаты одного прогона в PR и запоминает опубликованное в базе."""

    def __init__(self, api: BitbucketApi, store: Store, run_id: int) -> None:
        """Публикация прогона ``run_id`` через ``api``."""
        self._api = api
        self._store = store
        self._run_id = run_id

    async def publish(
        self,
        pr: PullRequest,
        reports: Sequence[ScenarioReport],
        status: RunStatus,
        run_url: str,
    ) -> None:
        """Закрывает прошлые таски, публикует новые и итог, ставит статус ревью."""
        # Пользователь нужен для статуса ревью: если его не определить, лучше упасть
        # до публикации, чем оставить таски без статуса и повторить их следующим прогоном.
        await self._api.current_slug()
        await self._resolve_previous(pr)
        for report in reports:
            if report.outcome is Outcome.ISSUES:
                # Таски публикуются по порядку сценариев: так их и читают в PR.
                await self._post_tasks(pr, report)  # noqa: WPS476
        summary = await self._api.add_comment(
            pr,
            summary_text(reports, commit=pr.commit, run_url=run_url),
        )
        self._store.comments.add(self._run_id, summary.id, summary.version, CommentKind.SUMMARY)
        review_status = _REVIEW_STATUSES.get(status)
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
            )

    async def _resolve_previous(self, pr: PullRequest) -> None:
        """Закрывает открытые таски прошлых прогонов; таска, удалённая вручную, — забыта."""
        for record in self._store.comments.open_tasks(pr.project, pr.repo, pr.id):
            try:
                # Немного запросов, а ошибку каждого нужно разобрать отдельно.
                await self._api.resolve_task(  # noqa: WPS476
                    pr,
                    record.comment_id,
                    record.version,
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
