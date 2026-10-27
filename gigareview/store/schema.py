"""Схема базы и её миграции (версия — ``PRAGMA user_version``)."""

__all__ = ["MIGRATIONS"]

_INITIAL = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_id INTEGER NOT NULL,
    pr_title TEXT NOT NULL DEFAULT '',
    pr_author TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '',
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    setup_log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX runs_by_pr ON runs (project, repo, pr_id, commit_id);
CREATE INDEX runs_by_status ON runs (status);

CREATE TABLE scenario_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    run_exit INTEGER,
    run_output TEXT NOT NULL DEFAULT '',
    validate_exit INTEGER,
    validate_output TEXT NOT NULL DEFAULT '',
    format_exit INTEGER,
    format_output TEXT NOT NULL DEFAULT '',
    tasks TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL
);
CREATE INDEX scenario_results_by_run ON scenario_results (run_id);
CREATE INDEX scenario_results_by_name ON scenario_results (name, outcome);

CREATE TABLE bb_comments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_id INTEGER NOT NULL,
    comment_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX bb_comments_by_pr ON bb_comments (project, repo, pr_id);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    run_id INTEGER REFERENCES runs (id) ON DELETE SET NULL
);
CREATE INDEX events_by_level ON events (level);

CREATE TABLE daemon_state (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL
);
"""

# Текст и привязка таски: инкрементальный сценарий получает открытые таски прошлых прогонов.
_TASK_DETAILS = """
ALTER TABLE bb_comments ADD COLUMN text TEXT NOT NULL DEFAULT '';
ALTER TABLE bb_comments ADD COLUMN path TEXT;
ALTER TABLE bb_comments ADD COLUMN line INTEGER;
"""

#: Миграции по порядку: i-я переводит базу с версии i на i + 1.
MIGRATIONS: tuple[str, ...] = (_INITIAL, _TASK_DETAILS)
