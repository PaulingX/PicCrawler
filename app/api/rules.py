"""规则相关接口：列表、设置本地下载目录、在线开关。"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.database import execute, query_one, setting_get, setting_set
from app.services.library_scanner import ensure_rule_shelf
from app.services.rule_registry import build_crawler, get_rule, list_rules

bp = Blueprint("rules", __name__, url_prefix="/api/rules")


@bp.get("")
def api_rules():
    rules = list_rules(include_disabled=True)
    data: list[dict] = []
    for rule in rules:
        key = f"rule_download_dir:{rule['rule_id']}"
        default_dir = str((Path(current_app.config["DOWNLOAD_ROOT"]) / rule["rule_id"]).resolve())
        download_dir = setting_get(key, default_dir) or default_dir
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        ensure_rule_shelf(rule["rule_id"], f"{rule['name']} 下载目录", [download_dir])

        data.append(
            {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "base_url": rule["base_url"],
                "enabled": int(rule.get("enabled", 1)),
                "supports_search": int(rule.get("supports_search", 0)),
                "categories": list(rule.get("categories") or []),
                "download_dir": download_dir,
            }
        )
    return jsonify({"items": data})


@bp.post("/<rule_id>/download_dir")
def api_set_rule_download_dir(rule_id: str):
    rule = get_rule(rule_id)
    if not rule:
        return jsonify({"error": "rule not found"}), 404

    payload = request.get_json(silent=True) or {}
    download_dir = str(payload.get("download_dir", "")).strip()
    if not download_dir:
        return jsonify({"error": "download_dir is required"}), 400

    download_dir = str(Path(download_dir).expanduser().resolve())
    Path(download_dir).mkdir(parents=True, exist_ok=True)

    setting_set(f"rule_download_dir:{rule_id}", download_dir)
    ensure_rule_shelf(rule_id, f"{rule['name']} 下载目录", [download_dir])
    return jsonify({"ok": True, "download_dir": download_dir})


@bp.post("/<rule_id>/enabled")
def api_set_rule_enabled(rule_id: str):
    rule = get_rule(rule_id)
    if not rule:
        return jsonify({"error": "rule not found"}), 404

    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload:
        return jsonify({"error": "enabled is required"}), 400

    enabled_raw = payload.get("enabled")
    enabled = 1 if str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"} else 0
    execute("UPDATE rules SET enabled = ? WHERE rule_id = ?", (enabled, rule_id))

    return jsonify({"ok": True, "rule_id": rule_id, "enabled": enabled})


# 保持 build_crawler 在命名空间内可被测试/其它模块引用
__all__ = ["bp", "build_crawler"]
