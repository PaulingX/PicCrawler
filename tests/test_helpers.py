"""路由层共享辅助函数（依赖 DB）的行为测试。"""
from __future__ import annotations

from datetime import timedelta

import app.api.helpers as helpers
from app.database import execute, query_one
from app.services.utils import utcnow


def test_is_rule_online_enabled(app):
    # 默认规则 4khd 在线浏览开启
    assert helpers._is_rule_online_enabled("4khd") is True
    assert helpers._is_rule_online_enabled("__nope__") is False


def test_save_and_get_cached_topics(app):
    topics = [
        {
            "topic_id": "t1",
            "title": "主题1",
            "cover_url": "https://x.com/c.jpg",
            "detail_url": "https://x.com/t/1",
        },
        {
            "topic_id": "t2",
            "title": "主题2",
            "cover_url": "not-an-image",
            "detail_url": "https://x.com/t/2",
        },
    ]
    helpers._save_topics("4khd", 1, topics)
    cached = helpers._get_cached_topics("4khd", 1)
    assert len(cached) == 2
    assert cached[0]["cover_url"] == "https://x.com/c.jpg"
    # 不可展示的封面会被清空
    assert cached[1]["cover_url"] == ""


def test_cleanup_stale_running_jobs(app):
    old = (utcnow() - timedelta(minutes=30)).isoformat(timespec="seconds")
    execute(
        """
        INSERT INTO download_jobs(
            job_id, rule_id, topic_id, title, detail_url, target_dir,
            status, total_images, downloaded_images, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("stale-1", "4khd", "t1", "T", "https://x/1", "/tmp/x", "running", 5, 0, old, old),
    )
    helpers._cleanup_stale_download_jobs()
    row = query_one("SELECT status, error_message FROM download_jobs WHERE job_id='stale-1'")
    assert row["status"] == "failed"
    assert row["error_message"]
