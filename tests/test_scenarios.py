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
    assert r"bad \| thing here" in text
    assert "нужны изменения" in text
    assert "(http://x/runs/1)" in text
    assert "Подробности" not in summary_text(reports, commit="c", run_url="")
