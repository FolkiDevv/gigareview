"""Подготовка рабочей копии PR: клон, ветка PR, команда init, окружение сценариев."""

import fcntl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from gigareview.bitbucket import PullRequest
from gigareview.config import RepoConfig
from gigareview.errors import SetupError
from gigareview.gitops import GitRepo
from gigareview.process import run_process

__all__ = [
    "INIT_TIMEOUT",
    "RepoLock",
    "Workspace",
    "prepare_workspace",
    "repo_dir",
    "scenario_env",
]

#: Таймаут команды init (``uv sync`` на холодном кеше бывает долгим), секунды.
INIT_TIMEOUT = 1800.0


@dataclass(frozen=True, slots=True)
class Workspace:
    """Готовая копия PR: каталог, merge-base с целевой веткой и журнал подготовки."""

    repo_dir: Path
    merge_base: str
    log: str


class RepoLock:
    """Межпроцессный замок копии репозитория: ``serve`` и ``run`` не делят её одновременно."""

    def __init__(self, home: Path, repo: RepoConfig) -> None:
        """Замок копии ``repo`` (файл в ``home/locks``)."""
        stem = repo.key.lower().replace("/", "-")
        name = f"{stem}.lock"
        self._path = home / "locks" / name
        self._lock_file: IO[str] | None = None

    def __enter__(self) -> Self:
        """Берёт замок; копия занята другим процессом — :class:`SetupError`."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._path.open("a", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise SetupError("Копия репозитория занята другим процессом gigareview") from None
        self._lock_file = lock_file
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Отпускает замок."""
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None


def repo_dir(home: Path, repo: RepoConfig) -> Path:
    """Каталог локальной копии репозитория."""
    return home / "repos" / repo.project.lower() / repo.slug.lower()


async def prepare_workspace(
    home: Path,
    repo: RepoConfig,
    pr: PullRequest,
    env: Mapping[str, str],
) -> Workspace:
    """Переключает копию на коммит PR и выполняет init с окружением ``env``.

    Сбой git — :class:`GitError`, сбой init — :class:`SetupError`.
    """
    git = GitRepo(repo_dir(home, repo), repo.clone_url)
    await git.ensure_clone()
    merge_base = await git.checkout(
        pr.from_ref.display_id,
        pr.to_ref.display_id,
        pr.commit,
        fork_pr=pr.id if pr.is_fork else None,
    )
    log = [git.log]
    if repo.init.strip():
        outcome = await run_process(
            ("bash", "-c", repo.init),
            cwd=git.path,
            env=env,
            time_limit=INIT_TIMEOUT,
        )
        log.append(f"$ {repo.init}\n{outcome.output}")
        if outcome.exit_code != 0:
            reason = "не уложилась в таймаут" if outcome.timed_out else f"код {outcome.exit_code}"
            raise SetupError(f"Команда init `{repo.init}`: {reason}\n{outcome.output}")
    return Workspace(git.path, merge_base, "".join(log))


def scenario_env(pr: PullRequest, workspace: Workspace, artifacts: Path) -> dict[str, str]:
    """Переменные окружения сценария о PR и рабочей копии."""
    return {
        "TARGET_BRANCH": pr.to_ref.display_id,
        "SOURCE_BRANCH": pr.from_ref.display_id,
        "PR_ID": str(pr.id),
        "PR_PROJECT": pr.project,
        "PR_REPO": pr.repo,
        "PR_COMMIT": pr.commit,
        "MERGE_BASE": workspace.merge_base,
        "GR_REPO_DIR": str(workspace.repo_dir),
        "GR_ARTIFACTS": str(artifacts),
    }
