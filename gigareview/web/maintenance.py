"""Обслуживание Web Dashboard: удаление старых прогонов и записей журнала."""

from datetime import datetime, timedelta
from typing import Final

from litestar import get, post
from litestar.params import FromQuery
from litestar.response import Redirect, Template

from gigareview.store.clock import utc_now
from gigareview.web.deps import AnyRequest, StoreDep, redirect_with

__all__ = ["DEFAULT_DAYS", "maintenance_page", "purge_events", "purge_runs"]

#: Срок хранения, который форма предлагает по умолчанию.
DEFAULT_DAYS: Final = 30
_PAGE: Final = "/maintenance"
_BAD_DAYS: Final = "Number of days must be a positive integer"


@get(_PAGE, name="maintenance", sync_to_thread=False)
def maintenance_page(
    store: StoreDep,
    notice: FromQuery[str] = "",
    error: FromQuery[str] = "",
) -> Template:
    """Очистка истории: сколько удалится при сроке по умолчанию и формы с выбором срока."""
    before = utc_now() - timedelta(days=DEFAULT_DAYS)
    return Template(
        template_name="maintenance.html",
        context={
            "notice": notice,
            "error": error,
            "default_days": DEFAULT_DAYS,
            "stale_runs": store.queries.stale(before),
            "stale_events": store.events.stale(before),
        },
    )


@post("/maintenance/purge-runs", name="purge_runs")
async def purge_runs(request: AnyRequest, store: StoreDep) -> Redirect:
    """Удалить завершённые прогоны старше заданного срока (кроме нужных демону)."""
    before = await _cutoff(request)
    if before is None:
        return redirect_with(_PAGE, error=_BAD_DAYS)
    deleted = store.queries.purge(before)
    return redirect_with(_PAGE, notice=f"Runs deleted: {deleted}")


@post("/maintenance/purge-events", name="purge_events")
async def purge_events(request: AnyRequest, store: StoreDep) -> Redirect:
    """Удалить записи журнала старше заданного срока."""
    before = await _cutoff(request)
    if before is None:
        return redirect_with(_PAGE, error=_BAD_DAYS)
    deleted = store.events.purge(before)
    return redirect_with(_PAGE, notice=f"Log entries deleted: {deleted}")


async def _cutoff(request: AnyRequest) -> datetime | None:
    """Момент «старше ``days`` дней» из формы; ``None`` — срок не целое положительное число."""
    form = await request.form()
    try:
        days = int(str(form.get("days", "")))
        before = utc_now() - timedelta(days=days)
    except (ValueError, OverflowError):
        return None
    return before if days >= 1 else None
