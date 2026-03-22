from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import current_app, g

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    crawler TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS online_topic_cache (
    rule_id TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    topic_id TEXT NOT NULL,
    title TEXT NOT NULL,
    cover_url TEXT,
    detail_url TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, page_no, topic_id)
);

CREATE TABLE IF NOT EXISTS online_image_cache (
    rule_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    image_index INTEGER NOT NULL,
    image_url TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, topic_id, image_index)
);

CREATE TABLE IF NOT EXISTS download_jobs (
    job_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    title TEXT NOT NULL,
    detail_url TEXT NOT NULL,
    target_dir TEXT NOT NULL,
    status TEXT NOT NULL,
    total_images INTEGER NOT NULL DEFAULT 0,
    downloaded_images INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelves (
    shelf_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    roots_json TEXT NOT NULL,
    source_type TEXT NOT NULL,
    rule_id TEXT UNIQUE,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_topics (
    topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shelf_id INTEGER NOT NULL,
    topic_key TEXT NOT NULL,
    title TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    cover_path TEXT,
    total_images INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (shelf_id, topic_key)
);

CREATE TABLE IF NOT EXISTS library_images (
    image_id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    image_index INTEGER NOT NULL,
    UNIQUE (topic_id, image_index)
);

CREATE INDEX IF NOT EXISTS idx_library_topics_shelf ON library_topics(shelf_id);
CREATE INDEX IF NOT EXISTS idx_library_images_topic ON library_images(topic_id);
CREATE INDEX IF NOT EXISTS idx_download_jobs_updated ON download_jobs(updated_at DESC);
"""

DEFAULT_RULES = [
    {
        "rule_id": "4khd",
        "name": "4KHD",
        "base_url": "https://www.4khd.com/",
        "crawler": "crawler_4khd",
        "enabled": 1,
    },
    {
        "rule_id": "asmhentai-zh",
        "name": "ASMHentai 中文",
        "base_url": "https://asmhentai.com/language/chinese/",
        "crawler": "crawler_asmhentai",
        "enabled": 1,
    },
    {
        "rule_id": "wnacg",
        "name": "WNACG",
        "base_url": "https://www.wnacg.com/",
        "crawler": "crawler_wnacg",
        "enabled": 1,
    },
    {
        "rule_id": "hitomi-chinese",
        "name": "Hitomi 中文",
        "base_url": "https://hitomi.la/index-chinese.html",
        "crawler": "crawler_hitomi",
        "enabled": 1,
    },
]

DEPRECATED_RULE_IDS = ["manxiangge"]


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = _connect(Path(current_app.config["DB_PATH"]))
    return g.db


def close_db(_: object | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def execute(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    return get_db().execute(sql, params).fetchone()


def query_all(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    return list(get_db().execute(sql, params).fetchall())


def setting_get(key: str, default: str | None = None) -> str | None:
    row = query_one(
        "SELECT setting_value FROM settings WHERE setting_key = ?",
        (key,),
    )
    if row is None:
        return default
    return str(row["setting_value"])


def setting_set(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings(setting_key, setting_value)
        VALUES(?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
        """,
        (key, value),
    )


def init_db(db_path: Path, download_root: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    download_root.mkdir(parents=True, exist_ok=True)

    with closing(_connect(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)

        for deprecated_rule_id in DEPRECATED_RULE_IDS:
            conn.execute("DELETE FROM online_topic_cache WHERE rule_id = ?", (deprecated_rule_id,))
            conn.execute("DELETE FROM online_image_cache WHERE rule_id = ?", (deprecated_rule_id,))
            conn.execute("DELETE FROM download_jobs WHERE rule_id = ?", (deprecated_rule_id,))
            conn.execute("DELETE FROM shelves WHERE rule_id = ?", (deprecated_rule_id,))
            conn.execute("DELETE FROM rules WHERE rule_id = ?", (deprecated_rule_id,))
            conn.execute(
                "DELETE FROM settings WHERE setting_key = ?",
                (f"rule_download_dir:{deprecated_rule_id}",),
            )

        for rule in DEFAULT_RULES:
            conn.execute(
                """
                INSERT INTO rules(rule_id, name, base_url, crawler, enabled)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    crawler = excluded.crawler
                """,
                (
                    rule["rule_id"],
                    rule["name"],
                    rule["base_url"],
                    rule["crawler"],
                    int(rule["enabled"]),
                ),
            )

            key = f"rule_download_dir:{rule['rule_id']}"
            default_dir = str((download_root / rule["rule_id"]).resolve())
            conn.execute(
                """
                INSERT OR IGNORE INTO settings(setting_key, setting_value)
                VALUES(?, ?)
                """,
                (key, default_dir),
            )

            row = conn.execute(
                "SELECT setting_value FROM settings WHERE setting_key = ?",
                (key,),
            ).fetchone()
            roots_json = json.dumps([row[0]])
            conn.execute(
                """
                INSERT INTO shelves(name, roots_json, source_type, rule_id, updated_at)
                VALUES(?, ?, 'rule', ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    name = excluded.name,
                    roots_json = excluded.roots_json,
                    source_type = 'rule',
                    updated_at = excluded.updated_at
                """,
                (f"{rule['name']} 下载目录", roots_json, rule["rule_id"], _utcnow()),
            )

        conn.commit()





