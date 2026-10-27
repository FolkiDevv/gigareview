"""Время в базе: UTC, ISO 8601."""

from datetime import UTC, datetime

__all__ = ["stamp", "utc_now"]


def utc_now() -> datetime:
    """Текущее время в UTC."""
    return datetime.now(UTC)


def stamp(moment: datetime | None = None) -> str:
    """Время в ISO 8601 для базы (по умолчанию — сейчас)."""
    return (moment or utc_now()).isoformat()
