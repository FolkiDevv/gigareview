"""Сборка приложения Litestar: маршруты, шаблоны, статика, CSRF."""

import secrets
from pathlib import Path

from litestar import Litestar
from litestar.config.csrf import CSRFConfig
from litestar.datastructures import State
from litestar.di import Provide
from litestar.logging.config import LoggingConfig
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from gigareview.daemon import Daemon
from gigareview.store import Store
from gigareview.web import actions, pages
from gigareview.web.formatting import FILTERS

__all__ = ["create_app"]

_HERE = Path(__file__).parent


def create_app(daemon: Daemon, store: Store, *, csrf_secret: str = "") -> Litestar:
    """Приложение Web Dashboard над демоном ``daemon`` и базой ``store``."""
    return Litestar(
        route_handlers=[
            pages.overview,
            pages.runs_page,
            pages.run_page,
            pages.pull_requests_page,
            pages.events_page,
            pages.config_page,
            pages.api_status,
            actions.poll_now,
            actions.pause,
            actions.resume,
            actions.reload_config,
            actions.rerun_run,
            actions.rerun_pr,
            create_static_files_router(path="/static", directories=[_HERE / "static"]),
        ],
        dependencies={
            "daemon": Provide(_provide_daemon, sync_to_thread=False),
            "store": Provide(_provide_store, sync_to_thread=False),
        },
        state=State({"daemon": daemon, "store": store}),
        template_config=TemplateConfig(
            directory=_HERE / "templates",
            engine=JinjaTemplateEngine,
            engine_callback=_register_filters,
        ),
        # По умолчанию Litestar пишет в консоль всё от INFO (каждый запрос httpx).
        logging_config=LoggingConfig(
            root={"level": "WARNING", "handlers": ["queue_listener"]},
            log_exceptions="always",
        ),
        csrf_config=CSRFConfig(secret=csrf_secret or secrets.token_urlsafe()),
    )


def _provide_daemon(state: State) -> Daemon:
    """Демон из состояния приложения."""
    daemon: Daemon = state.daemon
    return daemon


def _provide_store(state: State) -> Store:
    """База из состояния приложения."""
    store: Store = state.store
    return store


def _register_filters(engine: JinjaTemplateEngine) -> None:
    """Фильтры времени, длительности и подписей статусов."""
    engine.engine.filters.update(FILTERS)
