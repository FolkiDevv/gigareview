"""Тексты для Bitbucket: таска сценария и итоговый комментарий прогона."""

from collections.abc import Sequence
from types import MappingProxyType

from gigareview.config import ScenarioKind
from gigareview.scenarios.runner import ScenarioReport
from gigareview.scenarios.tasks import Task
from gigareview.statuses import Outcome, RunStatus

__all__ = ["overall_status", "summary_text", "task_text"]

#: Предел длины текста комментария (у Bitbucket ~32 тыс. символов).
MAX_COMMENT_CHARS = 30_000

_SHORT_COMMIT = 10

_OUTCOME_LABELS = MappingProxyType({
    Outcome.PASSED: "✅ пройден",
    Outcome.ISSUES: "❌ замечания",
    Outcome.ERROR: "⚠️ ошибка сценария",
})

_STATUS_LABELS = MappingProxyType({
    RunStatus.PASSED: "✅ замечаний нет — PR одобрен",
    RunStatus.ISSUES: "❌ есть замечания — нужны изменения",
    RunStatus.FAILED: "⚠️ замечаний нет, но часть сценариев завершилась ошибкой — статус не изменён",
})


def overall_status(reports: Sequence[ScenarioReport]) -> RunStatus:
    """Итог прогона: замечания важнее ошибок, ошибки — важнее успеха."""
    outcomes = {report.outcome for report in reports}
    if Outcome.ISSUES in outcomes:
        return RunStatus.ISSUES
    if Outcome.ERROR in outcomes:
        return RunStatus.FAILED
    return RunStatus.PASSED


def task_text(report: ScenarioReport, task: Task) -> str:
    """Текст таски: заголовок сценария и вывод (у обычного сценария — блоком кода)."""
    body = task.text
    if len(body) > MAX_COMMENT_CHARS:
        kept = body[:MAX_COMMENT_CHARS]
        body = f"{kept}\n… (обрезано, полный вывод — в gigareview)"
    scenario = report.scenario
    header = f"**gigareview · {scenario.display_title}**"
    if scenario.kind is ScenarioKind.AI:
        return f"{header}\n\n{body}"
    return f"{header}\n\n```\n{body}\n```"


def summary_text(reports: Sequence[ScenarioReport], *, commit: str, run_url: str) -> str:
    """Итоговый комментарий: таблица сценариев, итог и ссылка на прогон."""
    rows = [_summary_row(report) for report in reports]
    short_commit = commit[:_SHORT_COMMIT]
    status_label = _STATUS_LABELS[overall_status(reports)]
    lines = [
        f"**gigareview** — проверка коммита `{short_commit}`",
        "",
        "| Сценарий | Результат |",
        "|---|---|",
        *rows,
        "",
        f"Итог: {status_label}",
    ]
    if run_url:
        lines.append(f"\n[Подробности прогона]({run_url})")
    return "\n".join(lines)


def _summary_row(report: ScenarioReport) -> str:
    """Строка таблицы итогового комментария."""
    title = report.scenario.display_title
    label = _outcome_label(report)
    return f"| {title} | {label} |"


def _outcome_label(report: ScenarioReport) -> str:
    """Ячейка результата: исход, число тасок или текст ошибки."""
    label = _OUTCOME_LABELS[report.outcome]
    if report.outcome is Outcome.ERROR:
        error = " ".join(report.error.split()).replace("|", r"\|")
        return f"{label}: {error}"
    if not report.scenario.incremental:
        if report.outcome is Outcome.ISSUES:
            return f"{label}: {len(report.tasks)}"
        return label
    counts = _incremental_counts(report)
    return f"{label}: {counts}" if counts else label


def _incremental_counts(report: ScenarioReport) -> str:
    """Новые, оставшиеся открытыми и закрытые таски инкрементального сценария."""
    parts = []
    if report.tasks:
        parts.append(f"новых {len(report.tasks)}")
    if report.kept:
        parts.append(f"открыто прежних {report.kept}")
    if report.resolved:
        parts.append(f"закрыто {len(report.resolved)}")
    return ", ".join(parts)
