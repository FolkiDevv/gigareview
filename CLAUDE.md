# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

gigareview is a personal code-review daemon for Bitbucket Server, a sibling of gigacrew. It polls
the PRs where the user is a reviewer, checks out each new PR commit locally, runs configured
scenarios (shell scripts; AI scenarios call gigacrew), posts tasks and a summary comment, sets the
review status, and records everything in SQLite shown by a local Web Dashboard.
Python 3.13+, managed with uv; flat layout (the `gigareview` package sits at the repository root).
Comments, docstrings, user-facing text, README and commit messages are in Russian; commit messages
follow conventional commits (`feat(review): …`, `test(cli): …`). Keep both conventions.

## Commands

```sh
make dev       # uv sync: package + dev group (pytest, linters, mypy)
make format    # ruff check --fix + ruff format
make lint      # every static check, same as the CI lint job
make test      # offline tests under coverage; fails below 80 % of daemon code
make check     # lint + offline tests — run before pushing
make lock      # after editing dependencies in pyproject.toml
make audit     # pip-audit (not blocking in CI)
```

- One test: `uv run pytest tests/test_package.py::test_name` (or `-k pattern`).
- Test order is random (pytest-randomly). Add `-p no:randomly` for a fixed order; reproduce a
  failing order with the `--randomly-seed=N` printed in the pytest header.

## Verification is mandatory

A change is done only when `make lint` and `make test` both pass. `make lint` runs ruff (check and
format), flake8 with wemake-python-styleguide, import-linter (`lint-imports`), mypy `--strict` over
both `gigareview/` and `tests/`, codespell, typos, yamllint, deptry, `uv lock --check` and
`uv pip check`. CI runs the same checks plus the offline tests on Python 3.13 and 3.14.
flake8, codespell and yamllint do not honour `.gitignore`: stray worktrees or venvs inside the
repository (for example `.claude/worktrees/`) make `make lint` slow and noisy — remove them first.

## Linter configuration is frozen

The configuration is taken from gigacrew as is, minus its project-specific exceptions. Do not
change linter or type-checker configuration without the user's explicit consent. This covers
`[tool.ruff*]`, `[tool.mypy]`, `[tool.pytest.ini_options]`, `[tool.deptry]`, `[tool.codespell]` and
`[tool.typos]` in `pyproject.toml`; `setup.cfg` (wemake thresholds, allowed names,
`per-file-ignores`); `.importlinter`; `.yamllint.yml`; and the lint steps in the `Makefile` and
`.github/workflows/ci.yml`. Adding a per-file ignore, raising a threshold or disabling a rule
counts as a change: fix the code instead, or ask.

An inline `# noqa: CODE` / `# type: ignore[code]` is acceptable only for a genuine false positive,
always with a specific code and a one-line reason comment on the line above (never inside the
noqa comment). wemake caps a file at 10 noqa comments (WPS402).

Conventions the configuration enforces that are easy to trip over:
- Package `__init__.py` files contain only imports (and the module docstring); re-export as
  `from x import Name as Name` (WPS412 plus mypy's no-implicit-reexport). Other modules list their
  exports in `__all__`.
- Overriding methods carry `@override` (mypy `explicit-override`).
- No single-letter names and no wemake-blacklisted names (`data`, `result`, `value`, `item`,
  `info`, `params`, `content`, `handler`, …); unlike gigacrew, no domain names are allowed yet.
- Line length 100, double quotes, ruff preview formatting.

## Architecture

### Layers

`.importlinter` enforces the layering; a layer may import only the layers below it:

```
__main__ → cli → web → daemon → pipeline → scenarios | bitbucket → store → gitops → process
→ config → statuses → errors
```

Litestar, uvicorn and Jinja are imported only by `web` and `cli` (a second contract).

### Flow

- `cli/app.py:main` parses arguments and loads `Config` (`config/`, pydantic over TOML; the
  Bitbucket token comes only from the environment variable named in `bitbucket.token_env`).
  `serve` runs the `Daemon` and the Litestar app under one uvicorn event loop.
- `daemon/service.py` — the poll loop (`poll` every `interval_minutes`, or at once on
  `poll_now`) enqueues PR commits that `store.runs.should_skip` has not seen; workers run
  `pipeline.Reviewer.review`, one run per repository at a time.
- `pipeline/review.py` — workspace (`pipeline/workspace.py`: `gitops.GitRepo` clone/fetch/switch,
  then `init`), scenarios one by one (`scenarios/runner.py`: run → validate → format, exit codes
  0/1/other), then `pipeline/publish.py` resolves the previous tasks, posts tasks and the summary
  and sets the review status. Every failure ends as a run with status `error`; the reviewer
  never raises except on cancellation.
- `bitbucket/client.py` is the only HTTP code; the pipeline and daemon depend on the
  `BitbucketApi` protocol, which tests replace with `tests/fakes.py:FakeBitbucket`.
- `store/` — one SQLite connection (stdlib `sqlite3`, migrations by `PRAGMA user_version` in
  `store/schema.py`); repositories per table, `store/queries.py` for the dashboard.
- `web/` — Litestar handlers (sync handlers run on the event loop), Jinja templates and vendored
  static files. No CDN: htmx is vendored unminified (`htmx.js`, the minified build trips
  codespell/typos); live updates are htmx polling with `hx-select="#live"`, not WebSocket.
  POST actions use Litestar CSRF and redirect back.

Child processes go through `process.run_process` (timeout kills the whole process group).
Tests use real git repositories in `tmp_path` (`tests/fakes.py:make_origin`) and call async code
through `asyncio.run`.
