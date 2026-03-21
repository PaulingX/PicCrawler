from __future__ import annotations

import hashlib
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler


class Crawler4KHD(BaseCrawler):
    base_url = "https://www.4khd.com/"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        page_no = max(1, int(page_no))
        _ = query
        page_url = self.base_url if page_no == 1 else urljoin(self.base_url, f"page/{page_no}/")
        res = self.session.get(page_url, timeout=20)
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        topics: list[dict] = []
        seen: set[str] = set()

        containers = soup.select("article, .post, .entry, .item, li")
        for node in containers:
            anchor = (
                node.select_one("h1 a, h2 a, h3 a, .entry-title a")
                or node.select_one("a[href]")
            )
            if not anchor:
                continue

            detail_url = urljoin(self.base_url, anchor.get("href", "").strip())
            if not self._is_valid_topic_url(detail_url):
                continue

            title = anchor.get_text(" ", strip=True)
            if not title:
                continue

            if detail_url in seen:
                continue
            seen.add(detail_url)

            cover_url = ""
            image = node.select_one("img")
            if image:
                cover_url = self._extract_image_url(image, detail_url)

            topic_id = hashlib.md5(detail_url.encode("utf-8")).hexdigest()
            topics.append(
                {
                    "topic_id": topic_id,
                    "title": title,
                    "cover_url": cover_url,
                    "detail_url": detail_url,
                }
            )

            if len(topics) >= 80:
                break

        return topics

    def topic_images(self, detail_url: str) -> list[str]:
        detail_url = urljoin(self.base_url, detail_url)
        res = self.session.get(detail_url, timeout=20)
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        image_urls: list[str] = []
        seen: set[str] = set()

        selectors = [
            ".entry-content img",
            ".post-content img",
            ".single-content img",
            ".article-content img",
            "main img",
            "article img",
            "img",
        ]

        for selector in selectors:
            for image in soup.select(selector):
                full = self._extract_image_url(image, detail_url)
                if not full:
                    continue
                lower = full.lower()
                if any(key in lower for key in ["avatar", "logo", "icon", "emoji"]):
                    continue
                if full in seen:
                    continue
                seen.add(full)
                image_urls.append(full)

            if image_urls:
                break

        return image_urls

    def _extract_image_url(self, image, base_url: str) -> str:
        candidates: list[str] = []
        for attr in ["data-src", "data-original", "data-lazy-src", "src"]:
            v = (image.get(attr) or "").strip()
            if v:
                candidates.append(v)

        for attr in ["data-srcset", "srcset"]:
            srcset = (image.get(attr) or "").strip()
            if srcset:
                for part in srcset.split(","):
                    u = part.strip().split(" ")[0].strip()
                    if u:
                        candidates.append(u)

        for candidate in candidates:
            full = urljoin(base_url, candidate)
            if self._is_valid_image_url(full):
                return full
        return ""

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
        if any(token in lower for token in ["blank.gif", "spacer.", "placeholder"]):
            return False
        return True

    def _is_valid_topic_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.netloc.endswith("4khd.com"):
            return False

        path = parsed.path.rstrip("/")
        if not path:
            return False
        if path.startswith("/page"):
            return False
        if any(path.startswith(prefix) for prefix in ["/tag", "/category", "/author", "/wp-"]):
            return False

        return True


