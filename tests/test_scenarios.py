import asyncio
from pathlib import Path
from typing import Any

import pytest

from gigareview.config import Scenario
from gigareview.process import ProcessOutcome
from gigareview.scenarios import (
    ScenarioContext,
    ScenarioReport,
    Task,
    overall_status,
    parse_tasks,
    run_scenario,
    summary_text,
    task_text,
)
from gigareview.scenarios import report as report_module
from gigareview.scenarios.report import crash_text
from gigareview.scenarios.tasks import parse_review
from gigareview.statuses import Outcome, RunStatus


def _scenario(**fields: Any) -> Scenario:
    return Scenario.model_validate({"name": "check", **fields})


def _run(tmp_path: Path, **fields: Any) -> ScenarioReport:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    context = ScenarioContext(
        tmp_path,
        artifacts,
        {"PATH": "/usr/bin:/bin", "TARGET_BRANCH": "master"},
    )
    return asyncio.run(run_scenario(_scenario(**fields), context))


def test_run_exit_zero_passes(tmp_path: Path) -> None:
    report = _run(tmp_path, run='echo "$TARGET_BRANCH"')
    assert report.outcome is Outcome.PASSED
    assert report.run.output == "master\n"
    assert report.validation is None
    assert report.tasks == ()


def test_run_exit_one_without_validation_is_single_task(tmp_path: Path) -> None:
    report = _run(tmp_path, run="echo problem; exit 1")
    assert report.outcome is Outcome.ISSUES
    assert report.tasks == (Task("problem"),)


def test_empty_output_task_has_placeholder(tmp_path: Path) -> None:
    report = _run(tmp_path, run="exit 1")
    assert report.tasks[0].text.startswith("(")


def test_run_other_exit_without_validation_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, run="exit 5")
    assert report.outcome is Outcome.ERROR
    assert "код 5" in report.error


def test_validation_gets_output_and_exit_code(tmp_path: Path) -> None:
    validate = (
        '[ "$GR_RUN_EXIT" = 3 ] || exit 2; '
        'grep -q found "$GR_RUN_OUTPUT" || exit 2; '
        "grep -q found && exit 1"
    )
    report = _run(tmp_path, run="echo found; exit 3", validate=validate)
    assert report.outcome is Outcome.ISSUES
    assert report.validation is not None
    assert report.validation.exit_code == 1
    assert report.tasks == (Task("found"),)


def test_validation_zero_passes_despite_run_failure(tmp_path: Path) -> None:
    report = _run(tmp_path, run="exit 7", validate="exit 0")
    assert report.outcome is Outcome.PASSED


def test_validation_two_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, run="true", validate="exit 2")
    assert report.outcome is Outcome.ERROR
    assert "некорректно" in report.error


def test_run_timeout_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, run="sleep 5", timeout_minutes=0.005)
    assert report.outcome is Outcome.ERROR
    assert "основные команды" in report.error
    assert report.run.timed_out


def test_validation_timeout_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, run="true", validate="sleep 5", timeout_minutes=0.005)
    assert report.outcome is Outcome.ERROR
    assert "валидация" in report.error


def test_format_output_becomes_task(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        run="echo raw",
        validate="exit 1",
        format="echo noise >&2; sed 's/raw/pretty/'",
    )
    assert report.outcome is Outcome.ISSUES
    assert report.tasks == (Task("pretty"),)
    assert report.formatting is not None
    assert report.formatting.stderr == "noise\n"


def test_format_failure_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, run="echo raw", validate="exit 1", format="exit 4")
    assert report.outcome is Outcome.ERROR
    assert "форматирование вернуло код 4" in report.error


def test_format_timeout_is_error(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        run="echo raw",
        validate="exit 1",
        format="sleep 5",
        timeout_minutes=0.005,
    )
    assert report.outcome is Outcome.ERROR
    assert "таймаут" in report.error


def test_ai_scenario_produces_many_tasks(tmp_path: Path) -> None:
    tasks = '[{"text": "first", "path": "a.py", "line": 3}, {"text": "second"}]'
    report = _run(tmp_path, kind="ai", run=f"echo 'Вот ответ:'; echo '{tasks}'; exit 1")
    assert report.outcome is Outcome.ISSUES
    assert report.tasks == (Task("first", "a.py", 3), Task("second"))


def test_ai_scenario_bad_json_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, kind="ai", run="echo 'not json'; exit 1")
    assert report.outcome is Outcome.ERROR
    assert "таски" in report.error


