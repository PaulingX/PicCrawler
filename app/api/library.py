"""本地书架/图库接口：书架列表、创建、刷新、删除，以及主题与图片浏览。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

from app.database import query_all, query_one
from app.services.library_scanner import (
    create_custom_shelf,
    delete_custom_shelf,
    list_shelves,
    refresh_shelf,
)

bp = Blueprint("library", __name__, url_prefix="/api")


@bp.get("/shelves")
def api_shelves():
    return jsonify({"items": list_shelves()})


@bp.post("/shelves")
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


@bp.post("/shelves/<int:shelf_id>/refresh")
def api_refresh_shelf(shelf_id: int):
    try:
        result = refresh_shelf(shelf_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"ok": True, "result": result})


@bp.delete("/shelves/<int:shelf_id>")
def api_delete_shelf(shelf_id: int):
    try:
        result = delete_custom_shelf(shelf_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    return jsonify({"ok": True, "result": result})


@bp.get("/shelves/<int:shelf_id>/topics")
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


@bp.get("/shelves/topic/<int:topic_id>/images")
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


@bp.get("/library/image/<int:image_id>")
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


@bp.get("/library/topic-cover/<int:topic_id>")
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
