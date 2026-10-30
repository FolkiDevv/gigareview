"""Фильтры Jinja: время, длительность, подписи статусов."""

from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType

from gigareview.gitops import redact_credentials
from gigareview.statuses import EventLevel, RunStatus

__all__ = ["FILTERS", "duration", "status_label", "when", "without_credentials"]

# Статусы прогона, исходы сценария и уровни журнала — строки; одинаковые значения
# (passed, issues, error) подписываются одинаково.
_STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    RunStatus.QUEUED: "queued",
    RunStatus.RUNNING: "running",
    RunStatus.PASSED: "passed",
    RunStatus.ISSUES: "issues",
    RunStatus.FAILED: "scenario failures",
    RunStatus.ERROR: "error",
    RunStatus.INTERRUPTED: "interrupted",
    EventLevel.DEBUG: "debug",
    EventLevel.NOTE: "info",
    EventLevel.WARNING: "warning",
})

_SECONDS_IN_MINUTE = 60


def when(moment: datetime | None) -> str:
    """Время в локальном поясе: ``27.10 17:15:03`` (пусто — тире)."""
    if moment is None:
        return "—"
    return moment.astimezone().strftime("%d.%m %H:%M:%S")


def duration(seconds: float | None) -> str:
    """Длительность: ``42 с`` или ``3 мин 05 с`` (нет — тире)."""
    if seconds is None:
        return "—"
    minutes, rest = divmod(round(seconds), _SECONDS_IN_MINUTE)
    if minutes:
        padded = str(rest).zfill(2)
        return f"{minutes} min {padded} s"
    return f"{rest} s"


def status_label(status: str) -> str:
    """Подпись статуса прогона, исхода сценария или уровня события по-русски."""
    return _STATUS_LABELS.get(status, status)


#: Фильтры для окружения Jinja.
def without_credentials(url: str) -> str:
    """Адрес без пароля (``https://user:pass@host`` → ``https://***@host``)."""
    return redact_credentials(url, url)


FILTERS: Mapping[str, Callable[..., str]] = MappingProxyType({
    "without_credentials": without_credentials,
    "when": when,
    "duration": duration,
    "status_label": status_label,
})
