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

    Переносятся сценарии, отработавшие без ошибки и до конца: прерванное ревью (``partial``)
    проверило не всё и выполняется заново. Пусто — выполняются все сценарии: прогон
    не перезапуск, базовый прогон не опубликован (упал целиком) или проверял другой коммит,
    конфигурация сценариев изменилась или упавших сценариев нет.
    """
    if run.base_run_id is None:
        return ()
    base = store.runs.get(run.base_run_id)
    if base is None or not base.published or base.commit_id != run.commit_id:
        return ()
    if base.fingerprint != run.fingerprint:
        return ()
    known = {scenario.name: scenario for scenario in scenarios}
    reports = tuple(
        _report(known[record.name], record, base.id)
        for record in store.runs.scenarios(base.id)
        if record.name in known and _settled(record)
    )
    return () if len(reports) == len(known) else reports


def _settled(record: ScenarioRecord) -> bool:
    """Итог сценария окончательный: без ошибки и ревью не прервано."""
    return record.outcome is not Outcome.ERROR and not record.partial


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
