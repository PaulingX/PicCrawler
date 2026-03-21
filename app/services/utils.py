from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


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
