from __future__ import annotations

import ipaddress
import re
import sqlite3
import subprocess
import sys
import shutil
import ctypes
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests import Response as RequestsResponse
from requests.exceptions import RequestException
from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, send_file, Response

from app.config import USER_AGENT
from app.database import execute, query_all, query_one, setting_get, setting_set
from app.services.library_scanner import (
    create_custom_shelf,
    delete_custom_shelf,
    ensure_rule_shelf,
    list_shelves,
    refresh_shelf,
)
from app.services.rule_registry import build_crawler, get_rule, list_rules
from app.services.utils import is_image_file, sanitize_name

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.get("/api/rules")
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


@bp.post("/api/rules/<rule_id>/download_dir")
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


@bp.post("/api/system/select-folder")
def api_system_select_folder():
    payload = request.get_json(silent=True) or {}
    initial_dir = str(payload.get("initial_dir", "")).strip()

    if initial_dir:
        initial = Path(initial_dir).expanduser()
        initial_dir = str(initial.resolve()) if initial.exists() else ""

    try:
        if sys.platform.startswith("win"):
            selected = _select_folder_windows(initial_dir)
        else:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                initialdir=initial_dir or None,
                title="选择目录",
                mustexist=False,
            )
            root.destroy()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"系统目录选择失败: {exc}"}), 500

    chosen = str(Path(selected).resolve()) if selected else ""
    return jsonify({"ok": True, "path": chosen})


@bp.get("/api/system/directories")
def api_system_directories():
    raw_path = str(request.args.get("path", "")).strip()
    current = _normalize_existing_dir(raw_path)

    if current is None:
        roots = _list_directory_roots()
        return jsonify(
            {
                "ok": True,
                "is_root": True,
                "current": "",
                "parent": "",
                "items": [{"name": str(root), "path": str(root)} for root in roots],
            }
        )

    parent = current.parent
    parent_path = "" if parent == current else str(parent)

    children: list[dict[str, str]] = []
    try:
        dirs = [p for p in current.iterdir() if p.is_dir()]
    except (PermissionError, OSError):
        dirs = []

    dirs.sort(key=lambda p: p.name.lower())
    for child in dirs:
        children.append({"name": child.name, "path": str(child)})

    return jsonify(
        {
            "ok": True,
            "is_root": False,
            "current": str(current),
            "parent": parent_path,
            "items": children,
        }
    )


def _select_folder_windows(initial_dir: str) -> str:
    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_exe:
        raise RuntimeError("未找到 PowerShell，无法打开系统目录选择器")

    escaped = initial_dir.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dlg.Description = '选择目录'; "
        "$dlg.ShowNewFolderButton = $true; "
        f"if ('{escaped}' -and (Test-Path -LiteralPath '{escaped}')) {{ $dlg.SelectedPath = '{escaped}' }}; "
        "$result = $dlg.ShowDialog(); "
        "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Write-Output $dlg.SelectedPath }"
    )

    proc = subprocess.run(  # noqa: S603
        [ps_exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"PowerShell 退出码 {proc.returncode}")

    return (proc.stdout or "").strip()


def _normalize_existing_dir(raw_path: str) -> Path | None:
    if not raw_path:
        return None

    raw = raw_path.strip()
    if not raw:
        return None

    if sys.platform.startswith("win") and re.fullmatch(r"[a-zA-Z]:", raw):
        raw = raw + "\\"

    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None

    if not resolved.exists() or not resolved.is_dir():
        return None
    return resolved


def _list_directory_roots() -> list[Path]:
    if sys.platform.startswith("win"):
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        roots: list[Path] = []
        for idx in range(26):
            if not (bitmask & (1 << idx)):
                continue
            letter = chr(ord("A") + idx)
            root = Path(f"{letter}:\\")
            if root.exists():
                roots.append(root)
        roots.sort(key=lambda p: str(p).lower())
        return roots
    return [Path("/")]

@bp.post("/api/rules/<rule_id>/enabled")
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

@bp.get("/api/online/topics")
def api_online_topics():
    rule_id = request.args.get("rule", "4khd").strip()
    page_no = max(1, int(request.args.get("page", "1")))
    query = request.args.get("q", "").strip()
    category_raw = request.args.get("category", "").strip()
    category_id = int(category_raw) if category_raw.isdigit() else None

    if not _is_rule_online_enabled(rule_id):
        return jsonify({"error": "该规则在线浏览已关闭"}), 403

    topics: list[dict] = []
    try:
        crawler = build_crawler(rule_id)
        # Rules with category browsing should fetch per category directly.
        use_cache = (not query) and category_id is None and (rule_id not in {"asmhentai-zh", "wnacg", "manxiangge"})
        if query:
            if category_id is None:
                topics = crawler.list_topics(page_no, query=query)
            else:
                try:
                    topics = crawler.list_topics(page_no, query=query, category_id=category_id)
                except TypeError:
                    topics = crawler.list_topics(page_no, query=query)
        elif use_cache:
            topics = _get_cached_topics(rule_id, page_no)
            if not topics:
                topics = crawler.list_topics(page_no, query="")
                _save_topics(rule_id, page_no, topics)
        else:
            if category_id is None:
                topics = crawler.list_topics(page_no, query="")
            else:
                try:
                    topics = crawler.list_topics(page_no, query="", category_id=category_id)
                except TypeError:
                    topics = crawler.list_topics(page_no, query="")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"在线抓取失败: {exc}"}), 502

    output = []
    for topic in topics:
        item = dict(topic)
        item["cover_url"] = _proxy_remote_image_url(item.get("cover_url", ""), item.get("detail_url", ""))
        output.append(item)

    return jsonify({"items": output, "page": page_no, "q": query, "category": category_id})


