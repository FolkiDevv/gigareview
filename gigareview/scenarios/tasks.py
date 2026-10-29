"""Таски сценария: одна на весь вывод или JSON-список (ИИ-сценарии)."""

import json
from dataclasses import dataclass
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
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
    """Итог ИИ-сценария: таски и номера прошлых тасок, которые он считает закрытыми.

    ``partial`` — ревью прервано и проверило не всё: точкой отсчёта следующего
    инкрементального ревью оно не становится.
    """

    tasks: tuple[Task, ...]
    resolved: tuple[int, ...] = ()
    partial: bool = False


class _TaskModel(BaseModel):
    """Запись JSON-списка тасок."""

    model_config = ConfigDict(extra="ignore")

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    path: str | None = None
    line: int | None = Field(default=None, ge=1)


_TASKS = TypeAdapter(list[_TaskModel])
_RESOLVED = TypeAdapter(list[int])
#: Поля, по которым объект в выводе считается итогом ИИ-сценария.
_DOCUMENT_KEYS = frozenset(("tasks", "resolved", "partial"))
_PARTIAL = TypeAdapter(StrictBool)


def parse_tasks(text: str) -> tuple[Task, ...]:
    """Таски из JSON-списка — см. :func:`parse_review`."""
    return parse_review(text).tasks


def parse_review(text: str) -> ReviewDocument:
    """Разбирает ``[{"text", "path", "line"}]`` или ``{"tasks", "resolved", "partial"}``.

    Итог — последний объект вывода с ``tasks``, ``resolved`` или ``partial``: строки лога
    со скобками (``[INFO] …``, ``[]``) до и после него не мешают. Нет такого объекта —
    голый список тасок: фрагмент от первой ``[`` или ``{`` до последней парной скобки
    (модели любят оборачивать JSON в пояснения и ```-блоки). Неразборчивый вывод —
    :class:`ValueError`.
    """
    parsed: object = _last_document(text)
    if parsed is None:
        try:
            parsed = json.loads(_json_fragment(text))
        except json.JSONDecodeError as error:
            raise ValueError(f"вывод — не JSON: {error}") from error
    resolved: object = []
    partial_raw: object = False
    if isinstance(parsed, dict):
        resolved = parsed.get("resolved") or []
        partial_raw = parsed.get("partial", False)
        parsed = parsed.get("tasks", [])
    try:
        models = _TASKS.validate_python(parsed)
        resolved_ids = _RESOLVED.validate_python(resolved)
        # Строгое true/false: ошибочное «не прервано» сдвинуло бы точку отсчёта ревью.
        partial = _PARTIAL.validate_python(partial_raw)
    except ValidationError as error:
        raise ValueError(f"неверный список тасок: {error}") from error
    tasks = tuple(Task(model.text, model.path or None, model.line) for model in models)
    return ReviewDocument(tasks, tuple(dict.fromkeys(resolved_ids)), partial=partial)


def _last_document(text: str) -> dict[str, object] | None:
    """Последний объект-итог в тексте (с ``tasks``, ``resolved`` или ``partial``).

    Списки и прочие значения не берутся: строка лога ``[42]`` или вложенный список из
    обрезанного итога не должны подменить сам итог. Вложенные в найденный объект значения
    не просматриваются.
    """
    decoder = json.JSONDecoder()
    found: dict[str, object] | None = None
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found
        try:
            parsed, index = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(parsed, dict) and _DOCUMENT_KEYS & parsed.keys():
            found = parsed


def _json_fragment(text: str) -> str:
    """Фрагмент текста от первой открывающей скобки до последней закрывающей."""
    starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
    if not starts:
        return text
    start = min(starts)
    closing = "]" if text[start] == "[" else "}"
    end = text.rfind(closing)
    return text[start : end + 1] if end > start else text
