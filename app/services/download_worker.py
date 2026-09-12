from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from io import BytesIO
import atexit
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from flask import Flask

from app.config import USER_AGENT
from app.database import execute
from app.services.hitomi_urls import hitomi_candidate_urls
from app.services.library_scanner import upsert_downloaded_topic
from app.services.utils import guess_ext, header_safe_url, sanitize_name, utcnow_str

_LOG = logging.getLogger(__name__)
class DownloadWorker:
    def __init__(self, app: Flask, max_workers: int | None = None) -> None:
        self.app = app
        if max_workers is None:
            max_workers = int(os.environ.get("PICCRAWLER_DOWNLOAD_WORKERS") or os.environ.get("CONCURRENCY") or "4")
        self.max_workers = max(int(max_workers), 1)
        # 单主题内部并发下载的图片数（对远端限流敏感，默认保守 3）。
        self.image_workers = max(1, int(os.environ.get("PICCRAWLER_IMAGE_WORKERS") or "3"))
        # Recover interrupted tasks from previous process to avoid
        # leaving stale queued/running items in UI forever.
        with self.app.app_context():
            self._recover_interrupted_jobs()
        # 线程池并发：多个主题的下载任务可同时进行；主题内部再按
        # image_workers 并发拉取图片，文件名带序号，与完成顺序无关。
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="piccrawler-dl",
        )
        self._registry_lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        atexit.register(self.shutdown)

    def submit(
        self,
        rule_id: str,
        topic_id: str,
        title: str,
        detail_url: str,
        target_dir: str,
        image_urls: list[str],
    ) -> str:
        now = utcnow_str()
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
        cancel_event = threading.Event()
        future = self._executor.submit(
            self._run_task_safe,
            {
                "job_id": job_id,
                "rule_id": rule_id,
                "title": title,
                "target_dir": target_dir,
                "detail_url": detail_url,
                "image_urls": image_urls,
                "cancel_event": cancel_event,
            },
        )
        with self._registry_lock:
            self._futures[job_id] = future
            self._cancel_events[job_id] = cancel_event
        return job_id

    def cancel(self, job_id: str) -> bool:
        """取消任务：排队中的直接出队，运行中的在下张图片前停止。

        返回 False 表示任务已不在执行登记中（已完成或已被清理）。
        """
        with self._registry_lock:
            future = self._futures.get(job_id)
            event = self._cancel_events.get(job_id)
        if future is None:
            return False
        if future.cancel():
            self._forget(job_id)
            with self.app.app_context():
                self._mark_canceled(job_id)
            return True
        if event is not None:
            event.set()
            return True
        return False

    def _forget(self, job_id: str) -> None:
        with self._registry_lock:
            self._futures.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    def _run_task_safe(self, task: dict) -> None:
        try:
            with self.app.app_context():
                self._run_task(task)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("download task crashed: %s", task.get("job_id") if isinstance(task, dict) else "")
            try:
                with self.app.app_context():
                    self._mark_task_failed(task, exc)
            except Exception:  # noqa: BLE001
                pass
        finally:
            if isinstance(task, dict):
                self._forget(str(task.get("job_id", "")))

    def shutdown(self) -> None:
        """进程退出时释放线程池（放弃在途任务，下次启动会回收为失败）。"""
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass

    def _run_task(self, task: dict) -> None:
        job_id = task["job_id"]
        cancel_event: threading.Event = task["cancel_event"]
        now = utcnow_str()
        _execute_with_retry(
            "UPDATE download_jobs SET status='running', updated_at=? WHERE job_id=?",
            (now, job_id),
        )

        target = Path(task["target_dir"]).resolve() / sanitize_name(task["title"])
        target.mkdir(parents=True, exist_ok=True)

        image_urls: list[str] = task["image_urls"]
        detail_url = str(task.get("detail_url", "")).strip()
        rule_id = str(task.get("rule_id", "")).strip()
        session = requests.Session()

        # idx -> 错误信息（空串为成功）；并发写统一走 state_lock。
        results: dict[int, str] = {}
        state_lock = threading.Lock()

        def _flush_progress() -> None:
            with state_lock:
                ok = sum(1 for err in results.values() if not err)
            _execute_with_retry(
                "UPDATE download_jobs SET downloaded_images=?, updated_at=? WHERE job_id=?",
                (ok, utcnow_str(), job_id),
            )

        def _attempt(idx: int, url: str) -> str:
            """下载单张图片并落盘，返回错误信息（空串为成功）。"""
            try:
                res, final_url = _download_image_with_fallbacks(
                    session=session,
                    image_url=url,
                    referer=detail_url,
                )
                should_convert = _should_convert_hitomi_avif(
                    rule_id=rule_id,
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
                return ""
            except Exception as exc:  # noqa: BLE001
                return f"{idx}:{exc}"

        def _run_batch(items: list[tuple[int, str]]) -> None:
            """并发执行一批图片下载；取消时停止调度尚未开始的图片。"""
            if not items:
                return
            workers = min(self.image_workers, len(items))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="piccrawler-img") as pool:
                futures = {pool.submit(_attempt, idx, url): idx for idx, url in items}
                try:
                    for future in as_completed(futures):
                        idx = futures[future]
                        with state_lock:
                            results[idx] = future.result()
                        _flush_progress()
                        if cancel_event.is_set():
                            break
                finally:
                    for future in futures:
                        future.cancel()

        pending = list(enumerate(image_urls, start=1))
        _run_batch(pending)

        # 失败的图片统一重试一轮：远端偶发 5xx/超时常能恢复，提高完成率。
        if not cancel_event.is_set():
            failed = [(idx, url) for idx, url in pending if results.get(idx)]
            if failed:
                _run_batch(failed)

        with state_lock:
            downloaded = sum(1 for err in results.values() if not err)
            errors = [results[idx] for idx in sorted(results) if results[idx]]

        if downloaded == len(image_urls):
            final_status = "done"
        elif cancel_event.is_set():
            final_status = "canceled"
        elif downloaded > 0:
            final_status = "partial"
        else:
            final_status = "failed"

        sync_error = ""
        if downloaded > 0:
            try:
                upsert_downloaded_topic(
                    rule_id=rule_id,
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
            SET status=?, downloaded_images=?, error_message=?, updated_at=?
            WHERE job_id=?
            """,
            (final_status, downloaded, merged_errors, utcnow_str(), job_id),
        )

    def _mark_canceled(self, job_id: str) -> None:
        _execute_with_retry(
            """
            UPDATE download_jobs
            SET status='canceled',
                error_message=CASE
                    WHEN error_message IS NULL OR error_message = ''
                    THEN '任务已取消'
                    ELSE error_message
                END,
                updated_at=?
            WHERE job_id=? AND status IN ('queued', 'running')
            """,
            (utcnow_str(), job_id),
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
            (message, message, utcnow_str(), job_id),
        )

    def _recover_interrupted_jobs(self) -> None:
        now = utcnow_str()
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
            # Other origins: keep the Photon wrapper. imgbox direct links now
            # serve a placeholder image; wp.com still holds the real bytes.
            return url

    if host == "pic.4khd.com":
        return _rewrite_pic_to_img(parsed.path, parsed.query)

    # imgbox direct hotlinks serve a placeholder; download via wp.com wrapper.
    if re.match(r"^(?:images|thumbs)\d*\.imgbox\.com$", host):
        return urlunparse(("https", "i1.wp.com", f"/{host}{parsed.path or '/'}", "", parsed.query, ""))

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

    candidates.extend(hitomi_candidate_urls(normalized))
    candidates.extend(hitomi_candidate_urls(raw))

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
    # hitomi 中文站详情页含非 ASCII 字符，Referer 必须头部安全化；
    # 且该 CDN 校验 Referer，缺 Referer 时返回 404，所以不能简单丢弃。
    safe_referer = header_safe_url(referer) if referer_ok else ""

    for candidate in _candidate_download_urls(image_url):
        plans = [
            {
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                **({"Referer": safe_referer} if safe_referer else {}),
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
