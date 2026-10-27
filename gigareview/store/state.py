"""Состояние демона, которое переживает перезапуск: пауза, время опросов, пользователь."""

import sqlite3

__all__ = ["StateRepository"]


class StateRepository:
    """Таблица ``daemon_state``: строковые значения по ключу."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Репозиторий поверх соединения ``db``."""
        self._db = db

    def get(self, key: str, default: str = "") -> str:
        """Значение ``key`` или ``default``."""
        row = self._db.execute("SELECT state FROM daemon_state WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["state"])

    def set(self, key: str, state: str) -> None:
        """Сохраняет значение ``key``."""
        self._db.execute(
            "INSERT INTO daemon_state (key, state) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET state = excluded.state",
            (key, state),
        )
