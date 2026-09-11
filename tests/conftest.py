"""pytest 共享夹具：把数据目录重定向到临时目录，避免触碰真实 data/。

create_app() 在导入时用 `from app.config import DB_PATH` 把路径快照为模块常量，
因此必须在调用 create_app 之前覆盖 app 包命名空间里的 DB_PATH / DOWNLOAD_ROOT，
否则会落到真实的 data/piccrawler.db。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def app(tmp_path):
    db_path = tmp_path / "piccrawler.db"
    dl_root = tmp_path / "downloads"

    from app import create_app

    application = create_app(db_path=db_path, download_root=dl_root)

    ctx = application.app_context()
    ctx.push()
    yield application
    ctx.pop()


@pytest.fixture
def client(app):
    return app.test_client()
