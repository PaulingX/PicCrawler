from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def header_safe_url(value: str) -> str:
    """把 URL 变成可以放进 HTTP 头的值。

    HTTP 头只能是 latin-1，hitomi 中文站的详情页 URL 含非 ASCII 字符
    （如 `...-中文-4184194.html`），直接作为 Referer 会触发
    UnicodeEncodeError，导致图片代理 500 / 下载全部失败。
    只对非 ASCII 字符做百分号编码，其余字符原样保留（避免二次编码）。
    """
    text = str(value or "")
    if not text:
        return ""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        pass

    parts: list[str] = []
    for ch in text:
        try:
            ch.encode("latin-1")
            parts.append(ch)
        except UnicodeEncodeError:
            parts.append(quote(ch, safe=""))
    return "".join(parts)


def utcnow() -> datetime:
    """当前 UTC 时间。

    返回 naive datetime（与数据库中历史 ISO 字符串同基准），
    避免弃用的 datetime.utcnow()，也避免 naive/aware 混算。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_str() -> str:
    """当前 UTC 时间的 ISO 字符串（秒精度），数据库时间戳统一使用。"""
    return utcnow().isoformat(timespec="seconds")


def natural_key(value: str) -> list:
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r"(\d+)", value)]


def sanitize_name(value: str, fallback: str = "untitled") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value).strip(" .")
    return cleaned[:120] or fallback


def guess_ext(url: str, default: str = ".jpg") -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return ext
    return default


def is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def list_direct_images(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    files = [p for p in folder.iterdir() if is_image_file(p)]
    files.sort(key=lambda p: natural_key(p.name))
    return files


def list_recursive_images(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    files = [p for p in folder.rglob("*") if is_image_file(p)]
    files.sort(key=lambda p: natural_key(str(p.relative_to(folder))))
    return files


def find_cover_image(folder: Path) -> Path | None:
    direct = list_direct_images(folder)
    if direct:
        return direct[0]

    subdirs = [p for p in folder.iterdir() if p.is_dir()]
    subdirs.sort(key=lambda p: natural_key(p.name))
    for sub in subdirs:
        sub_images = list_recursive_images(sub)
        if sub_images:
            return sub_images[0]
    return None
