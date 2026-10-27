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

__all__ = ["Task", "parse_tasks"]


@dataclass(frozen=True, slots=True)
class Task:
    """Таска для PR; ``path``/``line`` — привязка к строке файла (если известна)."""

    text: str
    path: str | None = None
    line: int | None = None


class _TaskModel(BaseModel):
    """Запись JSON-списка тасок."""

    model_config = ConfigDict(extra="ignore")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    path: str | None = None
    line: int | None = Field(default=None, ge=1)


_TASKS = TypeAdapter(list[_TaskModel])


def parse_tasks(text: str) -> tuple[Task, ...]:
    """Разбирает JSON-список ``[{"text", "path", "line"}]`` (или ``{"tasks": [...]}``).

    Модели любят оборачивать JSON в пояснения и ```-блоки: берётся фрагмент от первой
    ``[`` или ``{`` до последней парной скобки. Неразборчивый вывод — :class:`ValueError`.
    """
    fragment = _json_fragment(text)
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError as error:
        raise ValueError(f"вывод — не JSON: {error}") from error
    if isinstance(parsed, dict):
        parsed = parsed.get("tasks")
    try:
        models = _TASKS.validate_python(parsed)
    except ValidationError as error:
        raise ValueError(f"неверный список тасок: {error}") from error
    return tuple(Task(model.text, model.path or None, model.line) for model in models)


def _json_fragment(text: str) -> str:
    """Фрагмент текста от первой открывающей скобки до последней закрывающей."""
    starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
    if not starts:
        return text
    start = min(starts)
    closing = "]" if text[start] == "[" else "}"
    end = text.rfind(closing)
    return text[start : end + 1] if end > start else text
