"""Подделки для тестов: Bitbucket в памяти, PR, git-репозитории, конфигурация."""

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Self

from gigareview.bitbucket import Anchor, Comment, PullRequest
from gigareview.config import Config
from gigareview.errors import BitbucketError
from gigareview.process import run_process
from gigareview.statuses import ReviewStatus

GIT_IDENTITY = MappingProxyType({
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
})


def make_pr(
    pr_id: int = 1,
    *,
    commit: str = "c0ffee",
    project: str = "PROJ",
    repo: str = "repo",
    source: str = "feature",
    target: str = "master",
    state: str = "OPEN",
) -> PullRequest:
    repository = {"slug": repo, "project": {"key": project}}
    return PullRequest.model_validate({
        "id": pr_id,
        "version": 3,
        "title": f"PR {pr_id}",
        "state": state,
        "fromRef": {
            "id": f"refs/heads/{source}",
            "displayId": source,
            "latestCommit": commit,
            "repository": repository,
        },
        "toRef": {
            "id": f"refs/heads/{target}",
            "displayId": target,
            "latestCommit": "base",
            "repository": repository,
        },
        "author": {"user": {"name": "author", "displayName": "Автор"}},
        "reviewers": [{"user": {"name": "me", "slug": "me"}, "status": "UNAPPROVED"}],
        "links": {"self": [{"href": f"https://bb.example.com/pr/{pr_id}"}]},
    })


@dataclass
class PostedComment:
    pr_id: int
    text: str
    task: bool
    anchor: Anchor | None
    id: int


@dataclass
class FakeBitbucket:
    """Bitbucket в памяти: PR задаются тестом, записи — в списках."""

    prs: list[PullRequest] = field(default_factory=list)
    comments: list[PostedComment] = field(default_factory=list)
    resolved: list[int] = field(default_factory=list)
    statuses: list[tuple[int, ReviewStatus]] = field(default_factory=list)
    slug_error: BitbucketError | None = None
    list_error: Exception | None = None
    get_error: Exception | None = None
    resolve_error: BitbucketError | None = None
    overrides: dict[int, PullRequest] = field(default_factory=dict)
    closed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    async def current_slug(self) -> str:
        if self.slug_error is not None:
            raise self.slug_error
        return "me"

    async def list_review_prs(self) -> list[PullRequest]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.prs)

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> PullRequest:
        if self.get_error is not None:
            raise self.get_error
        override = self.overrides.get(pr_id)
        if override is not None:
            return override
        for pr in self.prs:
            if pr.id == pr_id and pr.project == project and pr.repo == repo:
                return pr
        raise BitbucketError("not found", status_code=404)

    async def add_comment(
        self,
        pr: PullRequest,
        text: str,
        *,
        task: bool = False,
        anchor: Anchor | None = None,
    ) -> Comment:
        comment_id = 100 + len(self.comments)
        self.comments.append(PostedComment(pr.id, text, task, anchor, comment_id))
        return Comment(id=comment_id, version=0, text=text)

    async def resolve_task(self, pr: PullRequest, comment_id: int, version: int) -> Comment:
        if self.resolve_error is not None:
            raise self.resolve_error
        self.resolved.append(comment_id)
        return Comment(id=comment_id, version=version + 1, state="RESOLVED")

    async def set_review_status(self, pr: PullRequest, status: ReviewStatus) -> None:
        self.statuses.append((pr.id, status))

    @property
    def tasks(self) -> list[PostedComment]:
        return [comment for comment in self.comments if comment.task]

    @property
    def summaries(self) -> list[PostedComment]:
        return [comment for comment in self.comments if not comment.task]


def git(*args: str, cwd: Path) -> str:
    """Синхронная команда git для подготовки тестовых репозиториев."""
    env = {**os.environ, **GIT_IDENTITY}
    outcome = asyncio.run(run_process(("git", *args), cwd=cwd, env=env, time_limit=60))
    assert outcome.exit_code == 0, outcome.output
    return outcome.output.strip()


@dataclass(frozen=True)
class Origin:
    """Голый origin с веткой master и веткой feature поверх неё."""

    url: str
    work: Path
    master: str
    feature: str


def make_origin(root: Path) -> Origin:
    work = root / "work"
    work.mkdir(parents=True)
    git("init", "-q", "-b", "master", cwd=work)
    (work / ".gitignore").write_text(".venv/\n")
    (work / "app.py").write_text("print('master')\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "master", cwd=work)
    master = git("rev-parse", "HEAD", cwd=work)
    git("switch", "-q", "-c", "feature", cwd=work)
    (work / "app.py").write_text("print('feature')\n")
    git("commit", "-q", "-am", "feature", cwd=work)
    feature = git("rev-parse", "HEAD", cwd=work)
    origin = root / "origin.git"
    git("clone", "-q", "--bare", str(work), str(origin), cwd=root)
    return Origin(str(origin), work, master, feature)


def make_config(
    home: Path,
    origin_url: str,
    *,
    scenarios: dict[str, Any] | None = None,
    repo_extra: dict[str, Any] | None = None,
    daemon_extra: dict[str, Any] | None = None,
) -> Config:
    repo = {
        "project": "PROJ",
        "slug": "repo",
        "clone_url": origin_url,
        **(repo_extra or {}),
    }
    return Config.model_validate({
        "bitbucket": {"url": "https://bb.example.com", "token_env": "GR_TEST_TOKEN"},
        "daemon": {"workdir": str(home), **(daemon_extra or {})},
        "scenarios": {"ok": {"run": "true"}} if scenarios is None else scenarios,
        "repos": [repo],
    })
