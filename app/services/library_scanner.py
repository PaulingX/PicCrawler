from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path

from app.database import execute, query_all, query_one
from app.services.utils import list_direct_images

# Scan folder and subfolders up to 2 levels.
MAX_DEPTH = 2


def refresh_shelf(shelf_id: int) -> dict:
    shelf = query_one(
        "SELECT shelf_id, roots_json FROM shelves WHERE shelf_id = ?",
        (shelf_id,),
    )
    if shelf is None:
        raise ValueError("Shelf not found")

    roots: list[str] = json.loads(shelf["roots_json"])

    execute("DELETE FROM library_images WHERE topic_id IN (SELECT topic_id FROM library_topics WHERE shelf_id = ?)", (shelf_id,))
    execute("DELETE FROM library_topics WHERE shelf_id = ?", (shelf_id,))

    topic_count = 0
    image_count = 0
    for root in roots:
        root_path = Path(root)
        if not root_path.exists() or not root_path.is_dir():
            continue

        for folder in _walk_folders(root_path, MAX_DEPTH):
            # Each topic represents one folder; only include images directly in it
            # to avoid duplicated images across parent/child topics.
            images = list_direct_images(folder)
            if not images:
                continue

            topic_count += 1
            image_count += len(images)
            rel_path = str(folder.relative_to(root_path)) if folder != root_path else "."
            topic_key = f"{root_path.resolve()}::{rel_path}"
            title = folder.name if folder != root_path else root_path.name
            cover = images[0]

            cur = execute(
                """
                INSERT INTO library_topics(
                    shelf_id, topic_key, title, abs_path, rel_path,
                    cover_path, total_images, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shelf_id,
                    topic_key,
                    title,
                    str(folder.resolve()),
                    rel_path,
                    str(cover.resolve()) if cover else "",
                    len(images),
                    _utcnow(),
                ),
            )
            topic_id = int(cur.lastrowid)

            for index, image in enumerate(images, start=1):
                execute(
                    """
                    INSERT INTO library_images(topic_id, image_path, image_index)
                    VALUES(?, ?, ?)
                    """,
                    (topic_id, str(image.resolve()), index),
                )

    execute(
        "UPDATE shelves SET updated_at = ? WHERE shelf_id = ?",
        (_utcnow(), shelf_id),
    )
    return {"topics": topic_count, "images": image_count}


def ensure_rule_shelf(rule_id: str, name: str, roots: list[str]) -> int:
    row = query_one(
        "SELECT shelf_id FROM shelves WHERE rule_id = ?",
        (rule_id,),
    )
    roots_json = json.dumps(roots, ensure_ascii=False)
    if row:
        execute(
            "UPDATE shelves SET name=?, roots_json=?, source_type='rule', updated_at=? WHERE shelf_id=?",
            (name, roots_json, _utcnow(), row["shelf_id"]),
        )
        return int(row["shelf_id"])

    cur = execute(
        """
        INSERT INTO shelves(name, roots_json, source_type, rule_id, updated_at)
        VALUES(?, ?, 'rule', ?, ?)
        """,
        (name, roots_json, rule_id, _utcnow()),
    )
    return int(cur.lastrowid)


def create_custom_shelf(name: str, roots: list[str]) -> int:
    cur = execute(
        """
        INSERT INTO shelves(name, roots_json, source_type, rule_id, updated_at)
        VALUES(?, ?, 'custom', NULL, ?)
        """,
        (name, json.dumps(roots, ensure_ascii=False), _utcnow()),
    )
    return int(cur.lastrowid)


def list_shelves() -> list[dict]:
    rows = query_all(
        """
        SELECT shelf_id, name, roots_json, source_type, rule_id, updated_at
        FROM shelves
        ORDER BY source_type, shelf_id
        """
    )
    items: list[dict] = []
    for row in rows:
        item = dict(row)
        item["roots"] = json.loads(item.pop("roots_json"))
        items.append(item)
    return items


def delete_custom_shelf(shelf_id: int) -> dict:
    shelf = query_one(
        "SELECT shelf_id, source_type, name FROM shelves WHERE shelf_id = ?",
        (shelf_id,),
    )
    if shelf is None:
        raise ValueError("Shelf not found")

    if str(shelf["source_type"]) != "custom":
        raise PermissionError("Only custom shelf can be deleted")

    topic_row = query_one(
        "SELECT COUNT(1) AS cnt FROM library_topics WHERE shelf_id = ?",
        (shelf_id,),
    )
    image_row = query_one(
        """
        SELECT COUNT(1) AS cnt
        FROM library_images
        WHERE topic_id IN (SELECT topic_id FROM library_topics WHERE shelf_id = ?)
        """,
        (shelf_id,),
    )
    topic_count = int(topic_row["cnt"]) if topic_row else 0
    image_count = int(image_row["cnt"]) if image_row else 0

    execute(
        "DELETE FROM library_images WHERE topic_id IN (SELECT topic_id FROM library_topics WHERE shelf_id = ?)",
        (shelf_id,),
    )
    execute("DELETE FROM library_topics WHERE shelf_id = ?", (shelf_id,))
    execute("DELETE FROM shelves WHERE shelf_id = ?", (shelf_id,))

    return {
        "shelf_id": int(shelf_id),
        "name": str(shelf["name"]),
        "deleted_topics": topic_count,
        "deleted_images": image_count,
    }


def _walk_folders(root: Path, max_depth: int) -> list[Path]:
    results: list[Path] = []
    queue: deque[tuple[Path, int]] = deque([(root, 0)])

    while queue:
        folder, depth = queue.popleft()
        results.append(folder)
        if depth >= max_depth:
            continue

        try:
            children = [p for p in folder.iterdir() if p.is_dir()]
        except OSError:
            continue
        children.sort(key=lambda p: p.name.lower())
        for child in children:
            queue.append((child, depth + 1))

    return results


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


