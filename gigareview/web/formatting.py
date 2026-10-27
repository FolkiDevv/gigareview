"""Фильтры Jinja: время, длительность, подписи статусов."""

from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType

from gigareview.statuses import EventLevel, RunStatus

__all__ = ["FILTERS", "duration", "status_label", "when"]

# Статусы прогона, исходы сценария и уровни журнала — строки; одинаковые значения
# (passed, issues, error) подписываются одинаково.
_STATUS_LABELS: Mapping[str, str] = MappingProxyType({
    RunStatus.QUEUED: "в очереди",
    RunStatus.RUNNING: "выполняется",
    RunStatus.PASSED: "пройден",
    RunStatus.ISSUES: "замечания",
    RunStatus.FAILED: "сбой сценариев",
    RunStatus.ERROR: "ошибка",
    RunStatus.INTERRUPTED: "прерван",
    EventLevel.NOTE: "инфо",
    EventLevel.WARNING: "предупреждение",
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
        return f"{minutes} мин {padded} с"
    return f"{rest} с"


def status_label(status: str) -> str:
    """Подпись статуса прогона, исхода сценария или уровня события по-русски."""
    return _STATUS_LABELS.get(status, status)


#: Фильтры для окружения Jinja.
FILTERS: Mapping[str, Callable[..., str]] = MappingProxyType({
    "when": when,
    "duration": duration,
    "status_label": status_label,
})
