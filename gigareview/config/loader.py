"""Чтение конфигурации из TOML-файла и токена Bitbucket из окружения."""

import os
import tomllib
from pathlib import Path

from pydantic import ValidationError

from gigareview.config.models import BitbucketSettings, Config
from gigareview.errors import ConfigError

__all__ = ["default_config_path", "load_config", "resolve_token"]

#: Переменная окружения с путём к конфигурации (иначе ``~/.gigareview/config.toml``).
CONFIG_ENV = "GIGAREVIEW_CONFIG"


def default_config_path() -> Path:
    """Путь к конфигурации: ``$GIGAREVIEW_CONFIG`` или ``~/.gigareview/config.toml``."""
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return Path("~/.gigareview/config.toml").expanduser()


def load_config(path: Path) -> Config:
    """Читает и проверяет конфигурацию; любая проблема — :class:`ConfigError` с пояснением."""
    raw = _read_toml(path)
    try:
        return Config.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(f"{path}: неверная конфигурация\n{_describe(error)}") from error


def _read_toml(path: Path) -> dict[str, object]:
    """Разобранный TOML-файл; файла нет или он не разбирается — :class:`ConfigError`."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"Конфигурация {path} не найдена: создайте её командой `gigareview init-config`",
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Не удалось прочитать {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: ошибка TOML: {error}") from error


def resolve_token(settings: BitbucketSettings) -> str:
    """Токен Bitbucket из переменной окружения ``settings.token_env``."""
    token = os.environ.get(settings.token_env, "").strip()
    if not token:
        raise ConfigError(f"Не задан токен Bitbucket: переменная окружения {settings.token_env}")
    return token


def _describe(error: ValidationError) -> str:
    """Ошибки pydantic построчно: ``путь.к.полю: сообщение``."""
    lines = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "<корень>"
        lines.append(f"  {location}: {issue['msg']}")
    return "\n".join(lines)
