from pathlib import Path

import pytest
from fakes import FakeBitbucket, Origin, make_pr

from gigareview.cli import app, commands
from gigareview.config import Config
from gigareview.store import RunFilter, Store


def _write_config(tmp_path: Path, origin_url: str, run: str = "true") -> Path:
    path = tmp_path / "config.toml"
    workdir = tmp_path / "home"
    path.write_text(
        f"""
[bitbucket]
url = "https://bb"
token_env = "GR_CLI_TOKEN"

[daemon]
workdir = "{workdir}"

[scenarios.check]
run = "{run}"

[[repos]]
project = "PROJ"
slug = "repo"
clone_url = "{origin_url}"
""",
        encoding="utf-8",
    )
    return path


def test_init_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "sub" / "config.toml"
    assert app.main(["-c", str(path), "init-config"]) == 0
    assert path.read_text().startswith("# Конфигурация gigareview")
    assert app.main(["-c", str(path), "init-config"]) == 2
    assert "--force" in capsys.readouterr().err
    assert app.main(["-c", str(path), "init-config", "--force"]) == 0


def test_check_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(tmp_path, "unused")
    monkeypatch.delenv("GR_CLI_TOKEN", raising=False)
    assert app.main(["-c", str(path), "check-config"]) == 1
    assert "GR_CLI_TOKEN" in capsys.readouterr().out
    monkeypatch.setenv("GR_CLI_TOKEN", "t")
    assert app.main(["-c", str(path), "check-config"]) == 0
    assert "PROJ/repo: check" in capsys.readouterr().out


def test_missing_config_is_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert app.main(["-c", str(tmp_path / "none.toml"), "check-config"]) == 2
    assert "init-config" in capsys.readouterr().err


def test_run_unknown_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_config(tmp_path, "unused")
    assert app.main(["-c", str(path), "run", "PROJ", "other", "1"]) == 2
    assert "не описан" in capsys.readouterr().err


@pytest.mark.parametrize(("run", "code"), [("true", 0), ("exit 1", 1), ("exit 2", 2)])
def test_run_once(
    tmp_path: Path,
    origin: Origin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run: str,
    code: int,
) -> None:
    path = _write_config(tmp_path, origin.url, run)
    api = FakeBitbucket(prs=[make_pr(1, commit=origin.feature)])

    def fake_client(config: Config) -> FakeBitbucket:
        return api

    monkeypatch.setattr(commands, "_client", fake_client)
    assert app.main(["-c", str(path), "run", "PROJ", "repo", "1", "--dry-run"]) == code
    assert "Прогон #1" in capsys.readouterr().out
    assert api.comments == []
    assert api.closed
    with Store(tmp_path / "home" / "gigareview.sqlite3") as store:
        assert store.queries.runs(RunFilter())[0].dry_run


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        app.main(["--version"])
    assert "gigareview" in capsys.readouterr().out
