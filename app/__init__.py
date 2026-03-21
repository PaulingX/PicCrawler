from __future__ import annotations

from pathlib import Path

from flask import Flask

from app.config import DB_PATH, DOWNLOAD_ROOT
from app.database import close_db, init_db
from app.routes import bp
from app.services.download_worker import DownloadWorker


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(DB_PATH)
    app.config["DOWNLOAD_ROOT"] = str(DOWNLOAD_ROOT)

    Path(app.config["DOWNLOAD_ROOT"]).mkdir(parents=True, exist_ok=True)
    init_db(Path(app.config["DB_PATH"]), Path(app.config["DOWNLOAD_ROOT"]))

    app.teardown_appcontext(close_db)
    app.register_blueprint(bp)

    with app.app_context():
        app.extensions["download_worker"] = DownloadWorker(app)

    return app