@bp.get("/api/online/topic-images")
def api_online_topic_images():
    rule_id = request.args.get("rule", "4khd").strip()
    if not _is_rule_online_enabled(rule_id):
        return jsonify({"error": "该规则在线浏览已关闭"}), 403

    topic_id = request.args.get("topic_id", "").strip()
    detail_url = request.args.get("detail_url", "").strip()
    offset = max(0, int(request.args.get("offset", "0")))
    limit = max(1, min(100, int(request.args.get("limit", "20"))))

    if not topic_id or not detail_url:
        return jsonify({"error": "topic_id and detail_url are required"}), 400

    try:
        crawler = build_crawler(rule_id)
        paged_fetcher = getattr(crawler, "topic_images_page", None)

        if callable(paged_fetcher):
            page_data = paged_fetcher(detail_url=detail_url, offset=offset, limit=limit)
            raw_items = list(page_data.get("items") or []) if isinstance(page_data, dict) else []

            images: list[str] = []
            seen: set[str] = set()
            for raw in raw_items:
                url = str(raw or "").strip()
                if not _is_displayable_image_url(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                images.append(url)

            has_more = bool(page_data.get("has_more")) if isinstance(page_data, dict) else False
            total_raw = page_data.get("total") if isinstance(page_data, dict) else None
            try:
                total = int(total_raw)
            except (TypeError, ValueError):
                total = offset + len(images) + (1 if has_more else 0)
            total = max(0, total)

            next_offset_raw = page_data.get("next_offset") if isinstance(page_data, dict) else None
            try:
                next_offset = int(next_offset_raw)
            except (TypeError, ValueError):
                next_offset = offset + len(images)

            if has_more and next_offset <= offset:
                next_offset = offset + max(1, len(images))
            if total >= 0 and next_offset >= total:
                has_more = False

            proxied = [_proxy_remote_image_url(url, detail_url) for url in images]
            return jsonify(
                {
                    "items": proxied,
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": has_more,
                }
            )

        all_images = _get_or_fetch_images(rule_id, topic_id, detail_url)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"主题图片抓取失败: {exc}"}), 502

    chunk = all_images[offset : offset + limit]
    proxied = [_proxy_remote_image_url(url, detail_url) for url in chunk]
    return jsonify(
        {
            "items": proxied,
            "offset": offset,
            "limit": limit,
            "total": len(all_images),
            "next_offset": offset + len(chunk),
            "has_more": offset + len(chunk) < len(all_images),
        }
    )


