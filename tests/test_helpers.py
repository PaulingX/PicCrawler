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


def test_header_safe_url():
    from app.services.utils import header_safe_url

    # 纯 ASCII 原样返回
    assert header_safe_url("https://hitomi.la/reader/123.html") == "https://hitomi.la/reader/123.html"
    assert header_safe_url("") == ""
    # 非 ASCII 字符被百分号编码，ASCII 部分不被二次编码
    out = header_safe_url("https://hitomi.la/manga/xxx-中文-4184194.html")
    assert out == "https://hitomi.la/manga/xxx-%E4%B8%AD%E6%96%87-4184194.html"
    # 已有百分号转义不被二次编码
    assert header_safe_url("https://x.com/a%20b-中.jpg") == "https://x.com/a%20b-%E4%B8%AD.jpg"
    # 结果必须是 latin-1 可编码（可安全放入 HTTP 头）
    out.encode("latin-1")


def test_hitomi_gallery_id_from_slug_url():
    from app.services.crawler_hitomi import CrawlerHitomi

    crawler = CrawlerHitomi()
    # /reader/ 与 /galleries/ 形态
    assert crawler._extract_gallery_id("https://hitomi.la/reader/4184194.html") == 4184194
    assert crawler._extract_gallery_id("https://hitomi.la/galleries/4184194.html") == 4184194
    # galleryurl 的 /manga/ 中文 slug 形态（此前会解析失败导致下载报错）
    assert (
        crawler._extract_gallery_id(
            "https://hitomi.la/manga/dorei-onna-kyoushi-keiko-1--decensored--中文-4184194.html"
        )
        == 4184194
    )
    assert crawler._extract_gallery_id("4184194") == 4184194
    assert crawler._extract_gallery_id("https://hitomi.la/index-chinese.html") is None


def test_download_normalize_imgbox_and_wp():
    from app.services.download_worker import _normalize_download_image_url

    # imgbox 直链会被改写为 Photon 包装（直链现在返回占位图）
    assert (
        _normalize_download_image_url("https://images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg")
        == "https://i1.wp.com/images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg"
    )
    # 非 4khd 的 wp.com 包装保持原样
    assert (
        _normalize_download_image_url("https://i1.wp.com/images2.imgbox.com/2c/e6/x_o.jpg")
        == "https://i1.wp.com/images2.imgbox.com/2c/e6/x_o.jpg"
    )
