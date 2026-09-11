"""蓝图注册聚合 + 向后兼容再导出。

所有路由已按域拆分到：
  - app/api/system.py   (/api/system/*)
  - app/api/rules.py    (/api/rules/*)
  - app/api/online.py   (/api/online/*)
  - app/api/download.py  (/api/download, /api/download/jobs)
  - app/api/library.py  (/api/shelves*, /api/library/*)
  - app/pages.py        (/)

本文件仅负责统一注册蓝图，并 re-export 原 routes 顶层函数，
供 tests/baseline_snapshot.py 等旧脚本兼容使用（行为不变）。
"""
from __future__ import annotations

# ---- 向后兼容：保留原 routes 顶层的纯函数（实现已迁移到各自模块） ----
from app.api.helpers import (  # noqa: F401
    _cleanup_stale_download_jobs,
    _get_cached_topics,
    _is_rule_online_enabled,
    _save_topics,
)
from app.services.download_worker import (  # noqa: F401
    _candidate_download_urls,
    _normalize_download_image_url,
)
from app.services.image_proxy import (  # noqa: F401
    _candidate_fetch_urls,
    _cached_images_need_refresh,
    _fetch_image_with_fallbacks,
    _is_displayable_image_url,
    _is_low_quality_gallery_url,
    _is_safe_remote_url,
    _normalize_remote_image_url,
    _pick_direct_redirect_url,
    _prefer_display_image_url,
    _proxy_remote_image_url,
    _wrap_wp_proxy_url,
)

# ---- 蓝图 ----
from app.api.download import bp as download_bp  # noqa: E402
from app.api.library import bp as library_bp  # noqa: E402
from app.api.online import bp as online_bp  # noqa: E402
from app.api.rules import bp as rules_bp  # noqa: E402
from app.api.system import bp as system_bp  # noqa: E402
from app.pages import bp as pages_bp  # noqa: E402


def register_routes(app) -> None:
    """将全部按域拆分的蓝图注册到 Flask app。"""
    app.register_blueprint(system_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(online_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(pages_bp)
