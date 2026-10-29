"""Наблюдение за PR без нового коммита: последний опубликованный прогон и ход наблюдения."""

import sqlite3
from datetime import datetime

from gigareview.statuses import RunStatus
from gigareview.store.clock import parse_stamp, stamp
from gigareview.store.records import RunRecord, TaskWatchRecord

__all__ = ["WatchRepository"]

_ACTIVE = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
_PR_CONDITION = "project = ? AND repo = ? AND pr_id = ?"


class WatchRepository:
    """Таблица ``task_watch`` и выборки прогонов, нужные наблюдению за закрытыми тасками."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Репозиторий поверх соединения ``db``."""
        self._db = db

    def last_published(self, project: str, repo: str, pr_id: int) -> RunRecord | None:
        """Последний прогон PR, итоги которого опубликованы целиком."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT * FROM runs WHERE {_PR_CONDITION} AND published = 1 "  # noqa: S608
            "ORDER BY id DESC LIMIT 1",
            (project, repo, pr_id),
        ).fetchone()
        return None if row is None else RunRecord.from_row(row)

    def has_active(self, project: str, repo: str, pr_id: int) -> bool:
        """У PR есть прогон в очереди или в работе: статус поставит он."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT 1 FROM runs WHERE {_PR_CONDITION} AND status IN (?, ?) LIMIT 1",  # noqa: S608
            (project, repo, pr_id, *_ACTIVE),
        ).fetchone()
        return row is not None

    def is_published(self, project: str, repo: str, pr_id: int, commit_id: str) -> bool:
        """Итоги проверки коммита PR уже опубликованы каким-то прогоном."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT 1 FROM runs WHERE {_PR_CONDITION} AND commit_id = ? "  # noqa: S608
            "AND published = 1 LIMIT 1",
            (project, repo, pr_id, commit_id),
        ).fetchone()
        return row is not None

    def get(self, project: str, repo: str, pr_id: int, run_id: int) -> TaskWatchRecord:
        """Наблюдение за PR после прогона ``run_id``; после другого прогона — с начала."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT * FROM task_watch WHERE {_PR_CONDITION}",  # noqa: S608
            (project, repo, pr_id),
        ).fetchone()
        if row is None or row["run_id"] != run_id:
            return TaskWatchRecord(project, repo, pr_id, run_id)
        return TaskWatchRecord(
            project=project,
            repo=repo,
            pr_id=pr_id,
            run_id=run_id,
            task_counts=row["task_counts"],
            checked_at=parse_stamp(row["checked_at"]),
            retry_after=parse_stamp(row["retry_after"]),
            clear_since=parse_stamp(row["clear_since"]),
            settled_status=row["settled_status"],
        )

    def save(self, watch: TaskWatchRecord) -> None:
        """Сохраняет ход наблюдения (одна запись на PR)."""
        self._db.execute(
            "INSERT OR REPLACE INTO task_watch (project, repo, pr_id, run_id, task_counts, "
            "checked_at, retry_after, clear_since, settled_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                watch.project,
                watch.repo,
                watch.pr_id,
                watch.run_id,
                watch.task_counts,
                _stamp_or_none(watch.checked_at),
                _stamp_or_none(watch.retry_after),
                _stamp_or_none(watch.clear_since),
                watch.settled_status,
            ),
        )


def _stamp_or_none(moment: datetime | None) -> str | None:
    """Время для базы; ``None`` остаётся ``None``."""
    return None if moment is None else stamp(moment)
