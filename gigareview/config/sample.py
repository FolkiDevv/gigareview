"""Пример конфигурации для ``gigareview init-config``."""

__all__ = ["SAMPLE_CONFIG"]

SAMPLE_CONFIG = r'''# Конфигурация gigareview. Документация — README репозитория.

[daemon]
interval_minutes = 1          # как часто опрашивать Bitbucket
workdir = "~/.gigareview"     # клоны репозиториев, база SQLite
max_parallel = 1              # сколько прогонов одновременно (в разных репозиториях)
dry_run = false               # true — ничего не писать в Bitbucket
approve = true                # false — не ставить APPROVED (NEEDS_WORK — как обычно)

[bitbucket]
url = "https://bitbucket.example.com"
token_env = "BITBUCKET_TOKEN" # токен читается из этой переменной окружения
user = ""                     # slug пользователя; пусто — определить по токену

[dashboard]
host = "127.0.0.1"
port = 8765

# Общие сценарии — для всех репозиториев.
# Окружение команд: TARGET_BRANCH, SOURCE_BRANCH, PR_ID, PR_PROJECT, PR_REPO, PR_COMMIT,
# PR_TITLE, PR_DESCRIPTION, PR_AUTHOR, MERGE_BASE, GR_PREV_COMMIT, GR_OPEN_TASKS, GR_REPO_DIR,
# GR_ARTIFACTS, GIGAREVIEW; валидации и форматированию — ещё GR_RUN_EXIT и GR_RUN_OUTPUT,
# а вывод основных команд приходит им на stdin.
# Код валидации: 0 — замечаний нет, 1 — есть замечания, 2 — сценарий отработал некорректно.

[scenarios.mypy]
title = "mypy: новые ошибки относительно целевой ветки"
run = """
uv run mypy . --no-error-summary > "$GR_ARTIFACTS/pr.txt" || true
git worktree add --force --detach "$GR_ARTIFACTS/base" "$MERGE_BASE" > /dev/null
(cd "$GR_ARTIFACTS/base" && uv run mypy . --no-error-summary > "$GR_ARTIFACTS/base.txt" || true)
git worktree remove --force "$GR_ARTIFACTS/base"
strip() { sed -E 's/:[0-9]+(:[0-9]+)?:/:/' "$1" | sort -u; }
comm -13 <(strip "$GR_ARTIFACTS/base.txt") <(strip "$GR_ARTIFACTS/pr.txt")
"""
validate = '[ "$GR_RUN_EXIT" -eq 0 ] || exit 2; test -z "$(cat)"'
format = 'printf "Новые ошибки mypy:\n\n"; cat'
timeout_minutes = 5

# То же на mypy-diff (https://github.com/FolkiDevv/mypy-diff, `uv tool install mypy-diff`):
# ошибки сопоставляются с учётом сдвига строк, коды выхода (0/1/2) — те же, что у сценария,
# поэтому validate не нужен. --mypy-cmd задан явно: иначе mypy-diff может взять mypy из
# активного окружения демона, а не из .venv проекта. Цели — из настроек mypy или после `--`.
# Чтобы включить, раскомментируйте и уберите [scenarios.mypy].
# [scenarios.mypy-diff]
# title = "mypy: новые ошибки относительно целевой ветки"
# run = """
# mypy-diff-git -b "$MERGE_BASE" --mypy-cmd "$GR_REPO_DIR/.venv/bin/mypy" \
#   --format plain --only new --no-progress
# """
# timeout_minutes = 5

# ИИ-ревью на gigacrew: gigacrew — в PATH, его настройки (GIGACREW_*) — из окружения демона
# или ~/.gigacode/.env. После первого ревью смотрит только новые коммиты PR; свои прошлые
# таски закрывает сам, когда они исправлены.
[scenarios.ai-review]
title = "ИИ-ревью: ошибки и регрессии"
kind = "ai"
incremental = true
run = """
prompt=$("$GIGAREVIEW" ai-review prompt) || exit 2
gigacrew --mode plan --no-save --no-color -p "$prompt"
"""
validate = '"$GIGAREVIEW" ai-review tasks --check'
format = '"$GIGAREVIEW" ai-review tasks'
timeout_minutes = 20

[[repos]]
project = "PROJ"
slug = "backend"
clone_url = "ssh://git@bitbucket.example.com:7999/proj/backend.git"
main_branch = "master"
init = "uv sync"

[repos.scenarios.pytest]
title = "Тесты"
run = "uv run pytest -q -x"
'''
