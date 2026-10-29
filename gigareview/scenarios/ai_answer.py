"""Ответ gigacrew в ИИ-ревью: JSON с находками, их отбор, привязка к строкам и таски.

В выводе ``gigacrew -p`` вперемешку идут строки шагов агента и его ответ: итог — последний
```json-блок (или последний JSON-объект с полем ``findings``), а не первая скобка вывода.
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from operator import attrgetter
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from gigareview.errors import ScenarioError

__all__ = [
    "PARTIAL_NOTE",
    "Finding",
    "Review",
    "Selection",
    "Severity",
    "anchor_findings",
    "changed_lines",
    "extract_review",
    "render_review",
    "select_findings",
]

_FENCE = re.compile(r"```[ \t]*(?:json)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
#: Сколько последних символов вывода просматривается без ```json-блока.
_TAIL_CHARS = 100_000
_MAX_CONFIDENCE = 100

#: Пометка в тасках прерванного ревью.
PARTIAL_NOTE = "_Ревью прервано ограничителем gigacrew: проверены не все изменения._"
_BEFORE: Final = "before"


@unique
class Severity(StrEnum):
    """Серьёзность находки."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


_RANKS = MappingProxyType({Severity.CRITICAL: 0, Severity.MAJOR: 1, Severity.MINOR: 2})
_LABELS = MappingProxyType({
    Severity.CRITICAL: "🔴 Критично",
    Severity.MAJOR: "🟠 Ошибка",
    Severity.MINOR: "🟡 Замечание",
})


class Finding(BaseModel):
    """Находка ревью. Поля разбираются мягко: неверное значение — значение по умолчанию."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    path: str | None = None
    line: int | None = None
    end_line: int | None = None
    severity: Severity = Severity.MAJOR
    title: str = ""
    body: str = ""
    suggestion: str = ""
    confidence: int = _MAX_CONFIDENCE

    @property
    def rank(self) -> int:
        """Порядок по серьёзности: 0 — самая серьёзная."""
        return _RANKS[self.severity]

    @field_validator("path", mode=_BEFORE)
    @classmethod
    def _clean_path(cls, raw: object) -> str | None:
        if not isinstance(raw, str):
            return None
        path = raw.strip().removeprefix("./")
        return path or None

    @field_validator("line", "end_line", mode=_BEFORE)
    @classmethod
    def _clean_line(cls, raw: object) -> int | None:
        number = _as_int(raw)
        return number if number is not None and number >= 1 else None

    @field_validator("severity", mode=_BEFORE)
    @classmethod
    def _clean_severity(cls, raw: object) -> Severity:
        try:
            return Severity(str(raw).strip().lower())
        except ValueError:
            return Severity.MAJOR

    @field_validator("title", "body", "suggestion", mode=_BEFORE)
    @classmethod
    def _clean_text(cls, raw: object) -> str:
        return "" if raw is None else str(raw).strip()

    @field_validator("confidence", mode=_BEFORE)
    @classmethod
    def _clean_confidence(cls, raw: object) -> int:
        number = _as_int(raw)
        if number is None:
            return _MAX_CONFIDENCE
        return min(max(number, 0), _MAX_CONFIDENCE)


class Review(BaseModel):
    """Итог ревью: находки и номера закрытых прошлых тасок."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    findings: tuple[Finding, ...] = ()
    resolved: tuple[int, ...] = Field(default=())

    @field_validator("findings", mode=_BEFORE)
    @classmethod
    def _only_objects(cls, raw: object) -> object:
        if isinstance(raw, list):
            return [entry for entry in raw if isinstance(entry, dict)]
        return raw

    @field_validator("resolved", mode=_BEFORE)
    @classmethod
    def _only_ids(cls, raw: object) -> tuple[int, ...]:
        if not isinstance(raw, list):
            return ()
        ids = (_as_int(entry) for entry in raw)
        return tuple(dict.fromkeys(number for number in ids if number is not None))


@dataclass(frozen=True, slots=True)
class Selection:
    """Какие находки публиковать: порог уверенности, серьёзность и число."""

    min_confidence: int = 70
    min_severity: Severity = Severity.MAJOR
    limit: int = 10


def extract_review(text: str) -> Review:
    """Итог ревью из вывода gigacrew — последний подходящий JSON; нет его — ошибка."""
    for candidate in _candidates(text):
        review = _as_review(candidate)
        if review is not None:
            return review
    raise ScenarioError('в ответе нет итога ревью: ```json-блока с полем "findings"')


