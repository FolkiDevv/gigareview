"""Наблюдение за PR без нового коммита: последний опубликованный прогон и ход наблюдения."""

import sqlite3

from gigareview.statuses import RunStatus
from gigareview.store.clock import parse_stamp, stamp_or_none
from gigareview.store.records import RecheckWatchRecord, RunRecord, TaskWatchRecord

__all__ = ["WatchRepository"]

_ACTIVE = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
_PR_CONDITION = "project = ? AND repo = ? AND pr_id = ?"


class WatchRepository:
    """Таблицы ``task_watch`` и ``recheck_watch``; выборки прогонов для наблюдения за PR."""

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

    def active_run(self, project: str, repo: str, pr_id: int) -> RunRecord | None:
        """Прогон PR в очереди или в работе (статус поставит он), если такой есть."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT * FROM runs WHERE {_PR_CONDITION} AND status IN (?, ?) "  # noqa: S608
            "ORDER BY id DESC LIMIT 1",
            (project, repo, pr_id, *_ACTIVE),
        ).fetchone()
        return None if row is None else RunRecord.from_row(row)

    def latest_on_commit(
        self,
        project: str,
        repo: str,
        pr_id: int,
        commit_id: str,
    ) -> RunRecord | None:
        """Последний прогон коммита PR (любой, кроме сухого): для журнала наблюдения."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT * FROM runs WHERE {_PR_CONDITION} AND commit_id = ? "  # noqa: S608
            "AND dry_run = 0 ORDER BY id DESC LIMIT 1",
            (project, repo, pr_id, commit_id),
        ).fetchone()
        return None if row is None else RunRecord.from_row(row)

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

    def hold(self, project: str, repo: str, pr_id: int, run_id: int) -> None:
        """Прогон без замечаний придержан ручной таской: наблюдение следит и за ним."""
        self.save(TaskWatchRecord(project, repo, pr_id, run_id))

    def is_held(self, run_id: int) -> bool:
        """Наблюдение заведено для прогона ``run_id`` (с замечаниями или придержанного)."""
        row = self._db.execute("SELECT 1 FROM task_watch WHERE run_id = ?", (run_id,)).fetchone()
        return row is not None

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
                stamp_or_none(watch.checked_at),
                stamp_or_none(watch.retry_after),
                stamp_or_none(watch.clear_since),
                watch.settled_status,
            ),
        )

    def recheck(self, project: str, repo: str, pr_id: int) -> RecheckWatchRecord:
        """Сверка ответов «/recheck» на статусный комментарий PR; не сверялся — пустая запись."""
        row = self._db.execute(
            # В текст запроса подставляются только условия из кода.
            f"SELECT * FROM recheck_watch WHERE {_PR_CONDITION}",  # noqa: S608
            (project, repo, pr_id),
        ).fetchone()
        if row is None:
            return RecheckWatchRecord(project, repo, pr_id)
        return RecheckWatchRecord(
            project=project,
            repo=repo,
            pr_id=pr_id,
            comment_count=row["comment_count"],
            last_reply_id=row["last_reply_id"],
            checked_at=parse_stamp(row["checked_at"]),
            retry_after=parse_stamp(row["retry_after"]),
        )

    def save_recheck(self, watch: RecheckWatchRecord) -> None:
        """Сохраняет ход сверки ответов «/recheck» (одна запись на PR)."""
        self._db.execute(
            "INSERT OR REPLACE INTO recheck_watch (project, repo, pr_id, comment_count, "
            "last_reply_id, checked_at, retry_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                watch.project,
                watch.repo,
                watch.pr_id,
                watch.comment_count,
                watch.last_reply_id,
                stamp_or_none(watch.checked_at),
                stamp_or_none(watch.retry_after),
            ),
        )
