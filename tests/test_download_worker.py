"""下载 Worker 并发改造后的行为测试（离线：用 monkeypatch 模拟下载失败，验证线程池执行路径）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from app.database import query_one
from app.services.utils import sanitize_name


def _poll_status(job_id: str, timeout: float = 10) -> dict | None:
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = query_one(
            "SELECT status, error_message, downloaded_images FROM download_jobs WHERE job_id=?", (job_id,)
        )
        if row and row["status"] in ("done", "failed", "partial", "canceled"):
            return row
        time.sleep(0.05)
    return row


class _FakeResponse:
    """模拟 requests 的流式图片响应。"""

    def __init__(self, content: bytes):
        self.headers = {"Content-Type": "image/jpeg"}
        self._content = content

    def iter_content(self, chunk_size: int = 65536):
        yield self._content

    def close(self) -> None:
        pass


def test_worker_pool_executes_task_and_fails(app, monkeypatch):
    import app.services.download_worker as dw

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated download failure")

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _boom)

    worker = app.extensions["download_worker"]
    assert worker.max_workers >= 1

    job_id = worker.submit(
        rule_id="4khd",
        topic_id="t-bogus",
        title="Bogus主题",
        detail_url="https://example.com/x",
        target_dir=str(app.config["DOWNLOAD_ROOT"]),
        image_urls=["https://example.com/a.jpg"],
    )

    row = _poll_status(job_id)
    assert row is not None, "任务未被线程池执行"
    assert row["status"] == "failed"
    assert "simulated" in (row["error_message"] or "")


def test_worker_runs_multiple_tasks(app, monkeypatch):
    import app.services.download_worker as dw

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _boom)

    worker = app.extensions["download_worker"]
    job_ids: list[str] = []
    for i in range(3):
        job_ids.append(
            worker.submit(
                rule_id="4khd",
                topic_id=f"t-{i}",
                title=f"主题{i}",
                detail_url="https://example.com/x",
                target_dir=str(app.config["DOWNLOAD_ROOT"]),
                image_urls=["https://example.com/a.jpg"],
            )
        )

    for job_id in job_ids:
        row = _poll_status(job_id)
        assert row is not None
        assert row["status"] == "failed"


def test_worker_downloads_images_and_names_by_index(app, monkeypatch):
    """图片级并发后，落盘文件仍按 URL 序号命名，内容与序号一一对应。"""
    import app.services.download_worker as dw

    def _ok(session, image_url, referer=""):
        return _FakeResponse(image_url.encode("utf-8")), image_url

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _ok)

    worker = app.extensions["download_worker"]
    urls = [f"https://example.com/{i}.jpg" for i in range(1, 9)]
    job_id = worker.submit(
        rule_id="4khd",
        topic_id="t-ok",
        title="并发主题",
        detail_url="https://example.com/x",
        target_dir=str(app.config["DOWNLOAD_ROOT"]),
        image_urls=urls,
    )

    row = _poll_status(job_id)
    assert row is not None, "任务未被线程池执行"
    assert row["status"] == "done", row["error_message"]
    assert row["downloaded_images"] == 8

    topic_dir = Path(app.config["DOWNLOAD_ROOT"]) / sanitize_name("并发主题")
    for i, url in enumerate(urls, start=1):
        f = topic_dir / f"{i:04d}.jpg"
        assert f.is_file(), f"缺少文件 {f.name}"
        assert f.read_bytes() == url.encode("utf-8"), f"{f.name} 内容与序号不匹配"


def test_worker_retries_failed_images_once(app, monkeypatch):
    """首轮失败的图片会在收尾时重试一轮，成功后整体 done。"""
    import app.services.download_worker as dw

    calls: dict[str, int] = {}

    def _flaky(session, image_url, referer=""):
        calls[image_url] = calls.get(image_url, 0) + 1
        if image_url.endswith("2.jpg") and calls[image_url] == 1:
            raise RuntimeError("transient")
        return _FakeResponse(b"ok"), image_url

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _flaky)

    worker = app.extensions["download_worker"]
    urls = ["https://example.com/1.jpg", "https://example.com/2.jpg", "https://example.com/3.jpg"]
    job_id = worker.submit(
        rule_id="4khd",
        topic_id="t-retry",
        title="重试主题",
        detail_url="https://example.com/x",
        target_dir=str(app.config["DOWNLOAD_ROOT"]),
        image_urls=urls,
    )

    row = _poll_status(job_id)
    assert row is not None
    assert row["status"] == "done", row["error_message"]
    assert calls["https://example.com/2.jpg"] == 2
    assert calls["https://example.com/1.jpg"] == 1


def test_cancel_running_job_marks_canceled(app, monkeypatch):
    import app.services.download_worker as dw

    started = threading.Event()
    release = threading.Event()

    def _slow(session, image_url, referer=""):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("still blocked")

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _slow)

    worker = app.extensions["download_worker"]
    urls = [f"https://example.com/{i}.jpg" for i in range(1, 6)]
    job_id = worker.submit(
        rule_id="4khd",
        topic_id="t-cancel",
        title="取消主题",
        detail_url="https://example.com/x",
        target_dir=str(app.config["DOWNLOAD_ROOT"]),
        image_urls=urls,
    )

    assert started.wait(timeout=10), "下载任务未开始"
    assert worker.cancel(job_id) is True
    release.set()

    row = _poll_status(job_id)
    assert row is not None
    assert row["status"] == "canceled"


def test_cancel_queued_job_before_start(app, monkeypatch):
    """单线程池中第二个任务必然排队：排队中的任务可直接出队取消。"""
    import app.services.download_worker as dw

    started = threading.Event()
    release = threading.Event()

    def _slow(session, image_url, referer=""):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("blocked")

    monkeypatch.setattr(dw, "_download_image_with_fallbacks", _slow)

    worker = dw.DownloadWorker(app, max_workers=1)
    try:
        job_a = worker.submit(
            rule_id="4khd",
            topic_id="qa",
            title="A主题",
            detail_url="https://example.com/x",
            target_dir=str(app.config["DOWNLOAD_ROOT"]),
            image_urls=["https://example.com/a1.jpg"],
        )
        job_b = worker.submit(
            rule_id="4khd",
            topic_id="qb",
            title="B主题",
            detail_url="https://example.com/x",
            target_dir=str(app.config["DOWNLOAD_ROOT"]),
            image_urls=["https://example.com/b1.jpg"],
        )

        assert started.wait(timeout=10), "首个任务未开始"
        assert worker.cancel(job_b) is True
        row_b = query_one("SELECT status FROM download_jobs WHERE job_id=?", (job_b,))
        assert row_b["status"] == "canceled"
        assert not Path(app.config["DOWNLOAD_ROOT"]).joinpath(sanitize_name("B主题")).exists()
    finally:
        release.set()
        _poll_status(job_a)
        worker.shutdown()
