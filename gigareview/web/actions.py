"""Действия Web Dashboard (POST, под CSRF-защитой): опрос, пауза, повтор, перечитать конфиг."""

from litestar import post
from litestar.params import FromPath
from litestar.response import Redirect

from gigareview.daemon import Daemon
from gigareview.errors import GigaReviewError
from gigareview.web.deps import AnyRequest, DaemonDep, StoreDep, back, redirect_with

__all__ = ["pause", "poll_now", "reload_config", "rerun_pr", "rerun_run", "resume"]


@post("/actions/poll", name="poll_now", sync_to_thread=False)
def poll_now(request: AnyRequest, daemon: DaemonDep) -> Redirect:
    """Опросить Bitbucket сейчас."""
    daemon.poll_now()
    return redirect_with(back(request), notice="Опрос Bitbucket запущен")


@post("/actions/pause", name="pause", sync_to_thread=False)
def pause(request: AnyRequest, daemon: DaemonDep) -> Redirect:
    """Приостановить опрос."""
    daemon.set_paused(paused=True)
    return redirect_with(back(request), notice="Опрос приостановлен")


@post("/actions/resume", name="resume", sync_to_thread=False)
def resume(request: AnyRequest, daemon: DaemonDep) -> Redirect:
    """Возобновить опрос."""
    daemon.set_paused(paused=False)
    return redirect_with(back(request), notice="Опрос возобновлён")


@post("/actions/reload", name="reload_config", sync_to_thread=False)
def reload_config(request: AnyRequest, daemon: DaemonDep) -> Redirect:
    """Перечитать файл конфигурации."""
    try:
        daemon.reload_config()
    except GigaReviewError as error:
        return redirect_with(back(request), error=str(error))
    return redirect_with(back(request), notice="Конфигурация перечитана")


@post("/runs/{run_id:int}/rerun", name="rerun_run")
async def rerun_run(
    request: AnyRequest,
    daemon: DaemonDep,
    store: StoreDep,
    run_id: FromPath[int],
) -> Redirect:
    """Повторить проверку PR этого прогона на текущем коммите PR."""
    run = store.runs.get(run_id)
    if run is None:
        return redirect_with(back(request), error=f"Прогон #{run_id} не найден")
    return await _rerun(request, daemon, run.project, run.repo, run.pr_id)


@post("/prs/{project:str}/{repo:str}/{pr_id:int}/rerun", name="rerun_pr")
async def rerun_pr(
    request: AnyRequest,
    daemon: DaemonDep,
    project: FromPath[str],
    repo: FromPath[str],
    pr_id: FromPath[int],
) -> Redirect:
    """Повторить проверку PR."""
    return await _rerun(request, daemon, project, repo, pr_id)


async def _rerun(
    request: AnyRequest,
    daemon: Daemon,
    project: str,
    repo: str,
    pr_id: int,
) -> Redirect:
    """Ставит PR в очередь и открывает новый прогон (ошибка — назад с сообщением)."""
    try:
        run_id = await daemon.rerun(project, repo, pr_id)
    except GigaReviewError as error:
        return redirect_with(back(request), error=str(error))
    return redirect_with(f"/runs/{run_id}", notice="Проверка поставлена в очередь")
