"""端点行为测试：路由拆分后，关键接口的输入输出应与单体版一致。

对需要联网抓取的在线接口，用 monkeypatch 替换 build_crawler，避免沙箱无网。
"""
from __future__ import annotations


class _FakeCrawler:
    def list_topics(self, page, query="", category_id=None):
        return [
            {
                "topic_id": "t1",
                "title": "主题A",
                "cover_url": "https://x.com/c.jpg",
                "detail_url": "https://x.com/t/1",
            }
        ]

    def topic_images(self, detail_url):
        return ["https://x.com/1.jpg", "https://x.com/2.jpg"]


def _patch_crawler(monkeypatch):
    fake = _FakeCrawler()
    monkeypatch.setattr("app.api.online.build_crawler", lambda rule_id: fake)
    monkeypatch.setattr("app.services.image_proxy.build_crawler", lambda rule_id: fake)


def test_rules_list_shape(client):
    resp = client.get("/api/rules")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "items" in data
    assert len(data["items"]) > 0
    item = data["items"][0]
    for key in ("rule_id", "name", "base_url", "enabled", "supports_search", "categories", "download_dir"):
        assert key in item


def test_set_rule_enabled_toggle(client, app):
    resp = client.post("/api/rules/4khd/enabled", json={"enabled": 0})
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] == 0
    # 关闭后在线下拉接口应返回 403
    resp2 = client.get("/api/online/topics?rule=4khd")
    assert resp2.status_code == 403
    # 恢复
    client.post("/api/rules/4khd/enabled", json={"enabled": 1})


def test_set_rule_enabled_missing_field(client):
    resp = client.post("/api/rules/4khd/enabled", json={})
    assert resp.status_code == 400


def test_download_missing_fields_400(client):
    resp = client.post("/api/download", json={"rule": "4khd"})
    assert resp.status_code == 400


def test_download_jobs_empty(client):
    resp = client.get("/api/download/jobs")
    assert resp.status_code == 200
    assert resp.get_json()["items"] == []


def test_system_directories_root(client):
    resp = client.get("/api/system/directories")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["is_root"] is True
    assert isinstance(data["items"], list)


def test_image_proxy_unsafe_url_400(client):
    resp = client.get("/api/online/image-proxy?url=http://localhost/x.jpg")
    assert resp.status_code == 400


def test_image_proxy_missing_url_400(client):
    resp = client.get("/api/online/image-proxy")
    assert resp.status_code == 400


def test_online_topics_proxies_cover(client, monkeypatch):
    _patch_crawler(monkeypatch)
    resp = client.get("/api/online/topics?rule=4khd&page=1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["items"]) == 1
    cover = data["items"][0]["cover_url"]
    assert cover.startswith("/api/online/image-proxy")


def test_online_topic_images_paged(app, monkeypatch):
    # 直接使用 image_proxy 的 _get_or_fetch_images（已被 mock 的 build_crawler 覆盖）
    _patch_crawler(monkeypatch)
    from app.services.image_proxy import _get_or_fetch_images

    imgs = _get_or_fetch_images("4khd", "t1", "https://x.com/t/1")
    assert imgs == ["https://x.com/1.jpg", "https://x.com/2.jpg"]


def test_image_proxy_sets_cache_control(client, monkeypatch):
    class _FakeResp:
        headers = {"Content-Type": "image/jpeg"}

        def iter_content(self, chunk_size=65536):
            yield b"img-bytes"

        def close(self):
            pass

    def _fake_fetch(image_url, referer):
        return _FakeResp(), ""

    monkeypatch.setattr("app.api.online._fetch_image_with_fallbacks", _fake_fetch)
    resp = client.get("/api/online/image-proxy?url=https%3A%2F%2Fexample.com%2Fa.jpg")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "public, max-age=86400"


def test_download_job_cancel_unknown_404(client):
    resp = client.post("/api/download/jobs/nope/cancel")
    assert resp.status_code == 404


def test_download_job_cancel_finished_job(client):
    from app.database import execute

    execute(
        """
        INSERT INTO download_jobs(
            job_id, rule_id, topic_id, title, detail_url, target_dir,
            status, total_images, downloaded_images, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("done-1", "4khd", "t1", "T", "https://x/1", "/tmp/x", "done", 3, 3, "", ""),
    )
    resp = client.post("/api/download/jobs/done-1/cancel")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["canceled"] is False
    assert data["reason"] == "already_done"


def test_download_job_cancel_running_via_endpoint(client, app, monkeypatch):
    import threading
    import time

    import app.services.download_worker as dw
    from app.database import query_one

    started = threading.Event()
    release = threading.Event()

    def _slow(session, image_url, referer=""):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("blocked")

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _slow)

    worker = app.extensions["download_worker"]
    job_id = worker.submit(
        rule_id="4khd",
        topic_id="t-ep",
        title="端点取消主题",
        detail_url="https://example.com/x",
        target_dir=str(app.config["DOWNLOAD_ROOT"]),
        image_urls=["https://example.com/1.jpg", "https://example.com/2.jpg"],
    )

    assert started.wait(timeout=10), "下载任务未开始"

    try:
        resp = client.post(f"/api/download/jobs/{job_id}/cancel")
        assert resp.status_code == 200
        assert resp.get_json()["canceled"] is True
    finally:
        release.set()

    deadline = time.time() + 10
    row = None
    while time.time() < deadline:
        row = query_one("SELECT status FROM download_jobs WHERE job_id=?", (job_id,))
        if row and row["status"] in ("done", "failed", "partial", "canceled"):
            break
        time.sleep(0.05)
    assert row is not None and row["status"] == "canceled"
