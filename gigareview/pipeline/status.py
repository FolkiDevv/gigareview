"""Статусный комментарий PR: один на PR, обновляется по ходу каждого прогона.

Сразу после взятия PR в работу — «проверки начаты», после обычных сценариев — их итоги,
после ИИ-сценариев — итог прогона. Новый коммит PR обновляет тот же комментарий.
"""

import contextlib
from collections.abc import Callable, Sequence
from http import HTTPStatus

from gigareview.bitbucket import BitbucketApi, Comment, PullRequest
from gigareview.config import DaemonSettings, Scenario
from gigareview.errors import BitbucketError
from gigareview.scenarios import ScenarioReport, crash_text, summary_table, summary_text
from gigareview.store import CommentKind, RunRecord, Store, TaskDetails

__all__ = ["StatusBoard", "rewrite_summary"]


async def rewrite_summary(
    api: BitbucketApi,
    store: Store,
    pr: PullRequest,
    run_id: int,
    render: Callable[[str], str],
) -> bool:
    """Новый итог статусного комментария прогона ``run_id``: ``render`` получает его таблицу.

    Комментарий уже переписал другой прогон или его удалили вручную — он не трогается.
    """
    record = store.comments.latest_summary(pr.project, pr.repo, pr.id)
    if record is None or record.run_id != run_id:
        return False
    table = record.details.text
    try:
        comment = await api.update_comment(pr, record.comment_id, record.version, render(table))
    except BitbucketError as error:
        if error.status_code == HTTPStatus.NOT_FOUND:
            return False
        raise
    store.comments.update(record.id, comment.version, table)
    return True


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
        policy: DaemonSettings | None = None,
    ) -> None:
        """Комментарий прогона ``run`` со ссылкой ``run_url``; ``policy`` — настройки прогона."""
        self._api = api
        self._store = store
        self._run = run
        self._run_url = run_url
        self._enabled = enabled
        self._policy = policy or DaemonSettings()
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
            policy=self._policy,
        )
        await self._write(pr, text, summary_table(reports, commit=pr.commit, pending=pending))

    async def fail(self, pr: PullRequest) -> None:
        """Прогон упал: комментарий, уже показанный в PR, больше не говорит «идут проверки».

        Ошибка Bitbucket здесь не важна — причину падения прогона уже знает пайплайн.
        """
        if self._comment is None:
            return
        with contextlib.suppress(BitbucketError):
            text = crash_text(commit=pr.commit, run_url=self._run_url, policy=self._policy)
            await self._write(pr, text, "")

    async def _write(self, pr: PullRequest, text: str, table: str) -> None:
        """Первая запись прогона обновляет комментарий PR прошлых прогонов или создаёт его.

        Таблица сценариев запоминается: итог комментария потом переписывает наблюдение.
        """
        if not self._enabled:
            return
        if self._comment is not None and self._record_id is not None:
            self._comment = await self._api.update_comment(
                pr,
                self._comment.id,
                self._comment.version,
                text,
            )
            self._store.comments.update(self._record_id, self._comment.version, table)
            return
        self._comment = await self._first_write(pr, text)
        self._record_id = self._store.comments.add(
            self._run.id,
            self._comment.id,
            self._comment.version,
            CommentKind.SUMMARY,
            details=TaskDetails(table),
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
