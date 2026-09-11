from __future__ import annotations

from pathlib import Path

from flask import Flask

from app import config as app_config
from app.database import close_db, init_db
from app.routes import register_routes
from app.services.download_worker import DownloadWorker


def create_app(db_path=None, download_root=None) -> Flask:
    """创建 Flask 应用。

    db_path / download_root 可选：传入时优先使用，便于测试隔离与自定义部署。
    未传则回退到 app.config 中的实时配置（DB_PATH / DOWNLOAD_ROOT）。
    """
    resolved_db = Path(db_path) if db_path is not None else Path(app_config.DB_PATH)
    resolved_root = Path(download_root) if download_root is not None else Path(app_config.DOWNLOAD_ROOT)

    app = Flask(__name__)
    app.config["DB_PATH"] = str(resolved_db)
    app.config["DOWNLOAD_ROOT"] = str(resolved_root)

    resolved_root.mkdir(parents=True, exist_ok=True)
    init_db(resolved_db, resolved_root)

    app.teardown_appcontext(close_db)
    register_routes(app)

    with app.app_context():
        app.extensions["download_worker"] = DownloadWorker(app)

    return app
