"""Перезапуск упавших сценариев: итоги остальных переносятся из базового прогона."""

from collections.abc import Sequence

from gigareview.config import Scenario
from gigareview.process import ProcessOutcome
from gigareview.scenarios import ScenarioReport, Task
from gigareview.statuses import Outcome
from gigareview.store import RunRecord, ScenarioRecord, Store

__all__ = ["reused_reports"]


def reused_reports(
    store: Store,
    run: RunRecord,
    scenarios: Sequence[Scenario],
) -> tuple[ScenarioReport, ...]:
    """Итоги сценариев, которые прогон ``run`` берёт из базового прогона без выполнения.

    Переносятся сценарии, отработавшие без ошибки. Пусто — выполняются все сценарии: прогон
    не перезапуск, базовый прогон не опубликован (упал целиком), конфигурация сценариев
    изменилась или упавших сценариев нет.
    """
    if run.base_run_id is None:
        return ()
    base = store.runs.get(run.base_run_id)
    if base is None or not base.published or base.fingerprint != run.fingerprint:
        return ()
    known = {scenario.name: scenario for scenario in scenarios}
    reports = tuple(
        _report(known[record.name], record, base.id)
        for record in store.runs.scenarios(base.id)
        if record.name in known and record.outcome is not Outcome.ERROR
    )
    return () if len(reports) == len(known) else reports


def _report(scenario: Scenario, record: ScenarioRecord, base_id: int) -> ScenarioReport:
    """Итог сценария из записи базового прогона (номер — прогона, где он выполнялся)."""
    return ScenarioReport(
        scenario=scenario,
        outcome=record.outcome,
        run=ProcessOutcome(record.run_exit, record.run_output, "", record.duration),
        duration=record.duration,
        validation=_step(record.validate_exit, record.validate_output),
        formatting=_step(record.format_exit, record.format_output),
        tasks=tuple(Task(text) for text in record.tasks),
        error=record.error,
        partial=record.partial,
        reused_from=record.reused_from or base_id,
    )


def _step(exit_code: int | None, output: str) -> ProcessOutcome | None:
    """Шаг сценария из записи; не выполнялся — ``None``."""
    if exit_code is None and not output:
        return None
    return ProcessOutcome(exit_code, output, "", 0)
