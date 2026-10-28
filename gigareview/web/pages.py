"""Страницы Web Dashboard (GET)."""

from datetime import timedelta
from typing import Any

from litestar import get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath, FromQuery
from litestar.response import Redirect, Template

from gigareview.statuses import EventLevel, Outcome, RunStatus
from gigareview.store import RunFilter
from gigareview.store.clock import utc_now
from gigareview.web.deps import PAGE_SIZE, DaemonDep, StoreDep

__all__ = [
    "api_status",
    "config_page",
    "events_page",
    "favicon",
    "overview",
    "pull_requests_page",
    "run_page",
    "runs_page",
]

_RECENT = 10
_DAY = timedelta(days=1)


@get("/favicon.ico", include_in_schema=False, sync_to_thread=False)
def favicon() -> Redirect:
    """Иконка для браузеров, которые запрашивают ``/favicon.ico`` без ``<link rel=icon>``."""
    return Redirect("/static/favicon.svg")


@get("/", name="overview", sync_to_thread=False)
def overview(
    daemon: DaemonDep,
    store: StoreDep,
    notice: FromQuery[str] = "",
    error: FromQuery[str] = "",
) -> Template:
    """Обзор: состояние демона, сводка за сутки и всего, последние прогоны и ошибки."""
    return _page(
        "overview.html",
        notice=notice,
        error=error,
        status=daemon.status(),
        day=store.queries.stats(since=utc_now() - _DAY),
        total=store.queries.stats(),
        runs=store.queries.runs(RunFilter(), limit=_RECENT),
        errors=store.events.list(EventLevel.ERROR, limit=_RECENT // 2),
    )


@get("/runs", name="runs", sync_to_thread=False)
def runs_page(
    store: StoreDep,
    status: FromQuery[RunStatus | None] = None,
    repo: FromQuery[str] = "",
    pr: FromQuery[int | None] = None,
    scenario: FromQuery[str] = "",
    outcome: FromQuery[Outcome | None] = None,
    page: FromQuery[int] = 1,
) -> Template:
    """Журнал прогонов с фильтрами и пагинацией."""
    run_filter = RunFilter(status=status, repo=repo, pr_id=pr, scenario=scenario, outcome=outcome)
    page = max(page, 1)
    total = store.queries.count(run_filter)
    return _page(
        "runs.html",
        runs=store.queries.runs(run_filter, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE),
        total=total,
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        run_filter=run_filter,
        statuses=list(RunStatus),
        outcomes=list(Outcome),
        repos=store.queries.repos(),
        scenarios=store.queries.scenario_names(),
    )


@get("/runs/{run_id:int}", name="run", sync_to_thread=False)
def run_page(
    store: StoreDep,
    run_id: FromPath[int],
    notice: FromQuery[str] = "",
    error: FromQuery[str] = "",
) -> Template:
    """Прогон: подготовка, каждый сценарий с полным выводом, комментарии в Bitbucket."""
    run = store.runs.get(run_id)
    if run is None:
        raise NotFoundException(f"Run #{run_id} not found")
    return _page(
        "run.html",
        notice=notice,
        error=error,
        run=run,
        scenarios=store.runs.scenarios(run_id),
        comments=store.comments.for_run(run_id),
    )


@get("/prs", name="prs", sync_to_thread=False)
def pull_requests_page(
    store: StoreDep,
    notice: FromQuery[str] = "",
    error: FromQuery[str] = "",
) -> Template:
    """PR, по которым были прогоны: последний результат и повтор проверки."""
    return _page("prs.html", notice=notice, error=error, prs=store.queries.pull_requests())


@get("/events", name="events", sync_to_thread=False)
def events_page(
    store: StoreDep,
    level: FromQuery[EventLevel | None] = None,
    page: FromQuery[int] = 1,
) -> Template:
    """Журнал демона."""
    page = max(page, 1)
    total = store.events.count(level)
    return _page(
        "events.html",
        events=store.events.list(level, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE),
        level=level,
        levels=list(EventLevel),
        page=page,
        pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
    )


@get("/config", name="config", sync_to_thread=False)
def config_page(
    daemon: DaemonDep,
    notice: FromQuery[str] = "",
    error: FromQuery[str] = "",
) -> Template:
    """Текущая конфигурация (токен не показывается — только имя переменной)."""
    config = daemon.config
    repos = [(repo, config.scenarios_for(repo)) for repo in config.repos]
    return _page("config.html", notice=notice, error=error, config=config, repos=repos)


@get("/api/status", name="api_status", sync_to_thread=False)
def api_status(daemon: DaemonDep, store: StoreDep) -> dict[str, Any]:
    """Состояние демона и сводка в JSON (для скриптов и мониторинга)."""
    status = daemon.status()
    stats = store.queries.stats()
    return {
        "running": status.running,
        "paused": status.paused,
        "last_poll": status.last_poll,
        "next_poll": status.next_poll,
        "last_error": status.last_error,
        "queue_size": status.queue_size,
        "active_runs": list(status.active_runs),
        "runs": {str(key): count for key, count in stats.by_status.items()},
    }


def _page(template_name: str, **context: Any) -> Template:
    """Ответ-шаблон."""
    return Template(template_name=template_name, context=context)
