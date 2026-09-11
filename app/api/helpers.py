"""多个蓝图共用的路由层辅助函数（依赖 DB，但与具体图片 URL 逻辑解耦）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.database import execute, query_all, query_one
from app.services.utils import utcnow as _utcnow_dt


def _is_rule_online_enabled(rule_id: str) -> bool:
    row = query_one("SELECT enabled FROM rules WHERE rule_id = ?", (rule_id,))
    if row is None:
        return False
    return int(row["enabled"]) == 1


def _cleanup_stale_download_jobs() -> None:
    """标记长时间无进度的 running/queued 任务为失败，避免 UI 永远卡在"进行中"。"""
    now = _utcnow_dt()
    running_rows = query_all(
        "SELECT job_id, updated_at FROM download_jobs WHERE status = 'running' ORDER BY updated_at DESC"
    )
    stale_running_ids: list[str] = []
    for row in running_rows:
        updated_at = str(row["updated_at"] or "").strip()
        if not updated_at:
            stale_running_ids.append(str(row["job_id"]))
            continue
        try:
            ts = datetime.fromisoformat(updated_at)
        except ValueError:
            stale_running_ids.append(str(row["job_id"]))
            continue
        # Keep threshold conservative to avoid killing slow but healthy downloads.
        if now - ts > timedelta(minutes=20):
            stale_running_ids.append(str(row["job_id"]))

    for job_id in stale_running_ids:
        execute(
            """
            UPDATE download_jobs
            SET status='failed',
                error_message=CASE
                    WHEN error_message IS NULL OR error_message = ''
                    THEN '任务长时间无进度，已自动结束'
                    ELSE error_message
                END,
                updated_at=?
            WHERE job_id=?
            """,
            (now.isoformat(timespec="seconds"), job_id),
        )

    # If there is still at least one running task, queued tasks are still valid.
    running_row = query_one("SELECT COUNT(1) AS cnt FROM download_jobs WHERE status = 'running'")
    running_count = int(running_row["cnt"]) if running_row else 0
    if running_count > 0:
        return

    rows = query_all(
        "SELECT job_id, updated_at FROM download_jobs WHERE status = 'queued' ORDER BY updated_at DESC"
    )
    if not rows:
        return

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
        if not _is_displayable_cover(item.get("cover_url", "")):
            item["cover_url"] = ""
        items.append(item)
    return items


def _is_displayable_cover(url: str) -> bool:
    from app.services.image_proxy import _is_displayable_image_url

    return _is_displayable_image_url(url)


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
