"""Разбор аргументов и запуск команд gigareview."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

from gigareview.cli import ai_review, commands
from gigareview.config import Config, default_config_path, load_config
from gigareview.errors import GigaReviewError

__all__ = ["build_parser", "main"]

_EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    """Парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="gigareview",
        description="Автоматическое ревью pull request'ов Bitbucket сценариями на локальных копиях",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="Файл конфигурации (по умолчанию $GIGAREVIEW_CONFIG или ~/.gigareview/config.toml)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gigareview {metadata.version('gigareview')}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Запустить демон и Web Dashboard")
    serve.add_argument("--dry-run", action="store_true", help="Ничего не писать в Bitbucket")
    run = sub.add_parser("run", help="Однократно проверить PR (без демона)")
    run.add_argument("project", help="Ключ проекта")
    run.add_argument("repo", help="Slug репозитория")
    run.add_argument("pr_id", type=int, help="Номер PR")
    run.add_argument("--dry-run", action="store_true", help="Ничего не писать в Bitbucket")
    sub.add_parser("check-config", help="Проверить конфигурацию")
    init = sub.add_parser("init-config", help="Записать пример конфигурации")
    init.add_argument("--force", action="store_true", help="Перезаписать существующий файл")
    ai_review.add_parser(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа: код выхода 0 — успех, 1 — замечания/предупреждения, 2 — ошибка."""
    args = build_parser().parse_args(argv)
    if args.command == "ai-review":
        return ai_review.run(args)
    path: Path = args.config or default_config_path()
    try:
        if args.command == "init-config":
            return commands.init_config(path, force=args.force)
        config = _with_dry_run(load_config(path), dry_run=getattr(args, "dry_run", False))
        return _dispatch(args, config, path)
    except GigaReviewError as error:
        sys.stderr.write(f"Ошибка: {error}\n")
        return _EXIT_ERROR
    except KeyboardInterrupt:
        return 0


def _dispatch(args: argparse.Namespace, config: Config, path: Path) -> int:
    """Команда, которой нужна загруженная конфигурация."""
    if args.command == "check-config":
        return commands.check_config(config, path)
    if args.command == "run":
        return asyncio.run(commands.run_once(config, args.project, args.repo, args.pr_id))
    return asyncio.run(commands.serve(config, path))


def _with_dry_run(config: Config, *, dry_run: bool) -> Config:
    """Конфигурация с включённым сухим режимом, если он задан флагом."""
    if not dry_run:
        return config
    daemon = config.daemon.model_copy(update={"dry_run": True})
    return config.model_copy(update={"daemon": daemon})
