from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import cloudscraper
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler


class CrawlerXChina(BaseCrawler):
    base_url = "https://xchina.co/"

    def __init__(self) -> None:
        self.session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": self.base_url
        })
        proxy = str(os.getenv("PICCRAWLER_PROXY", "")).strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        page_no = max(1, int(page_no))
        keyword = str(query or "").strip()

        for list_url in self._build_list_urls(page_no=page_no, keyword=keyword):
            soup = self._fetch_soup(list_url)
            if soup is None:
                continue

            topics = self._parse_topics_from_soup(soup=soup, base_url=list_url)
            if topics:
                return topics

        return []

    def topic_images(self, detail_url: str) -> list[str]:
        detail_url = urljoin(self.base_url, detail_url)
        soup = self._fetch_soup(detail_url)
        if soup is None:
            return []

        images: list[str] = []
        seen: set[str] = set()

        for image_url in self._extract_topic_images_from_soup(page_soup=soup, detail_url=detail_url):
            if image_url in seen:
                continue
            seen.add(image_url)
            images.append(image_url)

        return images

    def topic_image_count(self, detail_url: str) -> int:
        detail_url = urljoin(self.base_url, detail_url)
        soup = self._fetch_soup(detail_url)
        if soup is None:
            return 0

        # 从页面上解析，比如：<div class="text">12P + 4V</div>
        text_candidates = [
            node.get_text(" ", strip=True)
            for node in soup.select(".info-card.photo-detail, .tab-contents")
        ]
        text_candidates.append(soup.get_text(" ", strip=True)[:12000])
        for text in text_candidates:
            if not text:
                continue
            match = re.search(r"\b(\d+)\s*[Pp]\b", text)
            if match:
                try:
                    count = int(match.group(1))
                    if count > 0:
                        return count
                except ValueError:
                    pass

        return len(self.topic_images(detail_url))

    def _build_list_urls(self, page_no: int, keyword: str) -> list[str]:
        if keyword:
            encoded = quote(keyword, safe="")
            if page_no <= 1:
                return [
                    urljoin(self.base_url, f"photos/keyword-{encoded}.html"),
                ]
            return [
                urljoin(self.base_url, f"photos/keyword-{encoded}/{page_no}.html"),
                urljoin(self.base_url, f"photos/keyword-{encoded}.html"),
            ]

        if page_no <= 1:
            return [urljoin(self.base_url, "photos.html")]
        return [
            urljoin(self.base_url, f"photos/{page_no}.html"),
        ]

    def _fetch_soup(self, url: str) -> BeautifulSoup | None:
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            response.encoding = "utf-8"
        except Exception:  # noqa: BLE001
            return None
        return BeautifulSoup(response.text or "", "html.parser")

    def _extract_topic_images_from_soup(self, page_soup: BeautifulSoup, detail_url: str) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()

        for item in page_soup.select("div.list.photo-items div.item.photo-image"):
            img_div = item.select_one("div.img")
            if img_div is None:
                continue
            
            style = img_div.get("style", "")
            match = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style)
            if not match:
                continue
            
            raw_url = match.group(1)
            full_url = urljoin(detail_url, raw_url)
            
            if "_600x0.webp" in full_url:
                high_res_url = full_url.replace("_600x0.webp", ".jpg")
            else:
                high_res_url = full_url

            if self._is_valid_image_url(high_res_url):
                if high_res_url not in seen:
                    seen.add(high_res_url)
                    images.append(high_res_url)
            elif self._is_valid_image_url(full_url):
                if full_url not in seen:
                    seen.add(full_url)
                    images.append(full_url)

        return images

    def _parse_topics_from_soup(self, soup: BeautifulSoup, base_url: str) -> list[dict]:
        topics: list[dict] = []
        seen: set[str] = set()

        for card in soup.select("div.list.photo-list div.item.photo"):
            anchor = card.select_one("a[href*='/photo/']")
            if anchor is None:
                continue

            href = str(anchor.get("href") or "").strip()
            if not href:
                continue
            detail_url = urljoin(base_url, href)
            if detail_url in seen:
                continue

            title = str(anchor.get("title") or "").strip()
            if not title:
                title_node = card.select_one(".title a")
                if title_node is not None:
                    title = title_node.get_text(" ", strip=True)
            if not title:
                continue

            cover_url = ""
            img_div = card.select_one(".img")
            if img_div is not None:
                style = img_div.get("style", "")
                match = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", style)
                if match:
                    cover_url = urljoin(detail_url, match.group(1))

            seen.add(detail_url)
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

    def _is_valid_image_url(self, url: str) -> bool:
        lower = str(url or "").lower()
        if not lower:
            return False
        if lower.startswith(("data:", "javascript:")):
            return False
        if "base64," in lower:
            return False

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        return bool(parsed.netloc)
