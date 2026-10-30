"""Таски PR в Bitbucket одним запросом: сверка базы и версии открытых тасок."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gigareview.bitbucket import BitbucketApi, Comment, PullRequest
from gigareview.store import Store

__all__ = ["TaskStates", "fetch_task_states"]


@dataclass(frozen=True, slots=True)
class TaskStates:
    """Снимок тасок PR: версии открытых и ручные таски пользователя демона.

    ``versions`` — номер открытой таски → её версия в Bitbucket; ``manual`` — номера открытых
    тасок от имени пользователя демона, которые демон не публиковал (их оставил сам
    пользователь); ``dismissed`` и ``reopened`` — номера тасок демона, которые эта сверка
    отметила закрытыми людьми и открытыми заново.
    """

    versions: Mapping[int, int]
    manual: tuple[int, ...] = ()
    dismissed: tuple[int, ...] = ()
    reopened: tuple[int, ...] = ()

    def is_open(self, comment_id: int) -> bool:
        """Таска открыта в Bitbucket."""
        return comment_id in self.versions


async def fetch_task_states(
    api: BitbucketApi,
    store: Store,
    pr: PullRequest,
    *,
    counts: tuple[int, int] | None = None,
    slug: str = "",
) -> TaskStates:
    """Снимок тасок PR; база принимает их состояние оттуда.

    Без тасок демона в PR (и без ``slug``) запроса нет. ``counts`` — свежие счётчики
    тасок PR: ноль открытых — все таски закрыты, тоже без запроса. Таска, закрытая или
    удалённая в Bitbucket, — ложное срабатывание (её закрыл человек: свои таски демон
    отмечает сам); открытая заново — снова замечание. ``slug`` — пользователь демона:
    его ручные таски считаются отдельно.
    """
    records = store.comments.tasks(pr.project, pr.repo, pr.id)
    if not records and not slug:
        return TaskStates(MappingProxyType({}))
    comments: list[Comment] = []
    if counts is None or counts[0]:
        comments = await api.list_tasks(pr)
    opened = {comment.id: comment for comment in comments if comment.is_open}
    reopened = [record for record in records if record.resolved and record.comment_id in opened]
    dismissed = [
        record for record in records if not record.resolved and record.comment_id not in opened
    ]
    for record in reopened:
        store.comments.mark_open(record.id)
    for gone in dismissed:
        store.comments.mark_dismissed(gone.id)
    known = {known_task.comment_id for known_task in records}
    manual = tuple(
        comment.id
        for comment in opened.values()
        if comment.id not in known and comment.author.matches(slug)
    )
    return TaskStates(
        MappingProxyType({comment.id: comment.version for comment in opened.values()}),
        manual,
        dismissed=tuple(task.comment_id for task in dismissed),
        reopened=tuple(task.comment_id for task in reopened),
    )
