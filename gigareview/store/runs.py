"""Запись прогонов и результатов сценариев."""

import sqlite3
from dataclasses import asdict

from gigareview.statuses import Outcome, RunStatus
from gigareview.store.clock import stamp
from gigareview.store.records import (
    TASK_SEPARATOR,
    CommentKind,
    NewRun,
    RunRecord,
    ScenarioRecord,
)

__all__ = ["RunRepository"]

#: Сколько раз подряд автоматический прогон коммита может упасть с ``error``.
MAX_AUTO_ATTEMPTS = 3

_SETTLED = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PASSED,
    RunStatus.ISSUES,
    RunStatus.FAILED,
)


class RunRepository:
    """Таблицы ``runs`` и ``scenario_results``: создание, ход и итог прогона."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Репозиторий поверх соединения ``db``."""
        self._db = db

    def create(self, run: NewRun) -> int:
        """Ставит прогон в очередь (статус ``queued``) и возвращает его номер."""
        fields = asdict(run)
        fields["dry_run"] = int(run.dry_run)
        fields["status"] = RunStatus.QUEUED.value
        fields["created_at"] = stamp()
        columns = ", ".join(fields)
        marks = ", ".join(f":{name}" for name in fields)
        # В текст запроса подставляются только имена колонок и условия из кода.
        cursor = self._db.execute(f"INSERT INTO runs ({columns}) VALUES ({marks})", fields)  # noqa: S608
        return int(cursor.lastrowid or 0)

    def get(self, run_id: int) -> RunRecord | None:
        """Прогон по номеру."""
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else RunRecord.from_row(row)

    def mark_running(self, run_id: int) -> None:
        """Прогон начал выполняться."""
        self._db.execute(
            "UPDATE runs SET status = ?, started_at = ? WHERE id = ?",
            (RunStatus.RUNNING.value, stamp(), run_id),
        )

    def append_setup_log(self, run_id: int, text: str) -> None:
        """Дописывает вывод подготовки (git, init) в журнал прогона."""
        self._db.execute(
            "UPDATE runs SET setup_log = setup_log || ? WHERE id = ?",
            (text, run_id),
        )

    def finish(self, run_id: int, status: RunStatus, error: str = "") -> None:
        """Прогон завершился со статусом ``status`` (и текстом ошибки, если она была)."""
        self._db.execute(
            "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status.value, error, stamp(), run_id),
        )

    def add_scenario(self, run_id: int, position: int, scenario: ScenarioRecord) -> None:
        """Сохраняет результат сценария ``position`` (по порядку в прогоне)."""
        fields = asdict(scenario)
        fields["tasks"] = TASK_SEPARATOR.join(scenario.tasks)
        fields["run_id"] = run_id
        fields["position"] = position
        columns = ", ".join(fields)
        marks = ", ".join(f":{name}" for name in fields)
        self._db.execute(
            # В текст запроса подставляются только имена колонок и условия из кода.
            f"INSERT INTO scenario_results ({columns}) VALUES ({marks})",  # noqa: S608
            fields,
        )

    def scenarios(self, run_id: int) -> list[ScenarioRecord]:
        """Результаты сценариев прогона по порядку."""
        rows = self._db.execute(
            "SELECT * FROM scenario_results WHERE run_id = ? ORDER BY position",
            (run_id,),
        )
        return [ScenarioRecord.from_row(row) for row in rows]

    def should_skip(
        self,
        project: str,
        repo: str,
        pr_id: int,
        commit_id: str,
        *,
        dry_run: bool,
    ) -> bool:
        """Коммит уже проверен, проверяется или исчерпал попытки — опрос его не ставит.

        Прерванные прогоны не считаются; упавшие с ``error`` повторяются до
        :data:`MAX_AUTO_ATTEMPTS` раз (сбой сети или git может пройти сам). Вне
        сухого режима сухие прогоны не считаются: их результат в Bitbucket не попал.
        """
        rows = self._db.execute(
            "SELECT status FROM runs WHERE project = ? AND repo = ? AND pr_id = ? "
            "AND commit_id = ? AND (? OR dry_run = 0)",
            (project, repo, pr_id, commit_id, int(dry_run)),
        ).fetchall()
        statuses = [RunStatus(row["status"]) for row in rows]
        errors = statuses.count(RunStatus.ERROR)
        return errors >= MAX_AUTO_ATTEMPTS or any(status in _SETTLED for status in statuses)

    def last_reviewed_commit(
        self,
        project: str,
        repo: str,
        pr_id: int,
        scenario: str,
        *,
        other_than: str,
    ) -> str | None:
        """Последний коммит PR (не ``other_than``), чьё ревью сценарием попало в Bitbucket.

        Считаются только опубликованные прогоны (есть итоговый комментарий: не сухой режим
        и не «PR изменился во время прогона»), где сценарий отработал без ошибки.
        """
        row = self._db.execute(
            "SELECT runs.commit_id FROM runs "
            "JOIN scenario_results ON scenario_results.run_id = runs.id "
            "WHERE runs.project = ? AND runs.repo = ? AND runs.pr_id = ? "
            "AND runs.commit_id != ? AND scenario_results.name = ? "
            "AND scenario_results.outcome IN (?, ?) AND EXISTS ("
            "SELECT 1 FROM bb_comments WHERE bb_comments.run_id = runs.id "
            "AND bb_comments.kind = ?) ORDER BY runs.id DESC LIMIT 1",
            (
                project,
                repo,
                pr_id,
                other_than,
                scenario,
                Outcome.PASSED.value,
                Outcome.ISSUES.value,
                CommentKind.SUMMARY.value,
            ),
        ).fetchone()
        return None if row is None else str(row["commit_id"])

    def interrupt_active(self) -> int:
        """Прогоны, оставшиеся ``queued``/``running`` от прошлого запуска, — ``interrupted``."""
        cursor = self._db.execute(
            "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE status IN (?, ?)",
            (
                RunStatus.INTERRUPTED.value,
                stamp(),
                "Демон был остановлен до завершения прогона",
                RunStatus.QUEUED.value,
                RunStatus.RUNNING.value,
            ),
        )
        return cursor.rowcount
