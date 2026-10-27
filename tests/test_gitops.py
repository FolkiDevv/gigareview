import asyncio
from pathlib import Path

import pytest
from fakes import Origin, git

from gigareview.errors import GitError
from gigareview.gitops import GitRepo, redact_credentials


def _checkout(repo: GitRepo, commit: str) -> str:
    async def scenario() -> str:
        await repo.ensure_clone()
        return await repo.checkout("feature", "master", commit)

    return asyncio.run(scenario())


def test_clone_and_checkout_pr(tmp_path: Path, origin: Origin) -> None:
    repo = GitRepo(tmp_path / "clones" / "repo", origin.url)
    merge_base = _checkout(repo, origin.feature)
    assert merge_base == origin.master
    assert (repo.path / "app.py").read_text() == "print('feature')\n"
    assert git("rev-parse", "HEAD", cwd=repo.path) == origin.feature
    assert git("branch", "--show-current", cwd=repo.path) == "feature"
    assert "git clone" in repo.log
    assert "git switch" in repo.log


def test_checkout_resets_local_state_but_keeps_ignored(tmp_path: Path, origin: Origin) -> None:
    repo = GitRepo(tmp_path / "repo", origin.url)
    _checkout(repo, origin.feature)
    (repo.path / "app.py").write_text("dirty\n")
    (repo.path / "untracked.txt").write_text("x")
    (repo.path / ".venv").mkdir()
    (repo.path / ".venv" / "keep").write_text("x")
    _checkout(GitRepo(repo.path, origin.url), origin.feature)
    assert (repo.path / "app.py").read_text() == "print('feature')\n"
    assert not (repo.path / "untracked.txt").exists()
    assert (repo.path / ".venv" / "keep").exists()


def test_checkout_picks_up_new_commits(tmp_path: Path, origin: Origin) -> None:
    repo = GitRepo(tmp_path / "repo", origin.url)
    _checkout(repo, origin.feature)
    (origin.work / "app.py").write_text("print('v2')\n")
    git("commit", "-q", "-am", "v2", cwd=origin.work)
    git("push", "-q", origin.url, "feature", cwd=origin.work)
    new_commit = git("rev-parse", "HEAD", cwd=origin.work)
    _checkout(GitRepo(repo.path, origin.url), new_commit)
    assert (repo.path / "app.py").read_text() == "print('v2')\n"


def test_fork_pr_is_fetched_from_pull_request_ref(tmp_path: Path, origin: Origin) -> None:
    git("push", "-q", origin.url, f"{origin.feature}:refs/pull-requests/7/from", cwd=origin.work)
    bare = Path(origin.url)
    git("symbolic-ref", "HEAD", "refs/heads/master", cwd=bare)
    git("update-ref", "-d", "refs/heads/feature", cwd=bare)
    repo = GitRepo(tmp_path / "repo", origin.url)

    async def scenario() -> str:
        await repo.ensure_clone()
        return await repo.checkout("fork-branch", "master", origin.feature, fork_pr=7)

    assert asyncio.run(scenario()) == origin.master
    assert git("rev-parse", "HEAD", cwd=repo.path) == origin.feature
    assert "refs/pull-requests/7/from" in repo.log


def test_unknown_commit_is_git_error(tmp_path: Path, origin: Origin) -> None:
    repo = GitRepo(tmp_path / "repo", origin.url)
    with pytest.raises(GitError, match="git switch"):
        _checkout(repo, "0" * 40)


def test_bad_clone_url_is_git_error(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path / "repo", str(tmp_path / "missing.git"))
    with pytest.raises(GitError, match="код"):
        _checkout(repo, "abc")


def test_credentials_are_redacted(tmp_path: Path) -> None:
    url = f"file://user:s3cret@localhost{tmp_path}/missing.git"
    repo = GitRepo(tmp_path / "repo", url)
    with pytest.raises(GitError) as caught:
        _checkout(repo, "abc")
    assert "s3cret" not in str(caught.value)
    assert "s3cret" not in repo.log
    assert "***@localhost" in repo.log


def test_token_as_username_is_redacted() -> None:
    url = "https://TOKEN123@bb.example.com/scm/p/r.git"
    assert redact_credentials(f"clone {url}", url) == "clone https://***@bb.example.com/scm/p/r.git"
    ssh = "ssh://git@bb.example.com:7999/p/r.git"
    assert redact_credentials(ssh, ssh) == ssh


def test_timeout_is_git_error(tmp_path: Path, origin: Origin) -> None:
    repo = GitRepo(tmp_path / "repo", origin.url, timeout=0.0001)
    with pytest.raises(GitError, match="не уложилась"):
        _checkout(repo, origin.feature)
