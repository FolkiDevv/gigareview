"""Подключение к SQLite и миграции; доступ к таблицам — через репозитории-атрибуты."""

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from gigareview.store.comments import CommentRepository
from gigareview.store.events import EventRepository
from gigareview.store.queries import RunQueries
from gigareview.store.runs import RunRepository
from gigareview.store.schema import MIGRATIONS
from gigareview.store.state import StateRepository

__all__ = ["Store"]


class Store:
    """База демона. Одно соединение на процесс: демон и веб живут в одном event loop."""

    def __init__(self, path: Path) -> None:
        """Открывает (создаёт) базу ``path`` и применяет недостающие миграции."""
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self.runs = RunRepository(self._db)
        self.queries = RunQueries(self._db)
        self.comments = CommentRepository(self._db)
        self.events = EventRepository(self._db)
        self.state = StateRepository(self._db)

    def __enter__(self) -> Self:
        """База на время блока ``with``."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Закрывает базу по выходе из блока ``with``."""
        self.close()

    def close(self) -> None:
        """Закрывает соединение."""
        self._db.close()

    def _migrate(self) -> None:
        """Применяет миграции, которых ещё нет в базе, каждую в своей транзакции."""
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self._db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