@bp.get("/api/online/topic-count")
def api_online_topic_count():
    rule_id = request.args.get("rule", "4khd").strip()
    if not _is_rule_online_enabled(rule_id):
        return jsonify({"error": "该规则在线浏览已关闭"}), 403

    topic_id = request.args.get("topic_id", "").strip()
    detail_url = request.args.get("detail_url", "").strip()
    if not topic_id or not detail_url:
        return jsonify({"error": "topic_id and detail_url are required"}), 400

    row = query_one(
        """
        SELECT MAX(image_index) AS cnt
        FROM online_image_cache
        WHERE rule_id = ? AND topic_id = ?
        """,
        (rule_id, topic_id),
    )
    cached_count = int(row["cnt"]) if row and row["cnt"] is not None else 0

    try:
        crawler = build_crawler(rule_id)

        count_fetcher = getattr(crawler, "topic_image_count", None)
        if callable(count_fetcher):
            count = max(0, int(count_fetcher(detail_url)))
            return jsonify({"count": count, "cached": False})

        paged_fetcher = getattr(crawler, "topic_images_page", None)
        if callable(paged_fetcher):
            page_data = paged_fetcher(detail_url=detail_url, offset=0, limit=1)
            if isinstance(page_data, dict):
                total_raw = page_data.get("total")
                try:
                    total = int(total_raw)
                except (TypeError, ValueError):
                    total = -1
                if total >= 0:
                    return jsonify({"count": total, "cached": False})

        if cached_count > 0:
            return jsonify({"count": cached_count, "cached": True})

        images = _get_or_fetch_images(rule_id, topic_id, detail_url)
        return jsonify({"count": len(images), "cached": False})
    except Exception as exc:  # noqa: BLE001
        if cached_count > 0:
            return jsonify({"count": cached_count, "cached": True})
        return jsonify({"error": f"主题数量获取失败: {exc}"}), 502


@bp.get("/api/online/image-proxy")
def api_online_image_proxy():
    image_url = request.args.get("url", "").strip()
    referer = request.args.get("referer", "").strip()

    if not image_url:
        return jsonify({"error": "url is required"}), 400
    if not _is_safe_remote_url(image_url):
        return jsonify({"error": "unsafe image url"}), 400

    resp, warn = _fetch_image_with_fallbacks(image_url=image_url, referer=referer)
    if resp is None:
        fallback_url = _pick_direct_redirect_url(image_url)
        if fallback_url:
            return redirect(fallback_url, code=302)
        return jsonify({"error": f"image proxy failed: {warn}"}), 502

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    out_headers: dict[str, str] = {}
    if warn:
        out_headers["X-PicCrawler-Proxy-Warn"] = warn[:200]

    def _generate():
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(_generate(), content_type=content_type, headers=out_headers)

@bp.post("/api/download")
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

    if not image_urls:
        return jsonify({"error": "No image found in topic"}), 400

    worker = current_app.extensions["download_worker"]
    job_id = worker.submit(rule_id, topic_id, title, detail_url, download_dir, image_urls)
    return jsonify({"ok": True, "job_id": job_id, "total_images": len(image_urls)})


@bp.get("/api/download/jobs")
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


@bp.get("/api/shelves")
def api_shelves():
    return jsonify({"items": list_shelves()})


@bp.post("/api/shelves")
def api_create_shelf():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    roots = payload.get("roots") or []
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not isinstance(roots, list) or not roots:
        return jsonify({"error": "roots must be non-empty array"}), 400

    normalized: list[str] = []
    for root in roots[:10]:
        resolved = str(Path(str(root)).expanduser().resolve())
        normalized.append(resolved)

    try:
        shelf_id = create_custom_shelf(name, normalized)
    except sqlite3.IntegrityError:
        return jsonify({"error": "书架名称已存在"}), 409

    return jsonify({"ok": True, "shelf_id": shelf_id})


@bp.post("/api/shelves/<int:shelf_id>/refresh")
def api_refresh_shelf(shelf_id: int):
    try:
        result = refresh_shelf(shelf_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True, "result": result})


