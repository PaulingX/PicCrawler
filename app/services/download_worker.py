from __future__ import annotations

from io import BytesIO
import logging
import queue
import re
import sqlite3
import threading
import time
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

_LOG = logging.getLogger(__name__)
_HITOMI_IMAGE_HOSTS = {
    "gold-usergeneratedcontent.net",
    "ltn.gold-usergeneratedcontent.net",
    "a.gold-usergeneratedcontent.net",
    "a1.gold-usergeneratedcontent.net",
    "a2.gold-usergeneratedcontent.net",
    "w1.gold-usergeneratedcontent.net",
    "w2.gold-usergeneratedcontent.net",
    "1.gold-usergeneratedcontent.net",
    "2.gold-usergeneratedcontent.net",
    "atn.gold-usergeneratedcontent.net",
    "btn.gold-usergeneratedcontent.net",
    "tn.hitomi.la",
}


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
            try:
                if task is None:
                    return
                with self.app.app_context():
                    self._run_task(task)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("download task crashed: %s", task.get("job_id") if isinstance(task, dict) else "")
                with self.app.app_context():
                    self._mark_task_failed(task, exc)
            finally:
                self._queue.task_done()

    def _run_task(self, task: dict) -> None:
        job_id = task["job_id"]
        now = _utcnow()
        _execute_with_retry(
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
                should_convert = _should_convert_hitomi_avif(
                    rule_id=str(task.get("rule_id", "")).strip(),
                    final_url=final_url,
                    content_type=res.headers.get("Content-Type", ""),
                )
                if should_convert:
                    converted = _convert_avif_bytes_to_webp(res.content)
                    file_path = target / f"{idx:04d}.webp"
                    with file_path.open("wb") as f:
                        f.write(converted)
                    res.close()
                else:
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

            _execute_with_retry(
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

        _execute_with_retry(
            """
            UPDATE download_jobs
            SET status=?, error_message=?, updated_at=?
            WHERE job_id=?
            """,
            (final_status, merged_errors, _utcnow(), job_id),
        )

    def _mark_task_failed(self, task: dict | None, exc: Exception) -> None:
        if not isinstance(task, dict):
            return

        job_id = str(task.get("job_id", "")).strip()
        if not job_id:
            return

        message = f"任务异常中止: {exc.__class__.__name__}: {exc}"
        _execute_with_retry(
            """
            UPDATE download_jobs
            SET status='failed',
                error_message=CASE
                    WHEN error_message IS NULL OR error_message = ''
                    THEN ?
                    ELSE error_message || '\n' || ?
                END,
                updated_at=?
            WHERE job_id=? AND status IN ('queued', 'running')
            """,
            (message, message, _utcnow(), job_id),
        )

    def _recover_interrupted_jobs(self) -> None:
        now = _utcnow()
        _execute_with_retry(
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


def _hitomi_candidate_urls(url: str) -> list[str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host not in _HITOMI_IMAGE_HOSTS:
        return []

    candidates: list[str] = []

    def _push(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _push(url)

    alt_hosts: list[str] = []
    if host.startswith("atn."):
        alt_hosts.append("btn." + host[4:])
    elif host.startswith("btn."):
        alt_hosts.append("atn." + host[4:])
    elif host.startswith("a1."):
        alt_hosts.append("a2." + host[3:])
    elif host.startswith("a2."):
        alt_hosts.append("a1." + host[3:])
    elif host.startswith("w1."):
        alt_hosts.append("w2." + host[3:])
    elif host.startswith("w2."):
        alt_hosts.append("w1." + host[3:])
    elif host.startswith("1."):
        alt_hosts.append("2." + host[2:])
    elif host.startswith("2."):
        alt_hosts.append("1." + host[2:])

    for alt_host in alt_hosts:
        netloc = alt_host if parsed.port is None else f"{alt_host}:{parsed.port}"
        _push(parsed._replace(netloc=netloc).geturl())

    m = re.match(r"^/([^/]+/\d+/[0-9a-f]{64})\.(avif|webp)$", path, re.IGNORECASE)
    if m:
        stem = m.group(1)
        ext = m.group(2).lower()
        if ext == "avif":
            _push(parsed._replace(path=f"/{stem}.webp").geturl())
        else:
            _push(parsed._replace(path=f"/{stem}.avif").geturl())
        for image_ext in ("jpg", "jpeg", "png", "gif"):
            _push(parsed._replace(path=f"/images/{stem}.{image_ext}").geturl())

    m = re.match(r"^/(images/)?([^/]+)/(\d+)/([0-9a-f]{64})\.(\w+)$", path, re.IGNORECASE)
    if m:
        prefix = m.group(1) or ""
        b_value = m.group(2)
        current_seg = m.group(3)
        hash_value = m.group(4).lower()
        ext = m.group(5)

        segment_values = {
            str(int(hash_value[-3:], 16)),
            str(int(hash_value[-1] + hash_value[-3:-1], 16)),
            str(int(hash_value[-2:], 16)),
        }
        segment_values.discard(current_seg)
        for seg in segment_values:
            _push(parsed._replace(path=f"/{prefix}{b_value}/{seg}/{hash_value}.{ext}").geturl())

    return candidates


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

    candidates.extend(_hitomi_candidate_urls(normalized))
    candidates.extend(_hitomi_candidate_urls(raw))

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


def _should_convert_hitomi_avif(rule_id: str, final_url: str, content_type: str) -> bool:
    if rule_id != "hitomi-chinese":
        return False
    ext = guess_ext(final_url, default="")
    if ext == ".avif":
        return True
    return "image/avif" in str(content_type or "").lower()


def _convert_avif_bytes_to_webp(data: bytes) -> bytes:
    try:
        from PIL import Image
        import pillow_avif  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "AVIF 转 WebP 需要依赖 Pillow 和 pillow-avif-plugin，请先安装 requirements.txt 依赖"
        ) from exc

    with Image.open(BytesIO(data)) as img:
        output = BytesIO()
        converted = img.convert("RGB") if img.mode in {"P", "RGBA", "LA"} else img
        converted.save(output, format="WEBP", quality=92, method=6)
        return output.getvalue()


def _execute_with_retry(sql: str, params: tuple | list = (), attempts: int = 6) -> None:
    for idx in range(attempts):
        try:
            execute(sql, params)
            return
        except sqlite3.OperationalError as exc:
            detail = str(exc).lower()
            if "locked" not in detail and "busy" not in detail:
                raise
            if idx >= attempts - 1:
                raise
            time.sleep(0.15 * (idx + 1))
