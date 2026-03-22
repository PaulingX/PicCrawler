from __future__ import annotations

import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from flask import Flask

from app.config import USER_AGENT
from app.database import execute
from app.services.library_scanner import upsert_downloaded_topic
from app.services.utils import guess_ext, sanitize_name


class DownloadWorker:
    def __init__(self, app: Flask) -> None:
        self.app = app
        # Recover interrupted tasks from previous process to avoid
        # leaving stale queued/running items in UI forever.
        with self.app.app_context():
            self._recover_interrupted_jobs()
        self._queue: queue.Queue[dict] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(
        self,
        rule_id: str,
        topic_id: str,
        title: str,
        detail_url: str,
        target_dir: str,
        image_urls: list[str],
    ) -> str:
        now = _utcnow()
        job_id = str(uuid.uuid4())
        execute(
            """
            INSERT INTO download_jobs(
                job_id, rule_id, topic_id, title, detail_url, target_dir,
                status, total_images, downloaded_images, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?)
            """,
            (
                job_id,
                rule_id,
                topic_id,
                title,
                detail_url,
                target_dir,
                len(image_urls),
                now,
                now,
            ),
        )
        self._queue.put(
            {
                "job_id": job_id,
                "rule_id": rule_id,
                "title": title,
                "target_dir": target_dir,
                "detail_url": detail_url,
                "image_urls": image_urls,
            }
        )
        return job_id

    def _loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            with self.app.app_context():
                self._run_task(task)

    def _run_task(self, task: dict) -> None:
        job_id = task["job_id"]
        now = _utcnow()
        execute(
            "UPDATE download_jobs SET status='running', updated_at=? WHERE job_id=?",
            (now, job_id),
        )

        target = Path(task["target_dir"]).resolve() / sanitize_name(task["title"])
        target.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        errors: list[str] = []
        image_urls: list[str] = task["image_urls"]
        detail_url = str(task.get("detail_url", "")).strip()
        session = requests.Session()

        for idx, url in enumerate(image_urls, start=1):
            try:
                res, final_url = _download_image_with_fallbacks(
                    session=session,
                    image_url=url,
                    referer=detail_url,
                )
                ext = guess_ext(final_url)
                file_path = target / f"{idx:04d}{ext}"
                with file_path.open("wb") as f:
                    for chunk in res.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                res.close()
                downloaded += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{idx}:{exc}")

            execute(
                """
                UPDATE download_jobs
                SET downloaded_images=?, updated_at=?
                WHERE job_id=?
                """,
                (downloaded, _utcnow(), job_id),
            )

        final_status = "done" if downloaded > 0 and downloaded == len(image_urls) else "partial"
        if downloaded == 0:
            final_status = "failed"

        sync_error = ""
        if downloaded > 0:
            try:
                upsert_downloaded_topic(
                    rule_id=str(task.get("rule_id", "")).strip(),
                    root_dir=str(task["target_dir"]),
                    topic_dir=str(target),
                    title_hint=str(task.get("title", "")).strip(),
                )
            except Exception as exc:  # noqa: BLE001
                sync_error = f"书架自动入库失败: {exc}"

        merged_errors = "\n".join(errors[:20]) if errors else ""
        if sync_error:
            merged_errors = f"{merged_errors}\n{sync_error}".strip()

        execute(
            """
            UPDATE download_jobs
            SET status=?, error_message=?, updated_at=?
            WHERE job_id=?
            """,
            (final_status, merged_errors, _utcnow(), job_id),
        )

    def _recover_interrupted_jobs(self) -> None:
        now = _utcnow()
        execute(
            """
            UPDATE download_jobs
            SET status='failed',
                error_message=CASE
                    WHEN error_message IS NULL OR error_message = ''
                    THEN '任务被中断（应用重启），已自动结束'
                    ELSE error_message
                END,
                updated_at=?
            WHERE status IN ('queued', 'running')
            """,
            (now,),
        )


def _normalize_download_image_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    def _rewrite_pic_to_img(path: str, query: str) -> str:
        return urlunparse(("https", "img.4khd.com", path, "", query, ""))

    if host.endswith(".wp.com"):
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and "." in parts[0]:
            origin_host = parts[0].strip().lower()
            origin_path = "/" + parts[1]
            if origin_host == "pic.4khd.com":
                return _rewrite_pic_to_img(origin_path, parsed.query)
            return urlunparse(("https", origin_host, origin_path, "", parsed.query, ""))

    if host == "pic.4khd.com":
        return _rewrite_pic_to_img(parsed.path, parsed.query)

    return url


def _candidate_download_urls(url: str) -> list[str]:
    raw = str(url or "").strip()
    normalized = _normalize_download_image_url(raw)
    candidates: list[str] = [normalized, raw]

    for item in [normalized, raw]:
        if not item:
            continue
        parsed = urlparse(item)
        if parsed.scheme == "https":
            candidates.append(urlunparse(("http", parsed.netloc, parsed.path, "", parsed.query, "")))

    unique: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not item or item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _looks_like_image_response(resp: requests.Response, request_url: str) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if content_type.startswith("image/"):
        return True

    parsed = urlparse(request_url)
    path = (parsed.path or "").lower()
    return any(path.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"])


def _download_image_with_fallbacks(
    session: requests.Session,
    image_url: str,
    referer: str = "",
) -> tuple[requests.Response, str]:
    errors: list[str] = []
    referer_ok = referer.startswith("http://") or referer.startswith("https://")

    for candidate in _candidate_download_urls(image_url):
        plans = [
            {
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                **({"Referer": referer} if referer_ok else {}),
            },
            {"User-Agent": USER_AGENT},
        ]

        for headers in plans:
            try:
                resp = session.get(candidate, timeout=25, stream=True, headers=headers, allow_redirects=True)
                resp.raise_for_status()
                if not _looks_like_image_response(resp, request_url=resp.url or candidate):
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    errors.append(f"{candidate}:not_image:{content_type[:40]}")
                    resp.close()
                    continue
                return resp, (resp.url or candidate)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{candidate}:{exc.__class__.__name__}")
                continue

    raise RuntimeError(";".join(errors[:8]) or "download failed")


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")