def test_ai_scenario_empty_list_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, kind="ai", run="echo '[]'; exit 1")
    assert report.outcome is Outcome.ERROR
    assert "пуст" in report.error


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n[{"text": "a"}]\n```', (Task("a"),)),
        ('{"tasks": [{"text": " b ", "path": ""}]}', (Task("b"),)),
        ('[{"text": "c", "line": 2, "extra": 1}]', (Task("c", None, 2),)),
    ],
)
def test_parse_tasks(text: str, expected: tuple[Task, ...]) -> None:
    assert parse_tasks(text) == expected


@pytest.mark.parametrize(
    "text",
    ["no brackets", "[1, 2]", '[{"text": ""}]', '[{"text": "  "}]', "[{]"],
)
def test_parse_tasks_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match=r"JSON|тасок"):
        parse_tasks(text)


def _report(outcome: Outcome, *, kind: str = "simple", **fields: Any) -> ScenarioReport:
    return ScenarioReport(
        scenario=_scenario(run="x", kind=kind, title="Проверка"),
        outcome=outcome,
        run=ProcessOutcome(0, "", "", 0.1),
        duration=0.1,
        **fields,
    )


def test_overall_status() -> None:
    passed = _report(Outcome.PASSED)
    issues = _report(Outcome.ISSUES)
    error = _report(Outcome.ERROR)
    assert overall_status([passed, passed]) is RunStatus.PASSED
    assert overall_status([passed, error]) is RunStatus.FAILED
    assert overall_status([error, issues]) is RunStatus.ISSUES
    assert overall_status([]) is RunStatus.PASSED


def test_task_text_simple_and_ai() -> None:
    simple = task_text(_report(Outcome.ISSUES), Task("line"))
    assert simple == "**gigareview · Проверка**\n\n```\nline\n```"
    ai_text = task_text(_report(Outcome.ISSUES, kind="ai"), Task("*md*"))
    assert ai_text == "**gigareview · Проверка**\n\n*md*"


def test_task_text_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_module, "MAX_COMMENT_CHARS", 3)
    assert "abc\n… (обрезано" in task_text(_report(Outcome.ISSUES), Task("abcdef"))


def test_summary_text() -> None:
    reports = [
        _report(Outcome.PASSED),
        _report(Outcome.ISSUES, tasks=(Task("a"), Task("b"))),
        _report(Outcome.ERROR, error="bad | thing\nhere"),
    ]
    text = summary_text(reports, commit="0123456789abcdef", run_url="http://x/runs/1")
    assert "`0123456789`" in text
    assert "| Проверка | ✅ пройден |" in text
    assert "❌ замечания: 2" in text
    assert "| Проверка | ⚠️ проверка не удалась (подробности — в gigareview) |" in text
    assert "bad" not in text
    assert "нужны изменения" in text
    assert "(http://x/runs/1)" in text
    assert "Подробности" not in summary_text(reports, commit="c", run_url="")


def test_summary_text_pending_and_crash() -> None:
    scenario = _scenario(run="x", kind="ai", title="ИИ")
    started = summary_text([], commit="c", run_url="", pending=[scenario])
    assert "| ИИ | ⏳ ожидает |" in started
    assert "проверки начаты" in started
    partial = summary_text([_report(Outcome.PASSED)], commit="c", run_url="", pending=[scenario])
    assert "| Проверка | ✅ пройден |" in partial
    assert "идут ИИ-сценарии" in partial
    crashed = crash_text(commit="0123456789abc", run_url="http://x/runs/2")
    assert "прогон не удался" in crashed
    assert "`0123456789`" in crashed
    assert "(http://x/runs/2)" in crashed


def test_incremental_scenario_reads_resolved_without_issues(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        kind="ai",
        incremental=True,
        run='echo \'{"tasks": [{"text": "x"}], "resolved": [3, 3, 4]}\'',
        format="cat",
    )
    assert report.outcome is Outcome.PASSED
    assert report.tasks == ()
    assert report.resolved == (3, 4)
    assert report.formatting is not None


def test_incremental_scenario_bad_document_is_error(tmp_path: Path) -> None:
    report = _run(tmp_path, kind="ai", incremental=True, run="echo nothing")
    assert report.outcome is Outcome.ERROR
    assert "таски" in report.error


def test_parse_review_with_resolved() -> None:
    document = parse_review('{"tasks": [{"text": "a"}], "resolved": [1, 2, 1]}')
    assert document.tasks == (Task("a"),)
    assert document.resolved == (1, 2)
    assert parse_review('{"resolved": [7]}').tasks == ()
    assert parse_review('[{"text": "b"}]').resolved == ()
    with pytest.raises(ValueError, match="тасок"):
        parse_review('{"tasks": [], "resolved": ["x"]}')
    assert parse_review('{"tasks": [{"text": "a"}], "partial": true}').partial
    with pytest.raises(ValueError, match="тасок"):
        parse_review('{"tasks": [{"text": "a"}], "partial": "true"}')


def test_partial_document_reaches_report(tmp_path: Path) -> None:
    document = '{"tasks": [{"text": "x"}], "partial": true}'
    report = _run(tmp_path, kind="ai", run=f"echo '{document}'; exit 1")
    assert report.outcome is Outcome.ISSUES
    assert report.partial


@pytest.mark.parametrize("options", [{"incremental": True}, {}])
def test_partial_review_without_issues_is_error(tmp_path: Path, options: dict[str, Any]) -> None:
    document = '{"tasks": [], "partial": true}'
    report = _run(tmp_path, kind="ai", run=f"echo '{document}'", **options)
    assert report.outcome is Outcome.ERROR
    assert "прервано" in report.error


@pytest.mark.parametrize("output", ["всё хорошо", '{"tasks": [], "partial": "да"}'])
def test_ai_pass_with_other_output_still_passes(tmp_path: Path, output: str) -> None:
    report = _run(tmp_path, kind="ai", run=f"echo '{output}'")
    assert report.outcome is Outcome.PASSED


def test_summary_text_incremental_counts() -> None:
    def incremental(outcome: Outcome, **fields: Any) -> ScenarioReport:
        scenario = _scenario(run="x", kind="ai", title="ИИ", incremental=True)
        return ScenarioReport(scenario, outcome, ProcessOutcome(0, "", "", 0.1), 0.1, **fields)

    reports = [
        incremental(Outcome.ISSUES, tasks=(Task("a"),), kept=2, resolved=(5,)),
        incremental(Outcome.PASSED, resolved=(6,)),
        incremental(Outcome.PASSED),
    ]
    text = summary_text(reports, commit="c", run_url="")
    assert "| ИИ | ❌ замечания: новых 1, открыто прежних 2, закрыто 1 |" in text
    assert "| ИИ | ✅ пройден: закрыто 1 |" in text
    assert "| ИИ | ✅ пройден |" in text
