import tomllib
from pathlib import Path

import pytest

from gigareview.config import (
    SAMPLE_CONFIG,
    BitbucketSettings,
    Config,
    ScenarioKind,
    default_config_path,
    load_config,
    resolve_token,
)
from gigareview.errors import ConfigError


def _config(**extra: object) -> Config:
    raw: dict[str, object] = {"bitbucket": {"url": "https://bb"}, **extra}
    return Config.model_validate(raw)


def test_sample_config_is_valid() -> None:
    config = Config.model_validate(tomllib.loads(SAMPLE_CONFIG))
    repo = config.repos[0]
    assert [scenario.name for scenario in config.scenarios_for(repo)] == [
        "mypy",
        "ai-review",
        "pytest",
    ]
    assert repo.main_branch == "master"
    assert config.scenarios["mypy"].validate_script
    assert config.scenarios["mypy"].format_script


def test_scenarios_merge_excludes_and_overrides() -> None:
    config = _config(
        scenarios={"a": {"run": "a"}, "b": {"run": "b"}, "c": {"run": "c"}},
        repos=[
            {
                "project": "P",
                "slug": "r",
                "clone_url": "u",
                "exclude_scenarios": ["a"],
                "scenarios": {"c": {"run": "own"}, "d": {"run": "d"}},
            },
        ],
    )
    merged = config.scenarios_for(config.repos[0])
    assert [(scenario.name, scenario.run) for scenario in merged] == [
        ("b", "b"),
        ("c", "own"),
        ("d", "d"),
    ]


def test_fingerprint_follows_scenarios_and_init() -> None:
    repo = {"project": "P", "slug": "r", "clone_url": "u"}
    first = _config(scenarios={"a": {"run": "a"}}, repos=[repo])
    second = _config(scenarios={"a": {"run": "b"}}, repos=[repo])
    third = _config(scenarios={"a": {"run": "a"}}, repos=[{**repo, "init": "uv sync"}])
    prints = {config.fingerprint(config.repos[0]) for config in (first, second, third)}
    assert len(prints) == 3


def test_timeout_defaults_depend_on_kind() -> None:
    config = _config(
        scenarios={
            "simple": {"run": "x"},
            "ai": {"run": "x", "kind": "ai"},
            "custom": {"run": "x", "kind": "ai", "timeout_minutes": 0.5},
        },
    )
    assert config.scenarios["simple"].timeout_seconds == 180
    assert config.scenarios["ai"].kind is ScenarioKind.AI
    assert config.scenarios["ai"].timeout_seconds == 1200
    assert config.scenarios["custom"].timeout_seconds == 30
    assert config.scenarios["simple"].display_title == "simple"


def test_find_repo_is_case_insensitive() -> None:
    config = _config(repos=[{"project": "PROJ", "slug": "Repo", "clone_url": "u"}])
    assert config.find_repo("proj", "repo") is config.repos[0]
    assert config.find_repo("proj", "other") is None
    assert config.repos[0].key == "PROJ/Repo"


def test_duplicate_repo_is_rejected() -> None:
    repo = {"project": "P", "slug": "r", "clone_url": "u"}
    with pytest.raises(ValueError, match="дважды"):
        _config(repos=[repo, {**repo, "project": "p"}])


def test_unknown_exclude_is_rejected() -> None:
    with pytest.raises(ValueError, match="nope"):
        _config(
            repos=[{"project": "P", "slug": "r", "clone_url": "u", "exclude_scenarios": ["nope"]}],
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://bb.example.com",
        "http://127.0.0.1:7990",
        "http://localhost:7990",
        "http://[::1]:7990",
    ],
)
def test_bitbucket_url_accepts_https_and_local_http(url: str) -> None:
    assert _config(bitbucket={"url": url}).bitbucket.url == url


@pytest.mark.parametrize("url", ["http://bb.example.com", "ftp://bb.example.com"])
def test_bitbucket_url_rejects_plain_http(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        _config(bitbucket={"url": url})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_dashboard_host_accepts_loopback(host: str) -> None:
    assert _config(dashboard={"host": host}).dashboard.host == host


@pytest.mark.parametrize("host", ["10.0.0.5", "box.lan", "192.168.1.10"])
def test_dashboard_host_rejects_network(host: str) -> None:
    with pytest.raises(ValueError, match=r"dashboard\.token"):
        _config(dashboard={"host": host})


def test_dashboard_host_rejects_empty() -> None:
    with pytest.raises(ValueError, match="host"):
        _config(dashboard={"host": ""})


def test_dashboard_network_host_with_token() -> None:
    dashboard = _config(dashboard={"host": "192.168.1.10", "token": "t" * 16}).dashboard
    assert dashboard.host == "192.168.1.10"
    assert dashboard.token.get_secret_value() == "t" * 16
    assert "t" * 16 not in repr(dashboard)


def test_dashboard_token_too_short() -> None:
    with pytest.raises(ValueError, match="16"):
        _config(dashboard={"host": "192.168.1.10", "token": "short"})


def test_dashboard_base_url() -> None:
    assert _config().dashboard.base_url == "http://127.0.0.1:8765"
    assert _config(dashboard={"public_url": "http://box:1/"}).dashboard.base_url == "http://box:1"
    assert _config(dashboard={"host": "::1"}).dashboard.base_url == "http://[::1]:8765"
    assert _config(dashboard={"host": "[::1]"}).dashboard.base_url == "http://[::1]:8765"


def test_daemon_settings_helpers() -> None:
    config = _config(daemon={"workdir": "~/gr", "interval_minutes": 2})
    assert config.daemon.home == Path("~/gr").expanduser()
    assert config.daemon.interval_seconds == 120


def test_load_config_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    assert load_config(path).bitbucket.url == "https://bitbucket.example.com"


def test_load_config_missing_file_suggests_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="init-config"):
        load_config(tmp_path / "missing.toml")


def test_load_config_bad_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[daemon\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(path)


def test_load_config_unreadable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="прочитать"):
        load_config(tmp_path)


def test_load_config_reports_field_paths(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[bitbucket]\nurl = "x"\n[daemon]\ninterval_minutes = 0\nbogus = 1\n')
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert "daemon.interval_minutes" in str(caught.value)
    assert "daemon.bogus" in str(caught.value)


def test_resolve_token(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = BitbucketSettings(url="https://bb", token_env="GR_TOKEN_UNDER_TEST")
    monkeypatch.delenv("GR_TOKEN_UNDER_TEST", raising=False)
    with pytest.raises(ConfigError, match="GR_TOKEN_UNDER_TEST"):
        resolve_token(settings)
    monkeypatch.setenv("GR_TOKEN_UNDER_TEST", " secret ")
    assert resolve_token(settings) == "secret"


def test_default_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GIGAREVIEW_CONFIG", raising=False)
    assert default_config_path() == Path("~/.gigareview/config.toml").expanduser()
    monkeypatch.setenv("GIGAREVIEW_CONFIG", str(tmp_path / "c.toml"))
    assert default_config_path() == tmp_path / "c.toml"


def test_incremental_only_for_ai_scenarios() -> None:
    config = _config(scenarios={"ai": {"run": "x", "kind": "ai", "incremental": True}})
    assert config.scenarios["ai"].incremental
    with pytest.raises(ValueError, match="incremental"):
        _config(scenarios={"plain": {"run": "x", "incremental": True}})


def test_sample_has_incremental_ai_review() -> None:
    config = Config.model_validate(tomllib.loads(SAMPLE_CONFIG))
    review = config.scenarios["ai-review"]
    assert review.kind is ScenarioKind.AI
    assert review.incremental
    assert "gigacrew --mode plan" in review.run
