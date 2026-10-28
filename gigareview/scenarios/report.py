"""Тексты для Bitbucket: таска сценария и статусный комментарий прогона."""

from collections.abc import Sequence
from types import MappingProxyType

from gigareview.config import Scenario, ScenarioKind
from gigareview.scenarios.runner import ScenarioReport
from gigareview.scenarios.tasks import Task
from gigareview.statuses import Outcome, RunStatus

__all__ = ["crash_text", "overall_status", "summary_text", "task_text"]

#: Предел длины текста комментария (у Bitbucket ~32 тыс. символов).
MAX_COMMENT_CHARS = 30_000

_SHORT_COMMIT = 10

_OUTCOME_LABELS = MappingProxyType({
    Outcome.PASSED: "✅ пройден",
    Outcome.ISSUES: "❌ замечания",
    Outcome.ERROR: "⚠️ проверка не удалась",
})

_DETAILS_NOTE = "(подробности — в gigareview)"
_PENDING_LABEL = "⏳ ожидает"
_STARTED_LABEL = "⏳ проверки начаты — статус ревью будет поставлен после всех сценариев"
_PARTIAL_LABEL = (
    "⏳ обычные проверки завершены, идут ИИ-сценарии — статус ревью будет поставлен после них"
)
_CRASH_LABEL = f"⚠️ прогон не удался {_DETAILS_NOTE} — статус ревью не изменён"

_STATUS_LABELS = MappingProxyType({
    RunStatus.PASSED: "✅ замечаний нет — PR одобрен",
    RunStatus.ISSUES: "❌ есть замечания — нужны изменения",
    RunStatus.FAILED: "⚠️ замечаний нет, но часть проверок не удалась — статус не изменён",
})
_PASSED_UNAPPROVED_LABEL = "✅ замечаний нет"


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


def summary_text(
    reports: Sequence[ScenarioReport],
    *,
    commit: str,
    run_url: str,
    pending: Sequence[Scenario] = (),
    approve: bool = True,
) -> str:
    """Статусный комментарий: таблица сценариев, итог (или «идут проверки») и ссылка.

    Без ``approve`` успешный итог не обещает одобрения PR: статус APPROVED не ставится.
    """
    rows = [_summary_row(report.scenario, _outcome_label(report)) for report in reports]
    rows.extend(_summary_row(scenario, _PENDING_LABEL) for scenario in pending)
    if pending and reports:
        status_label = _PARTIAL_LABEL
    elif pending:
        status_label = _STARTED_LABEL
    elif not approve and overall_status(reports) is RunStatus.PASSED:
        status_label = _PASSED_UNAPPROVED_LABEL
    else:
        status_label = _STATUS_LABELS[overall_status(reports)]
    lines = [
        _header(commit),
        "",
        "| Сценарий | Результат |",
        "|---|---|",
        *rows,
        "",
        f"Итог: {status_label}",
    ]
    return _with_link(lines, run_url)


def crash_text(*, commit: str, run_url: str) -> str:
    """Статусный комментарий прогона, который упал (git, init, Bitbucket)."""
    return _with_link([_header(commit), "", f"Итог: {_CRASH_LABEL}"], run_url)


def _header(commit: str) -> str:
    """Первая строка статусного комментария."""
    short_commit = commit[:_SHORT_COMMIT]
    return f"**gigareview** — проверка коммита `{short_commit}`"


def _with_link(lines: list[str], run_url: str) -> str:
    """Строки комментария и ссылка на прогон в Web Dashboard."""
    if run_url:
        lines.append(f"\n[Подробности прогона]({run_url})")
    return "\n".join(lines)


def _summary_row(scenario: Scenario, label: str) -> str:
    """Строка таблицы статусного комментария."""
    return f"| {scenario.display_title} | {label} |"


def _outcome_label(report: ScenarioReport) -> str:
    """Ячейка результата: исход и число тасок."""
    label = _OUTCOME_LABELS[report.outcome]
    if report.outcome is Outcome.ERROR:
        # Причина (код 2, таймаут, неразборчивый вывод) — в Web Dashboard, не в PR.
        return f"{label} {_DETAILS_NOTE}"
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
