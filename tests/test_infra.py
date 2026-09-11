"""基础设施测试：SQLite WAL/busy_timeout、create_app 可注入临时目录。"""
from __future__ import annotations

from app.database import get_db


def test_wal_enabled(app):
    mode = get_db().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_set(app):
    timeout = get_db().execute("PRAGMA busy_timeout").fetchone()[0]
    assert int(timeout) >= 1000


def test_create_app_uses_injected_paths(tmp_path):
    from app import create_app

    db = tmp_path / "sub" / "piccrawler.db"
    root = tmp_path / "sub" / "downloads"
    application = create_app(db_path=db, download_root=root)
    assert application.config["DB_PATH"] == str(db)
    assert application.config["DOWNLOAD_ROOT"] == str(root)
    assert db.exists()
