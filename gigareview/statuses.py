"""Статусы прогонов и исходы сценариев — общий словарь демона, базы и Web Dashboard."""

from enum import StrEnum, unique

__all__ = ["EventLevel", "Outcome", "ReviewStatus", "RunStatus", "Trigger"]


@unique
class Outcome(StrEnum):
    """Исход сценария: код валидации 0, 1 или сбой (код 2, другой код, таймаут)."""

    PASSED = "passed"
    ISSUES = "issues"
    ERROR = "error"


@unique
class RunStatus(StrEnum):
    """Состояние прогона сценариев на одном коммите PR."""

    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"  # все сценарии без замечаний — аппрув
    ISSUES = "issues"  # есть замечания — запрос изменений
    FAILED = "failed"  # замечаний нет, но сценарии завершились ошибкой
    ERROR = "error"  # сбой самого прогона: git, init, Bitbucket
    INTERRUPTED = "interrupted"  # демон остановлен посреди прогона

    @property
    def is_active(self) -> bool:
        """Прогон ещё в очереди или выполняется."""
        return self in {RunStatus.QUEUED, RunStatus.RUNNING}


@unique
class Trigger(StrEnum):
    """Кто запустил прогон: опрос демона, кнопка в Web Dashboard или командная строка."""

    AUTO = "auto"
    MANUAL = "manual"
    CLI = "cli"


@unique
class ReviewStatus(StrEnum):
    """Статус ревьюера в Bitbucket."""

    APPROVED = "APPROVED"
    NEEDS_WORK = "NEEDS_WORK"
    UNAPPROVED = "UNAPPROVED"


@unique
class EventLevel(StrEnum):
    """Уровень записи в журнале демона."""

    NOTE = "info"
    WARNING = "warning"
    ERROR = "error"
