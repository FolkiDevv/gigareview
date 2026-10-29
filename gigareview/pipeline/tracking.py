"""Таски PR в Bitbucket одним запросом: сверка базы и версии открытых тасок."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gigareview.bitbucket import BitbucketApi, Comment, PullRequest
from gigareview.store import Store

__all__ = ["TaskStates", "fetch_task_states"]


@dataclass(frozen=True, slots=True)
class TaskStates:
    """Снимок тасок PR: версии открытых и число ручных тасок пользователя демона.

    ``versions`` — номер открытой таски → её версия в Bitbucket; ``manual`` — открытые таски
    от имени пользователя демона, которые демон не публиковал (их оставил сам пользователь).
    """

    versions: Mapping[int, int]
    manual: int = 0

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

    Без тасок демона в PR запроса нет. ``counts`` — свежие счётчики тасок PR: ноль открытых
    — все таски закрыты, тоже без запроса. Таска, закрытая или удалённая в Bitbucket, —
    ложное срабатывание (её закрыл человек: свои таски демон отмечает сам); открытая заново
    — снова замечание. ``slug`` — пользователь демона: его ручные таски считаются отдельно.
    """
    records = store.comments.tasks(pr.project, pr.repo, pr.id)
    if not records:
        return TaskStates(MappingProxyType({}))
    comments: list[Comment] = []
    if counts is None or counts[0]:
        comments = await api.list_tasks(pr)
    opened = {comment.id: comment for comment in comments if comment.is_open}
    for record in records:
        is_open = record.comment_id in opened
        if is_open and record.resolved:
            store.comments.mark_open(record.id)
        elif not is_open and not record.resolved:
            store.comments.mark_dismissed(record.id)
    known = {known_task.comment_id for known_task in records}
    manual = sum(
        1
        for comment in opened.values()
        if comment.id not in known and slug and slug in {comment.author.slug, comment.author.name}
    )
    versions = {comment.id: comment.version for comment in opened.values()}
    return TaskStates(MappingProxyType(versions), manual)
