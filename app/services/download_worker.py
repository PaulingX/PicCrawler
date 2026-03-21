from __future__ import annotations

import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask

from app.database import execute
from app.services.utils import guess_ext, sanitize_name


class DownloadWorker:
    def __init__(self, app: Flask) -> None:
        self.app = app
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
                "title": title,
                "target_dir": target_dir,
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

        for idx, url in enumerate(image_urls, start=1):
            try:
                res = requests.get(url, timeout=25, stream=True)
                res.raise_for_status()
                ext = guess_ext(url)
                file_path = target / f"{idx:04d}{ext}"
                with file_path.open("wb") as f:
                    for chunk in res.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
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

        execute(
            """
            UPDATE download_jobs
            SET status=?, error_message=?, updated_at=?
            WHERE job_id=?
            """,
            (final_status, "\n".join(errors[:20]) if errors else "", _utcnow(), job_id),
        )


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")
