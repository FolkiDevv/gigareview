"""Время в базе: UTC, ISO 8601."""

from datetime import UTC, datetime

__all__ = ["clock_time", "parse_stamp", "stamp", "utc_now"]


def utc_now() -> datetime:
    """Текущее время в UTC."""
    return datetime.now(UTC)


def stamp(moment: datetime | None = None) -> str:
    """Время в ISO 8601 для базы (по умолчанию — сейчас)."""
    return (moment or utc_now()).isoformat()


def parse_stamp(moment: str | None) -> datetime | None:
    """ISO-время из базы в ``datetime`` (``None`` остаётся ``None``)."""
    return None if moment is None else datetime.fromisoformat(moment)


def clock_time(moment: datetime) -> str:
    """Время для журнала: ``17:15`` в локальном поясе, как на Web Dashboard."""
    return moment.astimezone().strftime("%H:%M")
