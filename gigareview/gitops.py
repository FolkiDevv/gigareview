"""Локальные копии репозиториев: клонирование, загрузка ветки PR и переключение на неё."""

from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from gigareview.errors import GitError
from gigareview.process import ProcessOutcome, run_process, subprocess_env

__all__ = ["GIT_TIMEOUT", "GitRepo", "redact_credentials"]

#: Таймаут одной команды git, секунды (клон большого репозитория бывает долгим).
GIT_TIMEOUT = 600.0

_HTTP_SCHEMES = frozenset(("http", "https"))

# git не должен ждать ввода логина или пароля: без терминала он бы завис до таймаута.
_GIT_ENV = MappingProxyType({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"})


class GitRepo:
    """Клон репозитория в ``path``; каждая команда добавляет свой вывод в :attr:`log`."""

    def __init__(self, path: Path, clone_url: str, *, timeout: float = GIT_TIMEOUT) -> None:
        """Копия ``clone_url`` в каталоге ``path`` (клонируется в :meth:`ensure_clone`)."""
        self.path = path
        self._log: list[str] = []
        self._clone_url = clone_url
        self._timeout = timeout

    @property
    def log(self) -> str:
        """Команды git и их вывод по порядку."""
        return "".join(self._log)

    async def ensure_clone(self) -> None:
        """Клонирует репозиторий, если локальной копии ещё нет."""
        if (self.path / ".git").exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await self._git(
            "clone",
            "--no-checkout",
            self._clone_url,
            str(self.path),
            cwd=self.path.parent,
        )

    async def checkout(
        self,
        source_branch: str,
        target_branch: str,
        commit: str,
        *,
        fork_pr: int | None = None,
    ) -> str:
        """Загружает ветки PR и целевую, переключается на ``commit``; возвращает merge-base.

        Локальная ветка ``source_branch`` пересоздаётся на ``commit``, правки и
        неотслеживаемые файлы удаляются; игнорируемые (``.venv``) остаются — init быстрее.
        PR из форка (``fork_pr`` — его номер) берётся из ``refs/pull-requests/<id>/from``
        целевого репозитория: ветки форка в origin нет.
        """
        source = _refspec(source_branch) if fork_pr is None else _pr_refspec(fork_pr)
        await self._git(
            "fetch",
            "--prune",
            "--force",
            "origin",
            source,
            _refspec(target_branch),
        )
        await self._git("switch", "--force", "--no-guess", "-C", source_branch, commit)
        await self._git("clean", "-fd")
        merge_base = await self._git("merge-base", f"origin/{target_branch}", commit)
        return merge_base.output.strip()

    async def _git(self, *args: str, cwd: Path | None = None) -> ProcessOutcome:
        """Команда git; ненулевой код или таймаут — :class:`GitError` с её выводом."""
        joined = redact_credentials(" ".join(args), self._clone_url)
        command = f"git {joined}"
        outcome = await run_process(
            ("git", *args),
            cwd=cwd or self.path,
            env=subprocess_env(_GIT_ENV),
            time_limit=self._timeout,
        )
        output = redact_credentials(outcome.output, self._clone_url)
        self._log.append(f"$ {command}\n{output}")
        if outcome.timed_out:
            raise GitError(f"`{command}` не уложилась в {self._timeout:.0f} с")
        if outcome.exit_code != 0:
            raise GitError(f"`{command}` завершилась с кодом {outcome.exit_code}:\n{output}")
        return outcome


def redact_credentials(text: str, url: str) -> str:
    """Скрывает учётные данные из ``url`` в тексте.

    Пароль скрывается всегда, а у http(s) — и одиночное имя: часто это токен
    (``https://TOKEN@host``). Имя в ``ssh://git@host`` секретом не считается.
    """
    parts = urlsplit(url)
    token_as_user = parts.scheme.lower() in _HTTP_SCHEMES and bool(parts.username)
    if parts.password is None and not token_as_user:
        return text
    userinfo = parts.netloc.rpartition("@")[0]
    return text.replace(f"{userinfo}@", "***@")


def _pr_refspec(pr_id: int) -> str:
    """Refspec исходной ветки PR, которую Bitbucket публикует в целевом репозитории."""
    return f"+refs/pull-requests/{pr_id}/from:refs/remotes/origin/pull-requests/{pr_id}"


def _refspec(branch: str) -> str:
    """Refspec ветки ``branch`` в ``refs/remotes/origin/``."""
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
