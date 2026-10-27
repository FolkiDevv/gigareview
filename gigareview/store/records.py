"""Записи хранилища: прогоны, результаты сценариев, комментарии, события, фильтры."""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, unique
from typing import Any, Self

from gigareview.statuses import EventLevel, Outcome, RunStatus, Trigger

__all__ = [
    "CommentKind",
    "CommentRecord",
    "EventRecord",
    "NewRun",
    "PullRequestSummary",
    "RunFilter",
    "RunRecord",
    "RunStats",
    "ScenarioRecord",
    "TaskDetails",
]


@unique
class CommentKind(StrEnum):
    """Комментарий демона в Bitbucket: таска по сценарию или итоговый комментарий."""

    TASK = "task"
    SUMMARY = "summary"


@dataclass(frozen=True, slots=True)
class NewRun:
    """Прогон, который ставится в очередь: PR, коммит и кто запустил."""

    project: str
    repo: str
    pr_id: int
    pr_title: str
    pr_author: str
    pr_url: str
    source_branch: str
    target_branch: str
    commit_id: str
    trigger: Trigger
    fingerprint: str = ""
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Прогон из базы."""

    id: int
    project: str
    repo: str
    pr_id: int
    pr_title: str
    pr_author: str
    pr_url: str
    source_branch: str
    target_branch: str
    commit_id: str
    trigger: Trigger
    status: RunStatus
    fingerprint: str
    error: str
    setup_log: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    dry_run: bool
    published: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> Self:
        """Запись из строки таблицы ``runs``."""
        fields = dict(row)
        fields["trigger"] = Trigger(fields["trigger"])
        fields["status"] = RunStatus(fields["status"])
        fields["dry_run"] = bool(fields["dry_run"])
        fields["published"] = bool(fields.get("published"))
        for stamp in ("created_at", "started_at", "finished_at"):
            fields[stamp] = _parse_time(fields[stamp])
        return cls(**fields)

    @property
    def repo_key(self) -> str:
        """``PROJECT/slug``."""
        return f"{self.project}/{self.repo}"

    @property
    def short_commit(self) -> str:
        """Первые 10 символов хеша коммита."""
        return self.commit_id[:10]

    @property
    def duration(self) -> float | None:
        """Длительность выполнения в секундах (``None`` — ещё не начат или не закончен)."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class ScenarioRecord:
    """Результат одного сценария в прогоне: коды и выводы всех шагов, исход, тексты тасок."""

    name: str
    title: str
    kind: str
    outcome: Outcome
    run_exit: int | None
    run_output: str
    duration: float
    validate_exit: int | None = None
    validate_output: str = ""
    format_exit: int | None = None
    format_output: str = ""
    tasks: tuple[str, ...] = ()
    error: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """Запись из строки таблицы ``scenario_results`` (тексты тасок — через разделитель)."""
        fields = dict(row)
        for column in ("id", "run_id", "position"):
            fields.pop(column)
        fields["outcome"] = Outcome(fields["outcome"])
        joined = fields["tasks"]
        fields["tasks"] = tuple(joined.split(TASK_SEPARATOR)) if joined else ()
        return cls(**fields)


#: Разделитель текстов тасок в одной колонке (ASCII Record Separator).
TASK_SEPARATOR = "\x1e"


@dataclass(frozen=True, slots=True)
class TaskDetails:
    """Текст и привязка опубликованной таски: их видит следующий прогон сценария."""

    text: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class CommentRecord:
    """Комментарий, который демон оставил в Bitbucket."""

    id: int
    run_id: int
    comment_id: int
    version: int
    kind: CommentKind
    scenario: str
    resolved: bool
    details: TaskDetails


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Запись журнала демона."""

    id: int
    created_at: datetime
    level: EventLevel
    message: str
    run_id: int | None


@dataclass(frozen=True, slots=True)
class RunFilter:
    """Фильтр списка прогонов (пустое поле — без ограничения)."""

    status: RunStatus | None = None
    repo: str = ""
    pr_id: int | None = None
    scenario: str = ""
    outcome: Outcome | None = None
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class RunStats:
    """Сводка по прогонам: число по статусам и средняя длительность."""

    by_status: dict[RunStatus, int] = field(default_factory=dict)
    average_duration: float | None = None

    @property
    def total(self) -> int:
        """Всего прогонов."""
        return sum(self.by_status.values())

    def count(self, status: RunStatus) -> int:
        """Прогонов со статусом ``status``."""
        return self.by_status.get(status, 0)


@dataclass(frozen=True, slots=True)
class PullRequestSummary:
    """PR, по которому были прогоны: последний прогон и их число."""

    last_run: RunRecord
    runs_count: int


def _parse_time(stamp: str | None) -> datetime | None:
    """ISO-время из базы в ``datetime`` (``None`` остаётся ``None``)."""
    return None if stamp is None else datetime.fromisoformat(stamp)
