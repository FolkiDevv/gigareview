"""Модели конфигурации: демон, Bitbucket, Web Dashboard, сценарии и репозитории."""

import hashlib
import ipaddress
import json
from enum import StrEnum, unique
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BitbucketSettings",
    "Config",
    "DaemonSettings",
    "DashboardSettings",
    "RepoConfig",
    "Scenario",
    "ScenarioKind",
]

_SIMPLE_TIMEOUT_MINUTES = 3
_AI_TIMEOUT_MINUTES = 20
_DEFAULT_PORT = 8765
_SECONDS_IN_MINUTE = 60
_MAX_PORT = 65535
_FINGERPRINT_CHARS = 16


class _Model(BaseModel):
    """Общие настройки моделей: неизменяемые, лишние ключи — ошибка (опечатка в TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


@unique
class ScenarioKind(StrEnum):
    """Вид сценария: обычный (одна таска) или ИИ (JSON-список тасок)."""

    SIMPLE = "simple"
    AI = "ai"


class Scenario(_Model):
    """Сценарий: основные команды, валидация результата и форматирование текста таски."""

    name: str = ""
    title: str = ""
    run: str = Field(min_length=1)
    validate_script: str = Field(default="", alias="validate")
    format_script: str = Field(default="", alias="format")
    kind: ScenarioKind = ScenarioKind.SIMPLE
    timeout_minutes: float | None = Field(default=None, gt=0)

    @property
    def display_title(self) -> str:
        """Заголовок для таски и отчёта: ``title``, иначе имя сценария."""
        return self.title or self.name

    @property
    def timeout_seconds(self) -> float:
        """Таймаут всего сценария: заданный или по виду (3 минуты, ИИ — 20)."""
        minutes = self.timeout_minutes
        if minutes is None:
            is_ai = self.kind is ScenarioKind.AI
            minutes = _AI_TIMEOUT_MINUTES if is_ai else _SIMPLE_TIMEOUT_MINUTES
        return minutes * _SECONDS_IN_MINUTE


def _is_loopback(host: str | None) -> bool:
    """``host`` — локальный адрес (localhost или loopback-IP)."""
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _named(scenarios: dict[str, Scenario]) -> dict[str, Scenario]:
    """Проставляет сценариям имена — ключи таблицы ``[scenarios.<имя>]``."""
    return {
        name: scenario.model_copy(update={"name": name}) for name, scenario in scenarios.items()
    }


class RepoConfig(_Model):
    """Репозиторий под наблюдением: где клонировать, как готовить и какие сценарии гонять."""

    project: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    clone_url: str = Field(min_length=1)
    main_branch: str = "master"
    init: str = ""
    exclude_scenarios: tuple[str, ...] = ()
    scenarios: dict[str, Scenario] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """``PROJECT/slug`` — ключ репозитория в журнале и Web Dashboard."""
        return f"{self.project}/{self.slug}"

    def matches(self, project: str, slug: str) -> bool:
        """Это репозиторий ``project/slug`` (без учёта регистра, как в Bitbucket)."""
        return self.project.casefold() == project.casefold() and (
            self.slug.casefold() == slug.casefold()
        )

    @field_validator("scenarios", mode="after")
    @classmethod
    def _name_scenarios(cls, scenarios: dict[str, Scenario]) -> dict[str, Scenario]:
        return _named(scenarios)


class DaemonSettings(_Model):
    """Настройки демона: период опроса, рабочий каталог, параллелизм, «сухой» режим."""

    interval_minutes: float = Field(default=1, gt=0)
    workdir: Path = Path("~/.gigareview")
    max_parallel: int = Field(default=1, ge=1)
    dry_run: bool = False

    @property
    def home(self) -> Path:
        """Рабочий каталог с раскрытой ``~``."""
        return self.workdir.expanduser()

    @property
    def interval_seconds(self) -> float:
        """Период опроса Bitbucket в секундах."""
        return self.interval_minutes * _SECONDS_IN_MINUTE


class BitbucketSettings(_Model):
    """Адрес Bitbucket, имя переменной окружения с токеном и slug пользователя."""

    url: str = Field(min_length=1)
    # Имя переменной окружения, а не сам токен.
    token_env: str = "BITBUCKET_TOKEN"  # noqa: S105
    user: str = ""

    @field_validator("url", mode="after")
    @classmethod
    def _require_https(cls, url: str) -> str:
        parts = urlsplit(url)
        # Токен уходит в заголовке Authorization: по HTTP — только на свою машину.
        if parts.scheme == "https" or (parts.scheme == "http" and _is_loopback(parts.hostname)):
            return url
        raise ValueError("нужен адрес https:// (http:// — только для localhost)")


class DashboardSettings(_Model):
    """Адрес Web Dashboard; ``public_url`` — для ссылок в комментариях Bitbucket."""

    host: str = "127.0.0.1"
    port: int = Field(default=_DEFAULT_PORT, ge=1, le=_MAX_PORT)
    public_url: str = ""

    @property
    def base_url(self) -> str:
        """Адрес для ссылок: ``public_url`` или ``http://host:port``."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (self.public_url or f"http://{host}:{self.port}").rstrip("/")

    @field_validator("host", mode="after")
    @classmethod
    def _require_loopback(cls, host: str) -> str:
        # У Web Dashboard нет входа: выводы сценариев и кнопки видны всем, кто достучится.
        if _is_loopback(host):
            return host.strip("[]")
        raise ValueError("Web Dashboard без аутентификации слушает только localhost")


class Config(_Model):
    """Вся конфигурация gigareview."""

    bitbucket: BitbucketSettings
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    scenarios: dict[str, Scenario] = Field(default_factory=dict)
    repos: tuple[RepoConfig, ...] = ()

    def find_repo(self, project: str, slug: str) -> RepoConfig | None:
        """Описание репозитория ``project/slug`` или ``None``, если он не наблюдается."""
        return next((repo for repo in self.repos if repo.matches(project, slug)), None)

    def scenarios_for(self, repo: RepoConfig) -> tuple[Scenario, ...]:
        """Сценарии репозитория: общие (кроме исключённых), затем свои; свои замещают общие."""
        merged = {
            name: scenario
            for name, scenario in self.scenarios.items()
            if name not in repo.exclude_scenarios
        }
        merged.update(repo.scenarios)
        return tuple(merged.values())

    def fingerprint(self, repo: RepoConfig) -> str:
        """Отпечаток всего, что влияет на прогон репозитория: init и сценарии."""
        payload = {
            "init": repo.init,
            "scenarios": [scenario.model_dump() for scenario in self.scenarios_for(repo)],
        }
        dumped = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(dumped.encode()).hexdigest()[:_FINGERPRINT_CHARS]

    @field_validator("scenarios", mode="after")
    @classmethod
    def _name_scenarios(cls, scenarios: dict[str, Scenario]) -> dict[str, Scenario]:
        return _named(scenarios)

    @model_validator(mode="after")
    def _check_repos(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for repo in self.repos:
            repo_id = (repo.project.casefold(), repo.slug.casefold())
            if repo_id in seen:
                raise ValueError(f"репозиторий {repo.key} описан дважды")
            seen.add(repo_id)
            unknown = sorted(set(repo.exclude_scenarios) - self.scenarios.keys())
            if unknown:
                missing = ", ".join(unknown)
                raise ValueError(f"{repo.key}: exclude_scenarios — нет общих сценариев {missing}")
        return self
