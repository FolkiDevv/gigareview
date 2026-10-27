# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

gigareview is a personal code-review tool built on GigaChat models, a sibling of gigacrew. It is at
an early stage: so far the repository holds only the package skeleton and the lint/test/CI setup.
Python 3.13+, managed with uv; flat layout (the `gigareview` package sits at the repository root).
Comments, docstrings, user-facing text, README and commit messages are in Russian; commit messages
follow conventional commits (`feat(review): …`, `test(cli): …`). Keep both conventions.

## Commands

```sh
make dev       # uv sync: package + dev group (pytest, linters, mypy)
make format    # ruff check --fix + ruff format
make lint      # every static check, same as the CI lint job
make test      # offline tests
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

Not designed yet. When layers appear, describe them here and enforce them with contracts in
`.importlinter` (as gigacrew does), together with the user's consent for the config change.
