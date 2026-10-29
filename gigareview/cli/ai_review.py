"""Команды ``gigareview ai-review``: шаги ИИ-сценария ревью на gigacrew.

``prompt`` — промпт для ``gigacrew -p`` (основные команды), ``tasks --check`` — валидация
ответа, ``tasks`` — форматирование в таски. Работают в окружении сценария, без конфигурации.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from gigareview.errors import GigaReviewError
from gigareview.scenarios.ai_answer import (
    Review,
    Selection,
    Severity,
    anchor_findings,
    changed_lines,
    extract_review,
    render_review,
    select_findings,
)
from gigareview.scenarios.ai_prompt import build_prompt, git_output, review_range

__all__ = ["add_parser", "run"]

_EXIT_ERROR = 2
#: Код gigacrew «ход остановлен ограничителем» (лимит шагов, контекста, серия ошибок):
#: итог модель ещё подводит, но ревью могло не дойти до части изменений.
_GIGACREW_STOPPED = "3"
_PARTIAL_NOTE = "_Ревью прервано ограничителем gigacrew: проверены не все изменения._"


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Подкоманда ``ai-review`` с действиями ``prompt`` и ``tasks``."""
    parser = sub.add_parser("ai-review", help="Шаги ИИ-сценария ревью на gigacrew")
    actions = parser.add_subparsers(dest="action", required=True)
    prompt = actions.add_parser("prompt", help="Промпт ревью для gigacrew -p")
    prompt.add_argument("--focus", default="", help="Дополнительные указания ревьюеру")
    tasks = actions.add_parser("tasks", help="Ответ gigacrew со stdin → таски gigareview")
    tasks.add_argument(
        "--check",
        action="store_true",
        help="Валидация: 0 — замечаний нет, 1 — есть, 2 — ответ не разобрать",
    )
    defaults = Selection()
    tasks.add_argument("--min-confidence", type=int, default=defaults.min_confidence)
    tasks.add_argument(
        "--min-severity",
        type=Severity,
        choices=list(Severity),
        default=defaults.min_severity,
    )
    tasks.add_argument("--limit", type=int, default=defaults.limit)


def run(args: argparse.Namespace) -> int:
    """Выполняет действие ``ai-review``; ошибка — сообщение в stderr и код 2."""
    try:
        if args.action == "prompt":
            sys.stdout.write(asyncio.run(build_prompt(os.environ, Path.cwd(), focus=args.focus)))
            return 0
        return asyncio.run(_tasks(args))
    except GigaReviewError as error:
        sys.stderr.write(f"Ошибка: {error}\n")
        return _EXIT_ERROR


async def _tasks(args: argparse.Namespace) -> int:
    """Валидация (``--check``) или форматирование ответа gigacrew."""
    code = os.environ.get("GR_RUN_EXIT", "0")
    if args.check and code not in {"0", _GIGACREW_STOPPED}:
        sys.stderr.write(f"gigacrew завершился с кодом {code}: ревью не закончено\n")
        return _EXIT_ERROR
    review = extract_review(sys.stdin.read())
    selection = Selection(args.min_confidence, args.min_severity, args.limit)
    findings = select_findings(review.findings, selection)
    partial = code == _GIGACREW_STOPPED
    if args.check:
        return _check(review, len(findings), partial=partial)
    scope = review_range(os.environ)
    diff = await git_output(Path.cwd(), "diff", "--no-color", "-U0", "-M", scope.base, "HEAD")
    anchored = anchor_findings(findings, changed_lines(diff))
    note = _PARTIAL_NOTE if partial else ""
    sys.stdout.write(f"{render_review(anchored, review.resolved, note=note)}\n")
    return 0


def _check(review: Review, selected: int, *, partial: bool) -> int:
    """Код валидации; прерванное ревью без находок — ошибка, чтобы PR не был одобрен."""
    sys.stdout.write(_check_summary(review, selected))
    if partial and not selected:
        sys.stderr.write(
            "gigacrew остановлен ограничителем (код 3) и замечаний не нашёл: ревью неполное, "
            "PR по нему не одобряется\n",
        )
        return _EXIT_ERROR
    return 1 if selected else 0


def _check_summary(review: Review, selected: int) -> str:
    """Строка итога валидации для журнала прогона."""
    found = len(review.findings)
    closed = len(review.resolved)
    return f"Находок: {found}, к публикации: {selected}, закрыто прежних тасок: {closed}\n"
