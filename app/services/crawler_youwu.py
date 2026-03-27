from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler


class CrawlerYouwu(BaseCrawler):
    base_url = "https://youwu.im/"

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
            soup = self._fetch_soup(page_url)
            if soup is None:
                continue

            topics = self._parse_topics_from_soup(soup=soup)
            if topics:
                return topics

        return []

    def topic_images(self, detail_url: str) -> list[str]:
        detail_url = urljoin(self.base_url, detail_url)
        first_soup = self._fetch_soup(detail_url)
        if first_soup is None:
            return []

        page_urls = self._collect_album_page_urls(detail_url=detail_url, first_soup=first_soup)
        images: list[str] = []
        seen: set[str] = set()

        for page_url in page_urls:
            if page_url == detail_url:
                soup = first_soup
            else:
                soup = self._fetch_soup(page_url)
                if soup is None:
                    continue

            for image_url in self._extract_album_images(soup=soup, base_url=page_url):
                if image_url in seen:
                    continue
                seen.add(image_url)
                images.append(image_url)

        return images

    def topic_image_count(self, detail_url: str) -> int:
        detail_url = urljoin(self.base_url, detail_url)
        first_soup = self._fetch_soup(detail_url)
        if first_soup is None:
            return 0

        first_images = self._extract_album_images(soup=first_soup, base_url=detail_url)
        first_count = len(first_images)
        if first_count <= 0:
            return 0

        page_numbers = self._extract_page_numbers(first_soup, detail_url=detail_url)
        last_page = max(page_numbers) if page_numbers else 1
        if last_page <= 1:
            return first_count

        last_url = self._build_page_url(detail_url=detail_url, page_no=last_page)
        last_soup = self._fetch_soup(last_url)
        if last_soup is None:
            return first_count

        last_count = len(self._extract_album_images(soup=last_soup, base_url=last_url))
        if last_count <= 0:
            last_count = first_count

        return (last_page - 1) * first_count + last_count

    def _build_list_urls(self, page_no: int, keyword: str) -> list[str]:
        if keyword:
            encoded = quote(keyword, safe="")
            search_url = urljoin(self.base_url, f"search/{encoded}")
            if page_no <= 1:
                return [search_url]
            return [
                f"{search_url}?page={page_no}",
                search_url,
            ]

        if page_no <= 1:
            return [self.base_url]
        return [urljoin(self.base_url, f"?page={page_no}")]

    def _fetch_soup(self, url: str) -> BeautifulSoup | None:
        try:
            res = self.session.get(url, timeout=25)
            res.raise_for_status()
        except Exception:  # noqa: BLE001
            return None
        return BeautifulSoup(res.text or "", "html.parser")

    def _parse_topics_from_soup(self, soup: BeautifulSoup) -> list[dict]:
        topics: list[dict] = []
        seen: set[str] = set()

        for anchor in soup.select("#main .relative > a[href*='/albums/']"):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue

            detail_url = urljoin(self.base_url, href)
            if not self._is_valid_topic_url(detail_url):
                continue
            if detail_url in seen:
                continue
            seen.add(detail_url)

            image = anchor.select_one("img")
            title = ""
            if image is not None:
                title = str(image.get("alt") or "").strip()

            card = anchor.find_parent("div")
            if not title and card is not None:
                title_node = card.select_one("h2")
                if title_node is not None:
                    title = title_node.get_text(" ", strip=True)

            if not title:
                title = detail_url.rstrip("/").split("/")[-1]

            cover_url = self._extract_image_url(image, detail_url) if image is not None else ""
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

    def _collect_album_page_urls(self, detail_url: str, first_soup: BeautifulSoup) -> list[str]:
        page_numbers = sorted(self._extract_page_numbers(first_soup, detail_url=detail_url))
        urls: list[str] = []
        for page_no in page_numbers:
            urls.append(self._build_page_url(detail_url=detail_url, page_no=page_no))
        return urls

    def _extract_page_numbers(self, soup: BeautifulSoup, detail_url: str) -> set[int]:
        parsed_detail = urlparse(detail_url)
        detail_path = parsed_detail.path.rstrip("/")
        page_numbers: set[int] = {1}

        for anchor in soup.select("nav[aria-label*='Pagination'] a[href], nav[role='navigation'] a[href]"):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue

            full = urljoin(detail_url, href)
            parsed = urlparse(full)
            if parsed.netloc != parsed_detail.netloc:
                continue
            if parsed.path.rstrip("/") != detail_path:
                continue

            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            value = str(params.get("page", "")).strip()
            if not value.isdigit():
                continue

            page_no = int(value)
            if page_no > 1:
                page_numbers.add(page_no)

        return page_numbers

    def _build_page_url(self, detail_url: str, page_no: int) -> str:
        parsed = urlparse(detail_url)
        if page_no <= 1:
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))

        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params["page"] = str(page_no)
        query = urlencode(params)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))

    def _extract_album_images(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()

        nodes = soup.select("#main img.block.my-1")
        if not nodes:
            nodes = soup.select("#main img")

        for image in nodes:
            url = self._extract_image_url(image, base_url)
            if not self._is_valid_topic_image_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            images.append(url)

        return images

    def _extract_image_url(self, image, base_url: str) -> str:
        if image is None:
            return ""

        candidates: list[str] = []
        for attr in ["data-src", "data-original", "data-lazy-src", "src"]:
            value = str(image.get(attr) or "").strip()
            if value:
                candidates.append(value)

        onerror = str(image.get("onerror") or "").strip()
        if onerror:
            for match in re.findall(r"this\.src=['\"]([^'\"]+)['\"]", onerror):
                value = str(match or "").strip()
                if value:
                    candidates.append(value)

        for candidate in candidates:
            full = urljoin(base_url, candidate)
            if self._is_valid_image_url(full):
                return full

        return ""

    def _is_valid_topic_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host != "youwu.im":
            return False
        path = parsed.path or ""
        return path.startswith("/albums/")

    def _is_valid_topic_image_url(self, url: str) -> bool:
        if not self._is_valid_image_url(url):
            return False

        lower = url.lower()
        if "sstatic1.histats.com" in lower:
            return False
        if any(token in lower for token in ["logo", "avatar", "icon", "favicon"]):
            return False

        parsed = urlparse(lower)
        path = parsed.path or ""
        return bool(re.search(r"\.(jpg|jpeg|png|webp|gif|avif)$", path))

    def _is_valid_image_url(self, url: str) -> bool:
        lower = str(url or "").lower()
        if not lower:
            return False
        if lower.startswith("data:") or lower.startswith("javascript:"):
            return False
        if "base64," in lower:
            return False

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False
        return True
