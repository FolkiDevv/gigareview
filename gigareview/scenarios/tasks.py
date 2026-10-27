"""Таски сценария: одна на весь вывод или JSON-список (ИИ-сценарии)."""

import json
from dataclasses import dataclass
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

__all__ = ["ReviewDocument", "Task", "parse_review", "parse_tasks"]


@dataclass(frozen=True, slots=True)
class Task:
    """Таска для PR; ``path``/``line`` — привязка к строке файла (если известна)."""

    text: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewDocument:
    """Итог ИИ-сценария: таски и номера прошлых тасок, которые он считает закрытыми."""

    tasks: tuple[Task, ...]
    resolved: tuple[int, ...] = ()


class _TaskModel(BaseModel):
    """Запись JSON-списка тасок."""

    model_config = ConfigDict(extra="ignore")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    path: str | None = None
    line: int | None = Field(default=None, ge=1)


_TASKS = TypeAdapter(list[_TaskModel])
_RESOLVED = TypeAdapter(list[int])


def parse_tasks(text: str) -> tuple[Task, ...]:
    """Таски из JSON-списка — см. :func:`parse_review`."""
    return parse_review(text).tasks


def parse_review(text: str) -> ReviewDocument:
    """Разбирает ``[{"text", "path", "line"}]`` или ``{"tasks": [...], "resolved": [id]}``.

    Модели любят оборачивать JSON в пояснения и ```-блоки: берётся фрагмент от первой
    ``[`` или ``{`` до последней парной скобки. Неразборчивый вывод — :class:`ValueError`.
    """
    fragment = _json_fragment(text)
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError as error:
        raise ValueError(f"вывод — не JSON: {error}") from error
    resolved: object = []
    if isinstance(parsed, dict):
        resolved = parsed.get("resolved") or []
        parsed = parsed.get("tasks", [])
    try:
        models = _TASKS.validate_python(parsed)
        resolved_ids = _RESOLVED.validate_python(resolved)
    except ValidationError as error:
        raise ValueError(f"неверный список тасок: {error}") from error
    tasks = tuple(Task(model.text, model.path or None, model.line) for model in models)
    return ReviewDocument(tasks, tuple(dict.fromkeys(resolved_ids)))


def _json_fragment(text: str) -> str:
    """Фрагмент текста от первой открывающей скобки до последней закрывающей."""
    starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
    if not starts:
        return text
    start = min(starts)
    closing = "]" if text[start] == "[" else "}"
    end = text.rfind(closing)
    return text[start : end + 1] if end > start else text
