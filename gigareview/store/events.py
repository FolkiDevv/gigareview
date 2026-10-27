"""Журнал демона."""

import sqlite3
from datetime import datetime

from gigareview.statuses import EventLevel
from gigareview.store.clock import stamp
from gigareview.store.records import EventRecord

__all__ = ["EventRepository"]


class EventRepository:
    """Таблица ``events``: записи журнала с уровнем и, если есть, номером прогона."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Репозиторий поверх соединения ``db``."""
        self._db = db

    def add(self, level: EventLevel, message: str, run_id: int | None = None) -> None:
        """Добавляет запись в журнал."""
        self._db.execute(
            "INSERT INTO events (created_at, level, message, run_id) VALUES (?, ?, ?, ?)",
            (stamp(), level.value, message, run_id),
        )

    def list(
        self,
        level: EventLevel | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventRecord]:
        """Записи, новые сначала; ``level`` — только этого уровня."""
        where, args = _where(level)
        rows = self._db.execute(
            # В текст запроса подставляются только имена колонок и условия из кода.
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
            (*args, limit, offset),
        )
        return [
            EventRecord(
                id=row["id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                level=EventLevel(row["level"]),
                message=row["message"],
                run_id=row["run_id"],
            )
            for row in rows
        ]

    def count(self, level: EventLevel | None = None) -> int:
        """Число записей (``level`` — только этого уровня)."""
        where, args = _where(level)
        # В текст запроса подставляются только имена колонок и условия из кода.
        row = self._db.execute(f"SELECT count(*) FROM events {where}", args).fetchone()  # noqa: S608
        return int(row[0])


def _where(level: EventLevel | None) -> tuple[str, tuple[str, ...]]:
    """Условие ``WHERE`` по уровню и его аргументы."""
    if level is None:
        return "", ()
    return "WHERE level = ?", (level.value,)
