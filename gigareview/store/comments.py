"""Комментарии и таски, которые демон оставил в Bitbucket."""

import sqlite3

from gigareview.store.records import CommentKind, CommentRecord

__all__ = ["CommentRepository"]


class CommentRepository:
    """Таблица ``bb_comments``: что опубликовано и какие таски ещё открыты."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Репозиторий поверх соединения ``db``."""
        self._db = db

    def add(
        self,
        run_id: int,
        comment_id: int,
        version: int,
        kind: CommentKind,
        scenario: str = "",
    ) -> None:
        """Запоминает опубликованный комментарий прогона ``run_id``."""
        self._db.execute(
            "INSERT INTO bb_comments (run_id, project, repo, pr_id, comment_id, version, kind, "
            "scenario) SELECT id, project, repo, pr_id, ?, ?, ?, ? FROM runs WHERE id = ?",
            (comment_id, version, kind.value, scenario, run_id),
        )

    def open_tasks(self, project: str, repo: str, pr_id: int) -> list[CommentRecord]:
        """Неразрешённые таски демона в PR (их закрывает следующий прогон)."""
        rows = self._db.execute(
            "SELECT * FROM bb_comments WHERE project = ? AND repo = ? AND pr_id = ? "
            "AND kind = ? AND resolved = 0 ORDER BY id",
            (project, repo, pr_id, CommentKind.TASK.value),
        )
        return [_record(row) for row in rows]

    def mark_resolved(self, record_id: int) -> None:
        """Таска закрыта в Bitbucket."""
        self._db.execute("UPDATE bb_comments SET resolved = 1 WHERE id = ?", (record_id,))

    def for_run(self, run_id: int) -> list[CommentRecord]:
        """Комментарии, опубликованные прогоном."""
        rows = self._db.execute(
            "SELECT * FROM bb_comments WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [_record(row) for row in rows]


def _record(row: sqlite3.Row) -> CommentRecord:
    """Запись из строки таблицы ``bb_comments``."""
    return CommentRecord(
        id=row["id"],
        run_id=row["run_id"],
        comment_id=row["comment_id"],
        version=row["version"],
        kind=CommentKind(row["kind"]),
        scenario=row["scenario"],
        resolved=bool(row["resolved"]),
    )
