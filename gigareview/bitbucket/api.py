"""Интерфейс Bitbucket, нужный демону: настоящий клиент и подделка в тестах."""

from typing import Protocol

from gigareview.bitbucket.models import Anchor, Comment, PullRequest
from gigareview.statuses import ReviewStatus

__all__ = ["BitbucketApi"]


class BitbucketApi(Protocol):
    """Операции Bitbucket, которыми пользуются демон и пайплайн."""

    async def current_slug(self) -> str:
        """Slug пользователя, от имени которого работает демон."""
        ...

    async def list_review_prs(self) -> list[PullRequest]:
        """Открытые PR со всего сервера, где пользователь — ревьюер."""
        ...

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequest:
        """PR по номеру."""
        ...

    async def add_comment(
        self,
        pr: PullRequest,
        text: str,
        *,
        task: bool = False,
        anchor: Anchor | None = None,
    ) -> Comment:
        """Комментарий к PR; ``task`` — таска (BLOCKER), ``anchor`` — к строке файла."""
        ...

    async def get_comment(self, pr: PullRequest, comment_id: int) -> Comment:
        """Комментарий PR по номеру (таска — с её состоянием)."""
        ...

    async def resolve_task(self, pr: PullRequest, comment_id: int, version: int) -> Comment:
        """Помечает таску выполненной (``RESOLVED``)."""
        ...

    async def update_comment(
        self,
        pr: PullRequest,
        comment_id: int,
        version: int,
        text: str,
    ) -> Comment:
        """Меняет текст комментария (статусный комментарий прогона)."""
        ...

    async def set_review_status(self, pr: PullRequest, status: ReviewStatus) -> None:
        """Ставит статус ревью текущего пользователя."""
        ...
