"""Статусный комментарий PR: один на PR, обновляется по ходу каждого прогона.

Сразу после взятия PR в работу — «проверки начаты», после обычных сценариев — их итоги,
после ИИ-сценариев — итог прогона. Новый коммит PR обновляет тот же комментарий.
"""

import contextlib
from collections.abc import Sequence
from http import HTTPStatus

from gigareview.bitbucket import BitbucketApi, Comment, PullRequest
from gigareview.config import Scenario
from gigareview.errors import BitbucketError
from gigareview.scenarios import ScenarioReport, crash_text, summary_text
from gigareview.store import CommentKind, RunRecord, Store

__all__ = ["StatusBoard"]


class StatusBoard:
    """Статусный комментарий прогона; в сухом режиме (``enabled=False``) ничего не пишет."""

    def __init__(
        self,
        api: BitbucketApi,
        store: Store,
        run: RunRecord,
        run_url: str,
        *,
        enabled: bool = True,
        approve: bool = True,
    ) -> None:
        """Комментарий прогона ``run`` со ссылкой ``run_url``; ``approve`` — как в итоге."""
        self._api = api
        self._store = store
        self._run = run
        self._run_url = run_url
        self._enabled = enabled
        self._approve = approve
        self._record_id: int | None = None
        self._comment: Comment | None = None

    async def show(
        self,
        pr: PullRequest,
        reports: Sequence[ScenarioReport],
        pending: Sequence[Scenario] = (),
    ) -> None:
        """Итоги сценариев ``reports``; ``pending`` — ещё не выполненные сценарии."""
        text = summary_text(
            reports,
            commit=pr.commit,
            run_url=self._run_url,
            pending=pending,
            approve=self._approve,
        )
        await self._write(pr, text)

    async def fail(self, pr: PullRequest) -> None:
        """Прогон упал: комментарий, уже показанный в PR, больше не говорит «идут проверки».

        Ошибка Bitbucket здесь не важна — причину падения прогона уже знает пайплайн.
        """
        if self._comment is None:
            return
        with contextlib.suppress(BitbucketError):
            await self._write(pr, crash_text(commit=pr.commit, run_url=self._run_url))

    async def _write(self, pr: PullRequest, text: str) -> None:
        """Первая запись прогона обновляет комментарий PR прошлых прогонов или создаёт его."""
        if not self._enabled:
            return
        if self._comment is not None and self._record_id is not None:
            self._comment = await self._api.update_comment(
                pr,
                self._comment.id,
                self._comment.version,
                text,
            )
            self._store.comments.set_version(self._record_id, self._comment.version)
            return
        self._comment = await self._first_write(pr, text)
        self._record_id = self._store.comments.add(
            self._run.id,
            self._comment.id,
            self._comment.version,
            CommentKind.SUMMARY,
        )

    async def _first_write(self, pr: PullRequest, text: str) -> Comment:
        """Обновляет комментарий прошлого прогона PR; удалён вручную — пишет новый."""
        previous = self._store.comments.latest_summary(pr.project, pr.repo, pr.id)
        if previous is not None:
            try:
                return await self._api.update_comment(
                    pr,
                    previous.comment_id,
                    previous.version,
                    text,
                )
            except BitbucketError as error:
                if error.status_code != HTTPStatus.NOT_FOUND:
                    raise
        return await self._api.add_comment(pr, text)