@bp.delete("/api/shelves/<int:shelf_id>")
def api_delete_shelf(shelf_id: int):
    try:
        result = delete_custom_shelf(shelf_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    return jsonify({"ok": True, "result": result})


@bp.get("/api/shelves/<int:shelf_id>/topics")
def api_shelf_topics(shelf_id: int):
    page = max(1, int(request.args.get("page", "1")))
    page_size = max(1, min(100, int(request.args.get("page_size", "20"))))
    offset = (page - 1) * page_size
    query = str(request.args.get("q", "")).strip()
    like_query = f"%{query}%"

    if query:
        total_row = query_one(
            """
            SELECT COUNT(1) AS cnt
            FROM library_topics
            WHERE shelf_id = ? AND title LIKE ? COLLATE NOCASE
            """,
            (shelf_id, like_query),
        )
    else:
        total_row = query_one(
            "SELECT COUNT(1) AS cnt FROM library_topics WHERE shelf_id = ?",
            (shelf_id,),
        )
    total = int(total_row["cnt"]) if total_row else 0

    if query:
        rows = query_all(
            """
            SELECT topic_id, title, rel_path, cover_path, total_images, updated_at
            FROM library_topics
            WHERE shelf_id = ? AND title LIKE ? COLLATE NOCASE
            ORDER BY updated_at DESC, topic_id DESC
            LIMIT ? OFFSET ?
            """,
            (shelf_id, like_query, page_size, offset),
        )
    else:
        rows = query_all(
            """
            SELECT topic_id, title, rel_path, cover_path, total_images, updated_at
            FROM library_topics
            WHERE shelf_id = ?
            ORDER BY updated_at DESC, topic_id DESC
            LIMIT ? OFFSET ?
            """,
            (shelf_id, page_size, offset),
        )

    items = []
    for row in rows:
        item = dict(row)
        item["cover_url"] = (
            f"/api/library/topic-cover/{item['topic_id']}" if item["cover_path"] else ""
        )
        items.append(item)

    return jsonify(
        {
            "items": items,
            "page": page,
            "page_size": page_size,
            "q": query,
            "total": total,
            "has_more": offset + page_size < total,
        }
    )


@bp.get("/api/shelves/topic/<int:topic_id>/images")
def api_shelf_topic_images(topic_id: int):
    offset = max(0, int(request.args.get("offset", "0")))
    limit = max(1, min(100, int(request.args.get("limit", "20"))))

    rows = query_all(
        """
        SELECT image_id, image_index
        FROM library_images
        WHERE topic_id = ?
        ORDER BY image_index
        LIMIT ? OFFSET ?
        """,
        (topic_id, limit, offset),
    )

    total_row = query_one(
        "SELECT COUNT(1) AS cnt FROM library_images WHERE topic_id = ?",
        (topic_id,),
    )
    total = int(total_row["cnt"]) if total_row else 0

    items = [
        {
            "image_id": int(row["image_id"]),
            "image_index": int(row["image_index"]),
            "image_url": f"/api/library/image/{int(row['image_id'])}",
        }
        for row in rows
    ]

    return jsonify(
        {
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + limit < total,
        }
    )


@bp.get("/api/library/image/<int:image_id>")
def api_library_image(image_id: int):
    row = query_one(
        "SELECT image_path FROM library_images WHERE image_id = ?",
        (image_id,),
    )
    if row is None:
        abort(404)

    path = Path(row["image_path"])
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


@bp.get("/api/library/topic-cover/<int:topic_id>")
def api_topic_cover(topic_id: int):
    row = query_one(
        "SELECT cover_path FROM library_topics WHERE topic_id = ?",
        (topic_id,),
    )
    if row is None or not row["cover_path"]:
        abort(404)

    path = Path(row["cover_path"])
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


def _is_rule_online_enabled(rule_id: str) -> bool:
    row = query_one("SELECT enabled FROM rules WHERE rule_id = ?", (rule_id,))
    if row is None:
        return False
    return int(row["enabled"]) == 1


def _cleanup_stale_download_jobs() -> None:
    # If there is at least one running task, queued tasks are still valid.
    running_row = query_one("SELECT COUNT(1) AS cnt FROM download_jobs WHERE status = 'running'")
    running_count = int(running_row["cnt"]) if running_row else 0
    if running_count > 0:
        return

    rows = query_all(
        "SELECT job_id, updated_at FROM download_jobs WHERE status = 'queued' ORDER BY updated_at DESC"
    )
    if not rows:
        return

    now = datetime.utcnow()
    stale_ids: list[str] = []
    for row in rows:
        updated_at = str(row["updated_at"] or "").strip()
        if not updated_at:
            stale_ids.append(str(row["job_id"]))
            continue
        try:
            ts = datetime.fromisoformat(updated_at)
        except ValueError:
            stale_ids.append(str(row["job_id"]))
            continue
        if now - ts > timedelta(seconds=60):
            stale_ids.append(str(row["job_id"]))

    for job_id in stale_ids:
        execute(
            """
            UPDATE download_jobs
            SET status='failed',
                error_message=CASE
                    WHEN error_message IS NULL OR error_message = ''
                    THEN '任务未执行，已自动清理'
                    ELSE error_message
                END,
                updated_at=?
            WHERE job_id=?
            """,
            (now.isoformat(timespec="seconds"), job_id),
        )


def _get_cached_topics(rule_id: str, page_no: int) -> list[dict]:
    rows = query_all(
        """
        SELECT topic_id, title, cover_url, detail_url
        FROM online_topic_cache
        WHERE rule_id = ? AND page_no = ?
        ORDER BY rowid
        """,
        (rule_id, page_no),
    )
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        if not _is_displayable_image_url(item.get("cover_url", "")):
            item["cover_url"] = ""
        items.append(item)
    return items


def _save_topics(rule_id: str, page_no: int, topics: list[dict]) -> None:
    execute(
        "DELETE FROM online_topic_cache WHERE rule_id = ? AND page_no = ?",
        (rule_id, page_no),
    )
    for item in topics:
        execute(
            """
            INSERT INTO online_topic_cache(
                rule_id, page_no, topic_id, title, cover_url, detail_url, cached_at
            ) VALUES(?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                rule_id,
                page_no,
                item["topic_id"],
                item["title"],
                item.get("cover_url", ""),
                item["detail_url"],
            ),
        )


def _is_low_quality_gallery_url(rule_id: str, url: str) -> bool:
    if rule_id not in {"wnacg", "manxiangge"}:
        return False

    lower = str(url or "").lower()
    if not lower:
        return True

    parsed = urlparse(lower)
    host = parsed.hostname or ""
    path = parsed.path or ""

    # Legacy malformed output like https://www.wnacg.com//t4.xxx/data/t/...
    if host in {"www.wnacg.com", "wnacg.com"} and path.startswith("//"):
        return True

    if "/data/t/" in path:
        return True
    if "/thumb/" in path or "/thumbnail/" in path or "/preview/" in path:
        return True
    if re.match(r"^t\d+\.", host):
        return True
    if host.startswith("t") and "/data/" in path:
        return True

    return False


def _cached_images_need_refresh(rule_id: str, urls: list[str]) -> bool:
    if not urls:
        return False
    if rule_id == "hitomi-chinese":
        # Hitomi URL algorithm updates can invalidate cached domains/paths;
        # always refresh to avoid serving stale 404 links.
        return True
    if rule_id not in {"wnacg", "manxiangge"}:
        return False

    return any(_is_low_quality_gallery_url(rule_id, u) for u in urls)


def _get_or_fetch_images(rule_id: str, topic_id: str, detail_url: str) -> list[str]:
    rows = query_all(
        """
        SELECT image_url
        FROM online_image_cache
        WHERE rule_id = ? AND topic_id = ?
        ORDER BY image_index
        """,
        (rule_id, topic_id),
    )
    if rows:
        cached = [str(r["image_url"]) for r in rows]
        cached = [u for u in cached if _is_displayable_image_url(u)]
        if cached and not _cached_images_need_refresh(rule_id, cached):
            return cached

    crawler = build_crawler(rule_id)
    raw_images = crawler.topic_images(detail_url)

    preferred: list[str] = []
    fallback: list[str] = []
    seen_urls: set[str] = set()

    for raw_url in raw_images:
        if not _is_displayable_image_url(raw_url):
            continue
        if raw_url in seen_urls:
            continue

        seen_urls.add(raw_url)
        fallback.append(raw_url)

        if _is_low_quality_gallery_url(rule_id, raw_url):
            continue

        preferred.append(raw_url)

    images = preferred if preferred else fallback

    execute(
        "DELETE FROM online_image_cache WHERE rule_id = ? AND topic_id = ?",
        (rule_id, topic_id),
    )
    for idx, image_url in enumerate(images, start=1):
        execute(
            """
            INSERT INTO online_image_cache(rule_id, topic_id, image_index, image_url, cached_at)
            VALUES(?, ?, ?, ?, datetime('now'))
            """,
            (rule_id, topic_id, idx, image_url),
        )
    return images


def _is_displayable_image_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    if lower.startswith("data:") or lower.startswith("javascript:"):
        return False
    if "base64," in lower:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return bool(parsed.netloc)


def _normalize_remote_image_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    # WordPress CDN wrapper: i0.wp.com/<origin-host>/<path>?w=1300
    if host.endswith(".wp.com"):
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and "." in parts[0]:
            origin_host = parts[0].strip().lower()
            origin_path = "/" + parts[1]
            return urlunparse(("https", origin_host, origin_path, "", "", ""))

    return url


def _wrap_wp_proxy_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or host.endswith(".wp.com"):
        return url

    # Keep WNACG raw image host to avoid wp.com upstream 400/502.
    if host.endswith("qy0.ru"):
        return url

    # Only use WordPress CDN wrapper for known unstable sources.
    if not host.endswith("4khd.com"):
        return url

    # Wrap origin image by WordPress image CDN as a fallback display URL.
    width = ""
    match = re.search(r"/w(\d+)-rw/", parsed.path)
    if match:
        width = match.group(1)

    query = parsed.query.strip()
    if width and "w=" not in query:
        query = f"w={width}" if not query else f"{query}&w={width}"

    wrapped_path = f"/{host}{parsed.path}"
    return urlunparse(("https", "i0.wp.com", wrapped_path, "", query, ""))


def _prefer_display_image_url(url: str) -> str:
    if not url:
        return ""

    normalized = _normalize_remote_image_url(url)
    wrapped = _wrap_wp_proxy_url(normalized)
    if _is_displayable_image_url(wrapped) and wrapped != normalized:
        return wrapped
    return url


def _proxy_remote_image_url(image_url: str, referer: str = "") -> str:
    if not image_url:
        return ""
    if image_url.startswith("/api/online/image-proxy"):
        return image_url

    display_url = _prefer_display_image_url(image_url)
    encoded = quote(display_url, safe="")
    if referer:
        encoded_ref = quote(referer, safe="")
        return f"/api/online/image-proxy?url={encoded}&referer={encoded_ref}"
    return f"/api/online/image-proxy?url={encoded}"


def _is_safe_remote_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    except ValueError:
        pass

    return True


def _build_proxy_headers(referer: str = "", minimal: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if minimal:
        return headers
    if referer and _is_safe_remote_url(referer):
        headers["Referer"] = referer
        parsed_referer = urlparse(referer)
        headers["Origin"] = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
    return headers


def _http_fallback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return url
    return urlunparse(("http", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _candidate_fetch_urls(image_url: str) -> list[str]:
    raw = image_url
    normalized = _normalize_remote_image_url(raw)
    wrapped = _wrap_wp_proxy_url(normalized)
    display = _prefer_display_image_url(raw)

    candidates: list[str] = [display, raw, wrapped, normalized]

    for u in [display, raw, wrapped, normalized]:
        if u and urlparse(u).scheme == "https":
            candidates.append(_http_fallback_url(u))

    unique: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _pick_direct_redirect_url(image_url: str) -> str:
    candidates = _candidate_fetch_urls(image_url)

    for url in candidates:
        host = (urlparse(url).hostname or "").lower()
        if host.endswith(".wp.com") and _is_safe_remote_url(url):
            return url

    for url in candidates:
        if urlparse(url).scheme == "https" and _is_safe_remote_url(url):
            return url

    for url in candidates:
        if _is_safe_remote_url(url):
            return url

    return ""


def _try_fetch_image(url: str, headers: dict[str, str]) -> RequestsResponse:
    return requests.get(
        url,
        headers=headers,
        timeout=25,
        stream=True,
        allow_redirects=True,
        verify=True,
    )


def _looks_like_image_response(resp: RequestsResponse, request_url: str) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if content_type.startswith("image/"):
        return True

    path_lower = urlparse(request_url).path.lower()
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
    if any(path_lower.endswith(ext) for ext in image_exts) and resp.status_code == 200:
        return True

    return False


def _fetch_image_with_fallbacks(image_url: str, referer: str) -> tuple[RequestsResponse | None, str]:
    attempt_errors: list[str] = []

    for candidate in _candidate_fetch_urls(image_url):
        if not _is_safe_remote_url(candidate):
            continue

        plans = [
            (candidate, _build_proxy_headers(referer=referer, minimal=False), "referer"),
            (candidate, _build_proxy_headers(referer=referer, minimal=True), "minimal"),
        ]

        for url, headers, label in plans:
            try:
                resp = _try_fetch_image(url=url, headers=headers)
            except RequestException as exc:
                attempt_errors.append(f"{label}:{urlparse(url).netloc}:{exc.__class__.__name__}")
                continue

            if resp.status_code == 200 and _looks_like_image_response(resp, request_url=url):
                warn = ""
                if url != image_url or label != "referer":
                    warn = f"fallback={label}:{urlparse(url).netloc}"
                return resp, warn

            content_type = (resp.headers.get("Content-Type") or "").lower()
            attempt_errors.append(
                f"{label}:{urlparse(url).netloc}:status={resp.status_code}:ct={content_type[:32]}"
            )
            resp.close()

    return None, ",".join(attempt_errors[:8]) or "unknown"
















