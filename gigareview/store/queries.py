"""Выборки прогонов для Web Dashboard: фильтры, пагинация, сводка, список PR."""

import sqlite3
from datetime import datetime

from gigareview.statuses import RunStatus
from gigareview.store.clock import stamp
from gigareview.store.records import PullRequestSummary, RunFilter, RunRecord, RunStats

__all__ = ["RunQueries"]


class RunQueries:
    """Чтение таблицы ``runs`` для страниц Web Dashboard."""

    def __init__(self, db: sqlite3.Connection) -> None:
        """Выборки поверх соединения ``db``."""
        self._db = db

    def runs(self, run_filter: RunFilter, *, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        """Прогоны под фильтром, новые сначала."""
        where, args = _where(run_filter)
        rows = self._db.execute(
            # В текст запроса подставляются только имена колонок и условия из кода.
            f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
            (*args, limit, offset),
        )
        return [RunRecord.from_row(row) for row in rows]

    def count(self, run_filter: RunFilter) -> int:
        """Число прогонов под фильтром."""
        where, args = _where(run_filter)
        # В текст запроса подставляются только имена колонок и условия из кода.
        row = self._db.execute(f"SELECT count(*) FROM runs {where}", args).fetchone()  # noqa: S608
        return int(row[0])

    def stats(self, since: datetime | None = None) -> RunStats:
        """Число прогонов по статусам и средняя длительность (с момента ``since``)."""
        where, args = _where(RunFilter(since=since))
        rows = self._db.execute(
            # В текст запроса подставляются только имена колонок и условия из кода.
            f"SELECT status, count(*) AS total FROM runs {where} GROUP BY status",  # noqa: S608
            args,
        )
        by_status = {RunStatus(row["status"]): int(row["total"]) for row in rows}
        joiner = "AND" if where else "WHERE"
        average = self._db.execute(
            # В текст запроса подставляются только имена колонок и условия из кода.
            "SELECT avg((julianday(finished_at) - julianday(started_at)) * 86400) "  # noqa: S608
            f"FROM runs {where} {joiner} started_at IS NOT NULL "
            "AND finished_at IS NOT NULL",
            args,
        ).fetchone()[0]
        return RunStats(by_status=by_status, average_duration=average)

    def pull_requests(self, *, limit: int = 100) -> list[PullRequestSummary]:
        """PR с прогонами: последний прогон каждого и их число, недавние сначала."""
        rows = self._db.execute(
            "SELECT runs.*, grouped.total AS runs_count FROM runs JOIN ("
            "SELECT max(id) AS last_id, count(*) AS total FROM runs "
            "GROUP BY project, repo, pr_id) AS grouped ON runs.id = grouped.last_id "
            "ORDER BY runs.id DESC LIMIT ?",
            (limit,),
        )
        summaries = []
        for row in rows:
            fields = dict(row)
            runs_count = int(fields.pop("runs_count"))
            summaries.append(PullRequestSummary(RunRecord.from_row(fields), runs_count))
        return summaries

    def repos(self) -> list[str]:
        """Репозитории (``PROJECT/slug``), по которым были прогоны."""
        rows = self._db.execute(
            "SELECT DISTINCT project || '/' || repo AS repo_key FROM runs ORDER BY repo_key",
        )
        return [row["repo_key"] for row in rows]

    def scenario_names(self) -> list[str]:
        """Имена сценариев, которые встречались в прогонах."""
        rows = self._db.execute("SELECT DISTINCT name FROM scenario_results ORDER BY name")
        return [row["name"] for row in rows]


def _where(run_filter: RunFilter) -> tuple[str, tuple[object, ...]]:
    """Условие ``WHERE`` фильтра прогонов и его аргументы."""
    conditions: list[str] = []
    args: list[object] = []
    if run_filter.status is not None:
        conditions.append("status = ?")
        args.append(run_filter.status.value)
    if run_filter.repo:
        conditions.append("project || '/' || repo = ?")
        args.append(run_filter.repo)
    if run_filter.pr_id is not None:
        conditions.append("pr_id = ?")
        args.append(run_filter.pr_id)
    if run_filter.since is not None:
        conditions.append("created_at >= ?")
        args.append(stamp(run_filter.since))
    if run_filter.scenario or run_filter.outcome is not None:
        conditions.append(_scenario_condition(run_filter, args))
    if not conditions:
        return "", ()
    joined = " AND ".join(conditions)
    return f"WHERE {joined}", tuple(args)


def _scenario_condition(run_filter: RunFilter, args: list[object]) -> str:
    """Подусловие «в прогоне есть сценарий с таким именем и/или исходом»."""
    parts = ["scenario_results.run_id = runs.id"]
    if run_filter.scenario:
        parts.append("scenario_results.name = ?")
        args.append(run_filter.scenario)
    if run_filter.outcome is not None:
        parts.append("scenario_results.outcome = ?")
        args.append(run_filter.outcome.value)
    joined = " AND ".join(parts)
    # В текст запроса подставляются только условия из кода, значения — параметрами.
    return f"EXISTS (SELECT 1 FROM scenario_results WHERE {joined})"  # noqa: S608