def select_findings(findings: Sequence[Finding], selection: Selection) -> list[Finding]:
    """Находки для публикации: пороги, без повторов, самые серьёзные первыми, не больше лимита."""
    threshold = _RANKS[selection.min_severity]
    seen: set[tuple[str | None, int | None, str]] = set()
    chosen = []
    for finding in findings:
        key = (finding.path, finding.line, finding.title.casefold())
        too_weak = finding.rank > threshold or finding.confidence < selection.min_confidence
        if too_weak or key in seen or not (finding.title or finding.body):
            continue
        seen.add(key)
        chosen.append(finding)
    chosen.sort(key=attrgetter("rank"))
    return chosen[: selection.limit]


def changed_lines(diff: str) -> dict[str, set[int]]:
    """Добавленные и изменённые строки новых версий файлов из ``git diff -U0``."""
    lines: dict[str, set[int]] = {}
    current: set[int] | None = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip().strip('"')
            current = None if target == "/dev/null" else lines.setdefault(target[2:], set())
            continue
        hunk = _HUNK.match(row)
        if hunk is not None and current is not None:
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            current.update(range(start, start + count))
    return lines


def anchor_findings(
    findings: Sequence[Finding],
    changed: Mapping[str, set[int]],
) -> list[Finding]:
    """Привязка к изменённой строке: ``line`` вне диффа — первая изменённая строка участка.

    Если в участке ``line..end_line`` изменённых строк нет, привязка остаётся как есть:
    Bitbucket её не примет, и таска будет опубликована с ``путь:строка`` в тексте.
    """
    anchored = []
    for finding in findings:
        lines = changed.get(finding.path or "")
        line = finding.line
        if lines is None or line is None or line in lines:
            anchored.append(finding)
            continue
        last = max(finding.end_line or line, line)
        inside = sorted(number for number in lines if line <= number <= last)
        anchored.append(finding.model_copy(update={"line": inside[0]}) if inside else finding)
    return anchored


def render_review(
    findings: Sequence[Finding],
    resolved: Sequence[int],
    *,
    partial: bool = False,
) -> str:
    """Итог для gigareview: ``{"tasks": [{"text", "path", "line"}], "resolved", "partial"}``.

    ``partial`` — ревью прервано: в тасках — пометка, а gigareview не берёт такой прогон
    точкой отсчёта следующего инкрементального ревью.
    """
    note = PARTIAL_NOTE if partial else ""
    tasks = [
        {"text": _task_text(finding, note), "path": finding.path, "line": finding.line}
        for finding in findings
    ]
    document = {"tasks": tasks, "resolved": list(resolved), "partial": partial}
    return json.dumps(document, ensure_ascii=False, indent=2)


def _task_text(finding: Finding, note: str) -> str:
    """Текст таски: серьёзность и заголовок, участок, суть и как исправить."""
    label = _LABELS[finding.severity]
    header = f"**{label}: {finding.title}**" if finding.title else f"**{label}**"
    parts = [header]
    if (
        finding.line is not None
        and finding.end_line is not None
        and finding.end_line > finding.line
    ):
        parts.append(f"_Строки {finding.line}–{finding.end_line}_")
    if finding.body:
        parts.append(finding.body)
    if finding.suggestion:
        parts.append(f"**Как исправить:** {finding.suggestion}")
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def _candidates(text: str) -> Iterator[object]:
    """JSON-значения ответа с конца: сначала ```-блоки, затем объекты в хвосте вывода."""
    for block in reversed(_FENCE.findall(text)):
        yield _loads(block)
    tail = text[-_TAIL_CHARS:]
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(tail) if char == "{"]
    for start in reversed(starts):
        try:
            parsed, _ = decoder.raw_decode(tail, start)
        except json.JSONDecodeError:
            continue
        # Голый список вне ```-блока — не итог: «[]» встречается и в строках шагов.
        if isinstance(parsed, dict):
            yield parsed


def _loads(block: str) -> object:
    """JSON-значение блока; не JSON — ``None``."""
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


def _as_review(candidate: object) -> Review | None:
    """Итог ревью из JSON-значения: объект с ``findings`` или список находок."""
    if isinstance(candidate, list):
        # Список из чужого ```-блока (README, вывод команды) — не итог ревью.
        if not all(
            isinstance(entry, dict) and ("title" in entry or "body" in entry) for entry in candidate
        ):
            return None
        candidate = {"findings": candidate}
    if not isinstance(candidate, dict) or "findings" not in candidate:
        return None
    try:
        return Review.model_validate(candidate)
    except ValidationError:
        return None


def _as_int(raw: object) -> int | None:
    """Целое из числа или строки; ``None`` — не число."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, (float, str)):
        try:
            return int(float(raw))
        except (ValueError, OverflowError):
            return None
    return None
