from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler


class Crawler4KHD(BaseCrawler):
    base_url = "https://www.4khd.com/"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        proxy = str(os.getenv("PICCRAWLER_PROXY", "")).strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        page_no = max(1, int(page_no))
        keyword = str(query or "").strip()

        for page_url in self._build_list_urls(page_no=page_no, keyword=keyword):
            try:
                res = self.session.get(page_url, timeout=20)
                res.raise_for_status()
            except Exception:  # noqa: BLE001
                continue

            topics = self._parse_topics_from_html(res.text or "")
            if topics:
                return topics

        return []

    def _build_list_urls(self, page_no: int, keyword: str) -> list[str]:
        if keyword:
            encoded = quote(keyword, safe="")
            if page_no <= 1:
                return [urljoin(self.base_url, f"search/{encoded}")]
            return [
                urljoin(self.base_url, f"search/{encoded}/page/{page_no}/"),
                urljoin(self.base_url, f"search/{encoded}"),
            ]

        if page_no <= 1:
            return [self.base_url]
        return [urljoin(self.base_url, f"page/{page_no}/")]

    def _parse_topics_from_html(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        topics: list[dict] = []
        seen: set[str] = set()

        containers = soup.select("article, .post, .entry, .item, li")
        for node in containers:
            anchor = node.select_one("h1 a, h2 a, h3 a, .entry-title a") or node.select_one("a[href]")
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
        first_html = res.text or ""
        first_soup = BeautifulSoup(first_html, "html.parser")

        page_urls = self._collect_topic_page_urls(detail_url=detail_url, first_soup=first_soup)
        image_urls: list[str] = []
        seen: set[str] = set()

        for page_url in page_urls:
            if page_url == detail_url:
                soup = first_soup
            else:
                try:
                    page_res = self.session.get(page_url, timeout=20)
                    page_res.raise_for_status()
                except Exception:  # noqa: BLE001
                    continue
                soup = BeautifulSoup(page_res.text or "", "html.parser")

            for image_url in self._extract_topic_images_from_soup(soup=soup, base_url=page_url):
                if image_url in seen:
                    continue
                seen.add(image_url)
                image_urls.append(image_url)

        return image_urls

    def topic_image_count(self, detail_url: str) -> int:
        detail_url = urljoin(self.base_url, detail_url)
        try:
            res = self.session.get(detail_url, timeout=20)
            res.raise_for_status()
        except Exception:  # noqa: BLE001
            return 0

        html = res.text or ""
        soup = BeautifulSoup(html, "html.parser")

        text_candidates = [
            (soup.title.get_text(" ", strip=True) if soup.title else ""),
            str(soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else ""),
            html[:5000],
        ]
        for text in text_candidates:
            if not text:
                continue
            match = re.search(r"(\d+)\s*photos?", text, re.IGNORECASE)
            if match:
                try:
                    total = int(match.group(1))
                    if total > 0:
                        return total
                except ValueError:
                    pass

        # fallback: expensive but accurate
        return len(self.topic_images(detail_url))

    def _collect_topic_page_urls(self, detail_url: str, first_soup: BeautifulSoup) -> list[str]:
        base = detail_url.rstrip("/")
        if not base:
            return [detail_url]

        page_numbers: set[int] = {1}
        parsed = urlparse(base)
        base_path = parsed.path.rstrip("/")
        base_path_slash = f"{base_path}/"

        for anchor in first_soup.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue

            full = urljoin(detail_url, href).split("#", 1)[0]
            parsed_full = urlparse(full)
            if parsed_full.netloc != parsed.netloc:
                continue

            path = parsed_full.path.rstrip("/")
            if not path.startswith(base_path_slash):
                continue

            suffix = path[len(base_path_slash) :]
            if not suffix.isdigit():
                continue

            page_no = int(suffix)
            if page_no > 1:
                page_numbers.add(page_no)

        return [base] + [f"{base}/{page_no}" for page_no in sorted(page_numbers) if page_no > 1]

    def _extract_topic_images_from_soup(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        selectors = [
            ".entry-content img",
            ".post-content img",
            ".single-content img",
            ".article-content img",
            "article img",
            "main img",
            "img",
        ]

        images: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            nodes = soup.select(selector)
            if not nodes:
                continue
            for image in nodes:
                full = self._extract_image_url(image, base_url)
                if not full:
                    continue
                if not self._is_valid_topic_image_url(full):
                    continue
                if full in seen:
                    continue
                seen.add(full)
                images.append(full)
            if images:
                break
        return images

    def _is_valid_topic_image_url(self, url: str) -> bool:
        if not self._is_valid_image_url(url):
            return False

        lower = url.lower()
        if any(key in lower for key in ["avatar", "logo", "icon", "emoji"]):
            return False
        if "4khd-beautifulgirls.webp" in lower:
            return False
        if "yt4.googleusercontent.com/-" not in lower and "4khd.com-" not in lower:
            # Avoid unrelated site template images.
            return False

        parsed = urlparse(lower)
        path = parsed.path or ""
        return bool(re.search(r"\.(jpg|jpeg|png|webp|gif|avif)$", path))

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
        if any(path.startswith(prefix) for prefix in ["/tag", "/category", "/author", "/search", "/wp-"]):
            return False

        return path.startswith("/content/")


