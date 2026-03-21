from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler

_IMAGE_PATTERN = re.compile(
    r"https?:\/\/[^\"'\s]+?\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\"'\s]*)?",
    re.IGNORECASE,
)
_ESCAPED_URL_PATTERN = re.compile(
    r"(?:https?:\/\/|\/\/)[^\"'\s]+?\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\"'\s]*)?",
    re.IGNORECASE,
)
_GALLERY_ID_PATTERN = re.compile(r"/g/(\d+)/?$", re.IGNORECASE)
_GALLERY_PAGE_PATTERN = re.compile(r"/gallery/(\d+)/(\d+)/?$", re.IGNORECASE)
_THUMB_SUFFIX_PATTERN = re.compile(
    r"/(\d+)t\.(jpg|jpeg|png|webp|gif|avif)$",
    re.IGNORECASE,
)
_FULL_SEQ_PATTERN = re.compile(r"^(.*?/)(\d+)\.(jpg|jpeg|png|webp|gif|avif)$", re.IGNORECASE)


class CrawlerAsmhentai(BaseCrawler):
    base_url = "https://asmhentai.com/language/chinese/"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        proxy = str(os.getenv("PICCRAWLER_PROXY", "")).strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        page_no = max(1, int(page_no))
        _ = query
        page_url = self.base_url if page_no == 1 else urljoin(self.base_url, f"?page={page_no}")

        res = self.session.get(page_url, timeout=25)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        topics: list[dict] = []
        seen: set[str] = set()

        # Primary parser: asmhentai card layout.
        for card in soup.select("div.preview_item"):
            detail_anchor = (
                card.select_one(".image a[href*='/g/']")
                or card.select_one(".cpt a[href*='/g/']")
                or card.select_one("a[href*='/g/']")
            )
            if not detail_anchor:
                continue

            href = (detail_anchor.get("href") or "").strip()
            if not href:
                continue

            detail_url = urljoin(self.base_url, href)
            if not self._is_valid_topic_url(detail_url):
                continue
            if detail_url in seen:
                continue
            seen.add(detail_url)

            title_node = card.select_one(".cpt h2.caption, .cpt a[href*='/g/'], h2.caption")
            image = card.select_one(".image img") or card.select_one("img")
            title = (
                (title_node.get_text(" ", strip=True) if title_node else "")
                or (detail_anchor.get("title") or "").strip()
                or (image.get("alt") if image else "")
                or detail_anchor.get_text(" ", strip=True)
            )
            if not title:
                title = detail_url.rstrip("/").split("/")[-1]

            cover_url = ""
            if image:
                cover_url = self._extract_image_from_tag(image, detail_url)

            topics.append(
                {
                    "topic_id": hashlib.md5(detail_url.encode("utf-8")).hexdigest(),
                    "title": title,
                    "cover_url": cover_url,
                    "detail_url": detail_url,
                }
            )
            if len(topics) >= 120:
                break

        if topics:
            return topics

        # Fallback parser for template changes.
        for anchor in soup.select("a[href*='/g/']"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            detail_url = urljoin(self.base_url, href)
            if not self._is_valid_topic_url(detail_url):
                continue
            if detail_url in seen:
                continue
            seen.add(detail_url)

            image = anchor.select_one("img") or anchor.find_previous("img") or anchor.find_next("img")
            title = (
                (anchor.get("title") or "").strip()
                or (image.get("alt") if image else "")
                or anchor.get_text(" ", strip=True)
            )
            if not title:
                title = detail_url.rstrip("/").split("/")[-1]

            cover_url = ""
            if image:
                cover_url = self._extract_image_from_tag(image, detail_url)

            topics.append(
                {
                    "topic_id": hashlib.md5(detail_url.encode("utf-8")).hexdigest(),
                    "title": title,
                    "cover_url": cover_url,
                    "detail_url": detail_url,
                }
            )

            if len(topics) >= 120:
                break

        return topics

    def topic_image_count(self, detail_url: str) -> int:
        detail_url = urljoin(self.base_url, detail_url)
        soup, raw = self._fetch_detail(detail_url)

        total = self._extract_total_pages(soup, raw)
        if total > 0:
            return total

        preview_links = soup.select("#append_thumbs .preview_thumb a[href], .preview_thumb a[href]")
        return len(preview_links)

    def topic_images_page(self, detail_url: str, offset: int = 0, limit: int = 20) -> dict:
        detail_url = urljoin(self.base_url, detail_url)
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))

        soup, raw = self._fetch_detail(detail_url)

        generated = self._generate_images_from_detail(detail_url, soup, raw)
        if generated:
            total = len(generated)
            end = min(total, offset + limit)
            return {
                "items": generated[offset:end],
                "offset": offset,
                "limit": limit,
                "total": total,
                "next_offset": end,
                "has_more": end < total,
            }

        # Fallback to legacy full parse when sequence inference fails.
        all_images = self._topic_images_fallback(detail_url=detail_url, soup=soup, raw_html=raw)
        total = len(all_images)
        end = min(total, offset + limit)
        return {
            "items": all_images[offset:end],
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": end,
            "has_more": end < total,
        }

    def topic_images(self, detail_url: str) -> list[str]:
        detail_url = urljoin(self.base_url, detail_url)
        soup, raw = self._fetch_detail(detail_url)

        generated = self._generate_images_from_detail(detail_url, soup, raw)
        if generated:
            return generated

        return self._topic_images_fallback(detail_url=detail_url, soup=soup, raw_html=raw)

    def _fetch_detail(self, detail_url: str) -> tuple[BeautifulSoup, str]:
        res = self.session.get(detail_url, timeout=25)
        res.raise_for_status()
        raw = res.text or ""
        soup = BeautifulSoup(raw, "html.parser")
        return soup, raw

    def _extract_total_pages(self, soup: BeautifulSoup, raw_html: str) -> int:
        value = (soup.select_one("#t_pages") or {}).get("value") if soup.select_one("#t_pages") else ""
        if value:
            try:
                total = int(str(value).strip())
                if total > 0:
                    return total
            except ValueError:
                pass

        text = soup.get_text(" ", strip=True) or raw_html
        match = re.search(r"Pages\s*:\s*(\d+)", text, re.IGNORECASE)
        if match:
            try:
                total = int(match.group(1))
                if total > 0:
                    return total
            except ValueError:
                pass

        return 0

    def _generate_images_from_detail(self, detail_url: str, soup: BeautifulSoup, raw_html: str) -> list[str]:
        total = self._extract_total_pages(soup, raw_html)
        if total <= 0:
            return []

        template = ""
        for img in soup.select("#append_thumbs .preview_thumb img, .preview_thumb img"):
            thumb = self._extract_image_from_tag(img, detail_url)
            full = self._to_full_image_from_thumb(thumb)
            if self._is_valid_image_url(full):
                template = full
                break

        if not template:
            return []

        parsed = urlparse(template)
        match = _FULL_SEQ_PATTERN.search(parsed.path or "")
        if not match:
            return []

        prefix, _, ext = match.group(1), match.group(2), match.group(3)

        images: list[str] = []
        for index in range(1, total + 1):
            path = f"{prefix}{index}.{ext}"
            url = urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))
            if self._is_valid_image_url(url):
                images.append(url)

        return images

    def _topic_images_fallback(self, detail_url: str, soup: BeautifulSoup, raw_html: str) -> list[str]:
        gallery_id = self._extract_gallery_id(detail_url)

        images: list[str] = []
        seen: set[str] = set()

        def _push(url: str) -> None:
            if not url:
                return
            fixed = url.replace("\\/", "/").strip()
            if fixed.startswith("//"):
                fixed = "https:" + fixed
            if not self._is_valid_image_url(fixed):
                return
            if self._is_thumbnail_or_cover_url(fixed):
                return
            if fixed in seen:
                return
            seen.add(fixed)
            images.append(fixed)

        for img in soup.select("#append_thumbs .preview_thumb img"):
            thumb_url = self._extract_image_from_tag(img, detail_url)
            _push(self._to_full_image_from_thumb(thumb_url))

        gallery_pages: list[str] = []
        for anchor in soup.select("#append_thumbs .preview_thumb a[href], a[href*='/gallery/']"):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue
            page_url = urljoin(detail_url, href)
            if not self._is_valid_gallery_page_url(page_url, gallery_id):
                continue
            if page_url in gallery_pages:
                continue
            gallery_pages.append(page_url)

        for page_url in gallery_pages:
            try:
                page_res = self.session.get(page_url, timeout=25)
                page_res.raise_for_status()
            except Exception:  # noqa: BLE001
                continue

            page_soup = BeautifulSoup(page_res.text, "html.parser")
            page_img = page_soup.select_one("img#fimg, #fimg, .image img, img[data-src]")
            if page_img:
                _push(self._extract_image_from_tag(page_img, page_url))

            for match in _IMAGE_PATTERN.findall(page_res.text):
                _push(match)

        for script in soup.select("script"):
            text = script.get_text(" ", strip=False)
            if not text:
                continue

            for match in _IMAGE_PATTERN.findall(text):
                fixed = match.replace("\\/", "/").encode("utf-8").decode("unicode_escape")
                _push(fixed)

            for match in _ESCAPED_URL_PATTERN.findall(text):
                fixed = match.replace("\\/", "/")
                if fixed.startswith("//"):
                    fixed = "https:" + fixed
                fixed = fixed.encode("utf-8").decode("unicode_escape")
                _push(fixed)

        if not images:
            for img in soup.select("img"):
                image_url = self._extract_image_from_tag(img, detail_url)
                _push(self._to_full_image_from_thumb(image_url))
                _push(image_url)

        return images

    def _extract_image_from_tag(self, tag, base_url: str) -> str:
        attrs = ["data-src", "data-original", "data-lazy-src", "src"]
        candidates: list[str] = []

        for attr in attrs:
            v = (tag.get(attr) or "").strip()
            if v:
                candidates.append(v)

        for attr in ["srcset", "data-srcset"]:
            srcset = (tag.get(attr) or "").strip()
            if srcset:
                for part in srcset.split(","):
                    url_piece = part.strip().split(" ")[0].strip()
                    if url_piece:
                        candidates.append(url_piece)

        for c in candidates:
            full = urljoin(base_url, c)
            if self._is_valid_image_url(full):
                return full
        return ""

    def _is_valid_topic_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.netloc.endswith("asmhentai.com"):
            return False
        path = parsed.path or ""
        if not path.startswith("/g/"):
            return False
        return True

    def _is_valid_image_url(self, url: str) -> bool:
        lower = url.lower()
        if lower.startswith("data:") or lower.startswith("javascript:"):
            return False
        if "base64," in lower:
            return False

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False

        if any(token in lower for token in ["avatar", "logo", "icon", "emoji", "blank.", "load_error"]):
            return False

        return any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"])

    def _extract_gallery_id(self, detail_url: str) -> str:
        match = _GALLERY_ID_PATTERN.search(urlparse(detail_url).path or "")
        if not match:
            return ""
        return match.group(1)

    def _is_valid_gallery_page_url(self, url: str, expected_gallery_id: str) -> bool:
        parsed = urlparse(url)
        if not parsed.netloc.endswith("asmhentai.com"):
            return False

        match = _GALLERY_PAGE_PATTERN.search(parsed.path or "")
        if not match:
            return False

        if expected_gallery_id and match.group(1) != expected_gallery_id:
            return False

        return True

    def _to_full_image_from_thumb(self, url: str) -> str:
        if not url:
            return ""

        parsed = urlparse(url)
        match = _THUMB_SUFFIX_PATTERN.search(parsed.path or "")
        if not match:
            return url

        full_path = _THUMB_SUFFIX_PATTERN.sub(r"/\1.\2", parsed.path)
        return urlunparse((parsed.scheme, parsed.netloc, full_path, parsed.params, parsed.query, parsed.fragment))

    def _is_thumbnail_or_cover_url(self, url: str) -> bool:
        lower = url.lower()
        if "/thumb." in lower:
            return True
        if "/cover." in lower:
            return True
        if re.search(r"/\d+t\.(jpg|jpeg|png|webp|gif|avif)(?:\?|$)", lower):
            return True
        return False
