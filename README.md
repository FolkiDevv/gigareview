# gigareview

> [!WARNING]
> **Это личный проект, а не продукт для широкой аудитории.** Если вы попали в этот репозиторий
> сами, а не по прямому приглашению автора, — этот проект точно не для вас. Он не публикуется
> на PyPI, не поддерживается для сторонних пользователей, issue и pull request'ы не принимаются.

Демон автоматического ревью pull request'ов Bitbucket Server / Data Center (API 1.0, проверено
на 8.19). Раз в N минут он забирает открытые PR, где вы стоите ревьюером, переключает локальную
копию репозитория на коммит PR и прогоняет сценарии — общие для всех репозиториев и свои для
конкретного. Замечания публикуются тасками в PR, итог — отдельным комментарием, статус ревью —
«одобрено» или «нужна доработка». Всё пишется в локальную SQLite-базу и видно в Web Dashboard на
localhost.

## Установка и запуск

Нужны Python 3.13+, [uv](https://docs.astral.sh/uv/), git и bash.

```sh
uv tool install git+https://github.com/FolkiDevv/gigareview
gigareview init-config            # пример в ~/.gigareview/config.toml — отредактируйте
export BITBUCKET_TOKEN=...        # Personal Access Token (права на чтение и запись PR)
gigareview check-config           # проверить конфигурацию и наличие токена
gigareview serve                  # демон + Web Dashboard на http://127.0.0.1:8765
```

Другие команды:

- `gigareview run PROJ repo 42 [--dry-run]` — однократно проверить PR без демона; код выхода
  0 — замечаний нет, 1 — есть замечания, 2 — ошибка.
- `gigareview serve --dry-run` — всё выполнять, но ничего не писать в Bitbucket.
- `-c путь` (или `$GIGAREVIEW_CONFIG`) — другой файл конфигурации.

## Конфигурация

Файл TOML; полный пример — `gigareview init-config`.

```toml
[daemon]
interval_minutes = 1          # период опроса Bitbucket
workdir = "~/.gigareview"     # клоны (repos/), база gigareview.sqlite3
max_parallel = 1              # прогонов одновременно (в одном репозитории — всегда по одному)
dry_run = false

[bitbucket]
url = "https://bitbucket.example.com"   # только https (http:// — лишь для localhost)
token_env = "BITBUCKET_TOKEN" # из какой переменной окружения читать токен
user = ""                     # slug пользователя; пусто — по заголовку X-AUSERNAME

[dashboard]
host = "127.0.0.1"            # только loopback: у дашборда нет входа
port = 8765
public_url = ""               # адрес для ссылок в комментариях (по умолчанию http://host:port)

[scenarios.<имя>]             # общий сценарий — для всех репозиториев
# ...

[[repos]]
project = "PROJ"
slug = "backend"
clone_url = "ssh://git@bitbucket.example.com:7999/proj/backend.git"
main_branch = "master"        # по умолчанию master
init = "uv sync"              # после каждого переключения на коммит PR
exclude_scenarios = []        # какие общие сценарии не запускать здесь

[repos.scenarios.<имя>]       # сценарий только этого репозитория (одноимённый общий — замещает)
# ...
```

## Сценарии

Сценарий — три bash-скрипта, которые выполняются в каталоге копии репозитория:

| Поле | Что делает |
|---|---|
| `run` | основные команды (например, `uv run mypy .`) |
| `validate` | оценивает результат: **0** — замечаний нет, **1** — есть замечания, **2** (и любой другой код) — сценарий отработал некорректно. Нет `validate` — оценивается код `run` по тем же правилам |
| `format` | текст таски из вывода `run`. Нет `format` — в таску идёт вывод `run` как есть. Код ≠ 0 — ошибка сценария |
| `title` | заголовок в тасках и отчёте (по умолчанию — имя) |
| `kind` | `simple` — одна таска на сценарий; `ai` — список тасок в JSON (ниже) |
| `timeout_minutes` | на весь сценарий; по умолчанию 3 для `simple`, 20 для `ai` |

Окружение всех шагов: `TARGET_BRANCH`, `SOURCE_BRANCH`, `PR_ID`, `PR_PROJECT`, `PR_REPO`,
`PR_COMMIT`, `MERGE_BASE` (общий предок с целевой веткой), `GR_REPO_DIR`, `GR_ARTIFACTS`
(временный каталог прогона). `validate` и `format` получают вывод `run` на stdin и в файле
`$GR_RUN_OUTPUT`; `validate` — ещё код завершения `run` в `$GR_RUN_EXIT`. Переменная с токеном
Bitbucket сценариям не передаётся.

Пример: новые ошибки mypy относительно целевой ветки.

```toml
[scenarios.mypy]
title = "mypy: новые ошибки"
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
```

### ИИ-сценарии

У сценария `kind = "ai"` итоговый текст (вывод `format`, иначе `run`) — JSON-список тасок:
`[{"text": "...", "path": "файл", "line": 12}]` (или `{"tasks": [...]}`); `path` и `line`
необязательны и привязывают таску к строке диффа. Пояснения вокруг JSON и ```-блоки
допускаются — берётся фрагмент от первой скобки до последней. Если строка не входит в дифф,
таска публикуется без привязки, с `путь:строка` в начале текста.

```toml
[scenarios.ai-breakage]
title = "ИИ: что сломают изменения"
kind = "ai"
run = '''
git diff "$MERGE_BASE" HEAD > "$GR_ARTIFACTS/diff.patch"
gigacrew --mode plan -p "Изучи дифф $GR_ARTIFACTS/diff.patch ($SOURCE_BRANCH → $TARGET_BRANCH). Найди, что эти изменения могут сломать в остальном коде и что автор мог упустить. Ответь только JSON-массивом [{\"text\": \"...\", \"path\": \"файл\", \"line\": 1}], без проблем — []."
'''
validate = '''python3 -c 'import json, re, sys
found = re.search(r"\[.*\]", sys.stdin.read(), re.S)
sys.exit(2 if found is None else int(bool(json.loads(found.group()))))'
'''
```

## Что происходит в Bitbucket

- Каждый сценарий с замечаниями — таска (комментарий с `severity = BLOCKER`), у ИИ-сценария —
  по таске на запись.
- Итог прогона — отдельный комментарий: таблица сценариев (пройден / замечания / ошибка) и
  ссылка на прогон в Web Dashboard.
- Все сценарии пройдены — APPROVED; есть замечания — NEEDS_WORK; замечаний нет, но есть ошибки
  сценариев — статус не меняется.
- Перед публикацией таски прошлых прогонов этого PR закрываются (RESOLVED).
- Если за время прогона PR закрыли или в него пришёл новый коммит, результат не публикуется.

Какой коммит проверять, демон решает сам: коммит, по которому уже есть прогон (в очереди, идёт
или завершён), повторно не проверяется; сбой самого прогона (git, init, Bitbucket) повторяется
при следующих опросах, но не больше трёх раз. Повторить вручную — кнопкой в Web Dashboard.

## Web Dashboard

- **Обзор** — состояние демона, последний и следующий опрос, очередь, сводка за сутки и всего,
  последние прогоны и ошибки; кнопки «Опросить сейчас», «Приостановить», «Перечитать
  конфигурацию».
- **Прогоны** — журнал с фильтрами по статусу, репозиторию, PR, сценарию и его исходу.
- **Прогон** — журнал git и init, каждый сценарий с кодами и полным выводом всех шагов, таски,
  опубликованные комментарии; «Повторить проверку».
- **Pull requests**, **Журнал** демона, **Конфигурация** (без токена).
- `GET /api/status` — состояние в JSON.

Живые обновления — без WebSocket: htmx раз в несколько секунд перезапрашивает страницу и
подменяет изменившийся блок (на странице прогона — только пока он выполняется). Все скрипты и
стили лежат в пакете (htmx вендорён в `gigareview/web/static/`), снаружи грузятся только шрифты
Google Fonts. Действия — POST-формы под CSRF-защитой; сервер слушает только localhost.

> Сценарии выполняют код из PR (`uv sync`, тесты) на вашей машине — наблюдайте только
> репозитории, авторам которых доверяете.

## Разработка

```sh
make dev     # пакет + dev-группа (pytest, линтеры, mypy, coverage)
make check   # все статические проверки и тесты — как в CI
make test    # тесты с покрытием: код демона (всё, кроме web/ и cli/) — не ниже 80 %
```

Остальные команды — `make help`. Слои и правила зависимостей — в `CLAUDE.md` и `.importlinter`.
