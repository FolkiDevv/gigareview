"""Выполнение сценария: основные команды → валидация → форматирование → таски."""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from gigareview.config import Scenario, ScenarioKind
from gigareview.process import ProcessOutcome, run_process
from gigareview.scenarios.tasks import ReviewDocument, Task, parse_review
from gigareview.statuses import Outcome

__all__ = ["ScenarioContext", "ScenarioReport", "run_scenario"]

#: Код валидации → исход сценария; любой другой код — ошибка сценария.
_OUTCOMES = MappingProxyType({0: Outcome.PASSED, 1: Outcome.ISSUES})

_EMPTY_OUTPUT = "(сценарий не вывел подробностей)"


@dataclass(frozen=True, slots=True)
class ScenarioContext:
    """Где и с каким окружением выполнять команды сценария."""

    repo_dir: Path
    artifacts: Path
    env: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    """Итог сценария: исход, результаты шагов, таски и текст ошибки.

    ``resolved`` — прошлые таски, которые инкрементальный сценарий считает закрытыми;
    ``kept`` — сколько его прошлых тасок остаются открытыми (считает пайплайн).
    """

    scenario: Scenario
    outcome: Outcome
    run: ProcessOutcome
    duration: float
    validation: ProcessOutcome | None = None
    formatting: ProcessOutcome | None = None
    tasks: tuple[Task, ...] = ()
    error: str = ""
    resolved: tuple[int, ...] = ()
    kept: int = 0


@dataclass(slots=True)
class _Steps:
    """Результаты шагов по мере выполнения."""

    run: ProcessOutcome
    validation: ProcessOutcome | None = None
    formatting: ProcessOutcome | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


async def run_scenario(scenario: Scenario, context: ScenarioContext) -> ScenarioReport:
    """Выполняет сценарий целиком в пределах его таймаута."""
    return await _Runner(scenario, context).execute()


class _Runner:
    """Один прогон сценария: общий дедлайн и окружение для всех шагов."""

    def __init__(self, scenario: Scenario, context: ScenarioContext) -> None:
        self._scenario = scenario
        self._context = context
        self._started = time.monotonic()
        self._deadline = self._started + scenario.timeout_seconds

    async def execute(self) -> ScenarioReport:
        """Основные команды, валидация, форматирование и разбор тасок."""
        steps = _Steps(run=await self._step(self._scenario.run, {}))
        if steps.run.timed_out:
            return self._failed(steps, "основные команды не уложились в таймаут")
        name = self._scenario.name
        output_file = self._context.artifacts / f"{name}.out"
        output_file.write_text(steps.run.output, encoding="utf-8")
        steps.extra_env = {
            "GR_RUN_EXIT": str(steps.run.exit_code),
            "GR_RUN_OUTPUT": str(output_file),
        }
        code = steps.run.exit_code
        if self._scenario.validate_script:
            steps.validation = await self._step(self._scenario.validate_script, steps.extra_env)
            if steps.validation.timed_out:
                return self._failed(steps, "валидация не уложилась в таймаут")
            code = steps.validation.exit_code
        outcome = _OUTCOMES.get(-1 if code is None else code)
        if outcome is None:
            return self._failed(
                steps,
                f"валидация вернула код {code}: сценарий отработал некорректно",
            )
        if outcome is Outcome.PASSED and not self._scenario.incremental:
            return self._report(steps, Outcome.PASSED)
        return await self._document(steps, outcome)

    async def _document(self, steps: _Steps, outcome: Outcome) -> ScenarioReport:
        """Итог: вывод форматирования или основных команд → таски и закрытые прошлые таски.

        Инкрементальному сценарию итог нужен и без замечаний — в нём список ``resolved``.
        """
        text = steps.run.output
        if self._scenario.format_script:
            steps.formatting = await self._step(
                self._scenario.format_script,
                steps.extra_env,
                merge_stderr=False,
            )
            if steps.formatting.exit_code != 0:
                return self._failed(steps, _format_error(steps.formatting))
            text = steps.formatting.output
        try:
            document = self._parse(text)
        except ValueError as error:
            return self._failed(steps, f"не удалось разобрать таски: {error}")
        if outcome is Outcome.PASSED:
            return self._report(steps, Outcome.PASSED, resolved=document.resolved)
        if not document.tasks:
            return self._failed(
                steps,
                "не удалось разобрать таски: валидация нашла замечания, а список тасок пуст",
            )
        return self._report(
            steps,
            Outcome.ISSUES,
            tasks=document.tasks,
            resolved=document.resolved,
        )

    def _parse(self, text: str) -> ReviewDocument:
        """Итог из текста: весь текст — одна таска, у ИИ-сценария — JSON."""
        if self._scenario.kind is ScenarioKind.SIMPLE:
            return ReviewDocument((Task(text.strip() or _EMPTY_OUTPUT),))
        return parse_review(text)

    async def _step(
        self,
        script: str,
        extra_env: Mapping[str, str],
        *,
        merge_stderr: bool = True,
    ) -> ProcessOutcome:
        """Шаг сценария: bash-скрипт в каталоге репозитория; stdin — вывод основных команд."""
        stdin_text = self._run_output(extra_env) if extra_env else ""
        return await run_process(
            ("bash", "-c", script),
            cwd=self._context.repo_dir,
            env={**self._context.env, **extra_env},
            stdin_text=stdin_text,
            time_limit=max(self._deadline - time.monotonic(), 0.1),
            merge_stderr=merge_stderr,
        )

    def _run_output(self, extra_env: Mapping[str, str]) -> str:
        """Вывод основных команд (сохранён в файл ``GR_RUN_OUTPUT``)."""
        return Path(extra_env["GR_RUN_OUTPUT"]).read_text(encoding="utf-8")

    def _report(
        self,
        steps: _Steps,
        outcome: Outcome,
        *,
        tasks: tuple[Task, ...] = (),
        error: str = "",
        resolved: tuple[int, ...] = (),
    ) -> ScenarioReport:
        return ScenarioReport(
            scenario=self._scenario,
            outcome=outcome,
            run=steps.run,
            duration=time.monotonic() - self._started,
            validation=steps.validation,
            formatting=steps.formatting,
            tasks=tasks,
            error=error,
            resolved=resolved,
        )

    def _failed(self, steps: _Steps, error: str) -> ScenarioReport:
        return self._report(steps, Outcome.ERROR, error=error)


def _format_error(formatting: ProcessOutcome) -> str:
    """Текст ошибки шага форматирования."""
    if formatting.timed_out:
        return "форматирование не уложилось в таймаут"
    return f"форматирование вернуло код {formatting.exit_code}"
