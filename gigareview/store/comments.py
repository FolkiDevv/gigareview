"""Комментарии и таски, которые демон оставил в Bitbucket."""

import sqlite3

from gigareview.store.records import CommentKind, CommentRecord, TaskDetails

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
        details: TaskDetails | None = None,
    ) -> int:
        """Запоминает опубликованный комментарий прогона ``run_id``; возвращает номер записи."""
        task = details or TaskDetails("")
        cursor = self._db.execute(
            "INSERT INTO bb_comments (run_id, project, repo, pr_id, comment_id, version, kind, "
            "scenario, text, path, line) "
            "SELECT id, project, repo, pr_id, ?, ?, ?, ?, ?, ?, ? FROM runs WHERE id = ?",
            (comment_id, version, kind.value, scenario, task.text, task.path, task.line, run_id),
        )
        return int(cursor.lastrowid or 0)

    def latest_summary(self, project: str, repo: str, pr_id: int) -> CommentRecord | None:
        """Статусный комментарий PR последнего прогона (его обновляют следующие прогоны)."""
        row = self._db.execute(
            "SELECT * FROM bb_comments WHERE project = ? AND repo = ? AND pr_id = ? "
            "AND kind = ? ORDER BY id DESC LIMIT 1",
            (project, repo, pr_id, CommentKind.SUMMARY.value),
        ).fetchone()
        return None if row is None else _record(row)

    def update(self, record_id: int, version: int, text: str) -> None:
        """Комментарий изменён: новая версия в Bitbucket и сохранённый текст."""
        self._db.execute(
            "UPDATE bb_comments SET version = ?, text = ? WHERE id = ?",
            (version, text, record_id),
        )

    def open_tasks(
        self,
        project: str,
        repo: str,
        pr_id: int,
        scenario: str | None = None,
    ) -> list[CommentRecord]:
        """Неразрешённые таски демона в PR (все или одного сценария ``scenario``)."""
        return [
            record for record in self.tasks(project, repo, pr_id, scenario) if not record.resolved
        ]

    def tasks(
        self,
        project: str,
        repo: str,
        pr_id: int,
        scenario: str | None = None,
    ) -> list[CommentRecord]:
        """Все таски демона в PR, и закрытые (все или одного сценария ``scenario``)."""
        rows = self._db.execute(
            "SELECT * FROM bb_comments WHERE project = ? AND repo = ? AND pr_id = ? "
            "AND kind = ? AND (? IS NULL OR scenario = ?) ORDER BY id",
            (project, repo, pr_id, CommentKind.TASK.value, scenario, scenario),
        )
        return [_record(row) for row in rows]

    def dismissed_tasks(
        self,
        project: str,
        repo: str,
        pr_id: int,
        scenario: str,
    ) -> list[CommentRecord]:
        """Таски сценария ``scenario``, которые закрыл или удалил человек, а не демон."""
        return [record for record in self.tasks(project, repo, pr_id, scenario) if record.dismissed]

    def mark_resolved(self, record_id: int) -> None:
        """Таску закрыл демон."""
        self._db.execute("UPDATE bb_comments SET resolved = 1 WHERE id = ?", (record_id,))

    def mark_dismissed(self, record_id: int) -> None:
        """Таску закрыл или удалил в Bitbucket человек: для демона это ложное срабатывание."""
        self._db.execute(
            "UPDATE bb_comments SET resolved = 1, dismissed = 1 WHERE id = ?",
            (record_id,),
        )

    def mark_open(self, record_id: int) -> None:
        """Закрытую таску снова открыли в Bitbucket."""
        self._db.execute(
            "UPDATE bb_comments SET resolved = 0, dismissed = 0 WHERE id = ?",
            (record_id,),
        )

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
        details=TaskDetails(row["text"], row["path"], row["line"]),
        dismissed=bool(row["dismissed"]),
    )
