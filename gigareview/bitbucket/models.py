"""Модели ответов Bitbucket: только поля, которые нужны демону; остальное игнорируется."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Anchor", "Comment", "Participant", "PullRequest", "Ref", "Repository", "User"]


class _ApiModel(BaseModel):
    """Ответ API: имена в camelCase, лишние поля игнорируются."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class Project(_ApiModel):
    """Проект Bitbucket."""

    key: str


class Repository(_ApiModel):
    """Репозиторий Bitbucket."""

    slug: str
    project: Project


class Ref(_ApiModel):
    """Ветка PR: исходная (``fromRef``) или целевая (``toRef``)."""

    id: str
    display_id: str = Field(alias="displayId")
    latest_commit: str = Field(alias="latestCommit")
    repository: Repository


class User(_ApiModel):
    """Пользователь Bitbucket."""

    name: str = ""
    slug: str = ""
    display_name: str = Field(default="", alias="displayName")


class Participant(_ApiModel):
    """Участник PR (автор или ревьюер) и его статус."""

    user: User
    status: str = "UNAPPROVED"


class PullRequest(_ApiModel):
    """Pull request."""

    id: int
    version: int = 0
    title: str = ""
    description: str = ""
    state: str = "OPEN"
    from_ref: Ref = Field(alias="fromRef")
    to_ref: Ref = Field(alias="toRef")
    author: Participant
    reviewers: tuple[Participant, ...] = ()
    links: dict[str, Any] = Field(default_factory=dict)

    @property
    def project(self) -> str:
        """Ключ проекта целевого репозитория."""
        return self.to_ref.repository.project.key

    @property
    def repo(self) -> str:
        """Slug целевого репозитория."""
        return self.to_ref.repository.slug

    @property
    def is_fork(self) -> bool:
        """Исходная ветка лежит в другом репозитории (форке)."""
        return self.from_ref.repository != self.to_ref.repository

    @property
    def commit(self) -> str:
        """Последний коммит исходной ветки."""
        return self.from_ref.latest_commit

    @property
    def url(self) -> str:
        """Ссылка на PR в веб-интерфейсе Bitbucket (пусто, если её нет в ответе)."""
        links = self.links.get("self") or [{}]
        return str(links[0].get("href", ""))

    @property
    def author_name(self) -> str:
        """Отображаемое имя автора."""
        return self.author.user.display_name or self.author.user.name

    def reviewer_status(self, slug: str) -> str | None:
        """Статус ревьюера ``slug`` или ``None``, если он не в ревьюерах."""
        return next(
            (
                reviewer.status
                for reviewer in self.reviewers
                if slug in {reviewer.user.slug, reviewer.user.name}
            ),
            None,
        )


class Comment(_ApiModel):
    """Комментарий PR (таска — комментарий с ``severity = BLOCKER``)."""

    id: int
    version: int = 0
    text: str = ""
    state: str = "OPEN"
    severity: str = "NORMAL"


@dataclass(frozen=True, slots=True)
class Anchor:
    """Привязка комментария к строке файла в диффе PR."""

    path: str
    line: int | None = None
