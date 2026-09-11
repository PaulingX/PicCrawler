"""下载接口：提交下载任务、查询进行中的任务列表。"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.api.helpers import _cleanup_stale_download_jobs
from app.database import query_all, query_one, setting_get
from app.services.image_proxy import (
    _get_or_fetch_images,
    _is_displayable_image_url,
    _normalize_remote_image_url,
)
from app.services.utils import is_image_file, sanitize_name

bp = Blueprint("download", __name__, url_prefix="/api")


@bp.post("/download")
def api_download_topic():
    payload = request.get_json(silent=True) or {}
    rule_id = str(payload.get("rule", "4khd")).strip()
    topic_id = str(payload.get("topic_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    detail_url = str(payload.get("detail_url", "")).strip()

    if not all([rule_id, topic_id, title, detail_url]):
        return jsonify({"error": "rule, topic_id, title, detail_url are required"}), 400

    download_dir = setting_get(
        f"rule_download_dir:{rule_id}",
        str((Path(current_app.config["DOWNLOAD_ROOT"]) / rule_id).resolve()),
    )
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    target_dir = (Path(download_dir) / sanitize_name(title)).resolve()
    if target_dir.exists():
        has_existing_images = False
        try:
            has_existing_images = any(is_image_file(p) for p in target_dir.iterdir())
        except OSError:
            has_existing_images = False
        if has_existing_images:
            return jsonify(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "folder_exists",
                    "target_dir": str(target_dir),
                    "message": "同名目录已存在且包含图片，已跳过下载",
                }
            )

    provided_urls = payload.get("image_urls")
    image_urls: list[str] = []

    if isinstance(provided_urls, list):
        seen: set[str] = set()
        for raw in provided_urls[:5000]:
            url = str(raw or "").strip()
            if not _is_displayable_image_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            image_urls.append(url)

    if not image_urls:
        try:
            image_urls = _get_or_fetch_images(rule_id, topic_id, detail_url)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"下载任务创建失败: {exc}"}), 502

    # Normalize unstable upstream URLs (e.g. 4khd/wp wrappers) for downloader.
    normalized_urls: list[str] = []
    seen_download_urls: set[str] = set()
    for raw_url in image_urls:
        normalized = _normalize_remote_image_url(str(raw_url or "").strip())
        resolved = normalized if _is_displayable_image_url(normalized) else str(raw_url or "").strip()
        if not _is_displayable_image_url(resolved):
            continue
        if resolved in seen_download_urls:
            continue
        seen_download_urls.add(resolved)
        normalized_urls.append(resolved)
    image_urls = normalized_urls

    if not image_urls:
        return jsonify({"error": "No image found in topic"}), 400

    worker = current_app.extensions["download_worker"]
    job_id = worker.submit(rule_id, topic_id, title, detail_url, download_dir, image_urls)
    return jsonify({"ok": True, "job_id": job_id, "total_images": len(image_urls)})


@bp.get("/download/jobs")
def api_download_jobs():
    _cleanup_stale_download_jobs()
    limit = max(1, min(200, int(request.args.get("limit", "30"))))
    rows = query_all(
        """
        SELECT job_id, rule_id, topic_id, title, status, total_images,
               downloaded_images, error_message, target_dir, created_at, updated_at
        FROM download_jobs
        WHERE status IN ('queued', 'running')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return jsonify({"items": [dict(r) for r in rows]})


@bp.post("/download/jobs/<job_id>/cancel")
def api_download_job_cancel(job_id: str):
    row = query_one("SELECT status FROM download_jobs WHERE job_id = ?", (job_id,))
    if row is None:
        return jsonify({"error": "job not found"}), 404

    status = str(row["status"] or "")
    if status not in ("queued", "running"):
        return jsonify({"ok": True, "canceled": False, "reason": f"already_{status}"})

    worker = current_app.extensions["download_worker"]
    canceled = bool(worker.cancel(job_id))
    return jsonify({"ok": True, "canceled": canceled})
