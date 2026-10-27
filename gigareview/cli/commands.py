"""Команды: демон с Web Dashboard, однократный прогон, проверка и пример конфигурации."""

import contextlib
import sys
from pathlib import Path
from types import MappingProxyType

import uvicorn

from gigareview.bitbucket import BitbucketClient
from gigareview.config import SAMPLE_CONFIG, Config, resolve_token
from gigareview.daemon import Daemon
from gigareview.errors import ConfigError
from gigareview.pipeline import Reviewer, new_run
from gigareview.statuses import RunStatus, Trigger
from gigareview.store import Store
from gigareview.web import create_app

__all__ = ["check_config", "database_path", "init_config", "run_once", "serve"]

#: Код выхода ``gigareview run`` по статусу прогона (остальные статусы — 2).
_RUN_EXIT_CODES = MappingProxyType({RunStatus.PASSED: 0, RunStatus.ISSUES: 1})


def database_path(config: Config) -> Path:
    """Файл базы демона."""
    return config.daemon.home / "gigareview.sqlite3"


def _client(config: Config) -> BitbucketClient:
    """Клиент Bitbucket по конфигурации (токен — из окружения)."""
    return BitbucketClient(
        config.bitbucket.url,
        resolve_token(config.bitbucket),
        user=config.bitbucket.user,
    )


async def serve(config: Config, config_path: Path) -> int:
    """Демон и Web Dashboard в одном процессе до Ctrl+C."""
    dashboard = config.dashboard
    async with contextlib.AsyncExitStack() as stack:
        store = stack.enter_context(Store(database_path(config)))
        client = await stack.enter_async_context(_client(config))
        daemon = Daemon(config, store, client, config_path=config_path)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(daemon, store),
                host=dashboard.host,
                port=dashboard.port,
                log_level="warning",
            ),
        )
        _say(f"Web Dashboard: {dashboard.base_url}")
        await daemon.start()
        stack.push_async_callback(daemon.stop)
        await server.serve()
    return 0


async def run_once(config: Config, project: str, repo: str, pr_id: int) -> int:
    """Однократный прогон PR без демона; код выхода — 0, 1 (замечания) или 2."""
    repo_config = config.find_repo(project, repo)
    if repo_config is None:
        raise ConfigError(f"Репозиторий {project}/{repo} не описан в конфигурации")
    with Store(database_path(config)) as store:
        async with _client(config) as client:
            pr = await client.get_pull_request(project, repo, pr_id)
            run = new_run(
                pr,
                Trigger.CLI,
                fingerprint=config.fingerprint(repo_config),
                dry_run=config.daemon.dry_run,
            )
            run_id = store.runs.create(run)
            status = await Reviewer(config, store, client).review(run_id, pr)
            record = store.runs.get(run_id)
            _say(f"Прогон #{run_id}: {status}")
            for scenario in store.runs.scenarios(run_id):
                _say(f"  {scenario.outcome:<7} {scenario.title} {scenario.error}".rstrip())
            if record is not None and record.error:
                _say(record.error)
    return _RUN_EXIT_CODES.get(status, 2)


def check_config(config: Config, config_path: Path) -> int:
    """Сводка конфигурации и наличие токена."""
    _say(f"Конфигурация {config_path} в порядке")
    bitbucket = config.bitbucket
    _say(f"Bitbucket: {bitbucket.url}")
    _say(f"Общих сценариев: {len(config.scenarios)}")
    for repo in config.repos:
        names = ", ".join(scenario.name for scenario in config.scenarios_for(repo)) or "—"
        _say(f"  {repo.key}: {names}")
    try:
        resolve_token(bitbucket)
    except ConfigError as error:
        _say(f"Внимание: {error}")
        return 1
    return 0


def init_config(path: Path, *, force: bool) -> int:
    """Записывает пример конфигурации (существующий файл — только с ``force``)."""
    if path.exists() and not force:
        raise ConfigError(f"{path} уже существует: добавьте --force, чтобы перезаписать")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    _say(f"Пример конфигурации записан в {path}")
    return 0


def _say(line: str) -> None:
    """Строка в stdout."""
    sys.stdout.write(f"{line}\n")
