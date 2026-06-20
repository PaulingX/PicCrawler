from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import cloudscraper
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler


class CrawlerHotgirl(BaseCrawler):
    base_url = "https://hotgirl.asia/"
    _BLOCKED_SINGLE_SLUGS = {
        "photos",
        "videos",
        "stars",
        "most-viewed",
        "most-favorites",
        "most-rating",
        "top-imdb",
        "top-week-viewed",
        "top-month-viewed",
        "top-year-viewed",
        "search",
        "feed",
        "page",
    }

    def __init__(self) -> None:
        self.session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.session.headers.update({"User-Agent": USER_AGENT})
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
        detail_url = self._normalize_detail_url(urljoin(self.base_url, detail_url))
        soup = self._fetch_soup(detail_url)
        if soup is None:
            return []

        images: list[str] = []
        seen: set[str] = set()

        page_urls = self._collect_detail_page_urls(detail_url=detail_url, first_soup=soup)
        for page_url in page_urls:
            if page_url == detail_url:
                page_soup = soup
            else:
                page_soup = self._fetch_soup(page_url)
                if page_soup is None:
                    continue

            for image_url in self._extract_topic_images_from_soup(page_soup=page_soup, detail_url=page_url):
                if image_url in seen:
                    continue
                seen.add(image_url)
                images.append(image_url)

        return images

    def topic_image_count(self, detail_url: str) -> int:
        detail_url = self._normalize_detail_url(urljoin(self.base_url, detail_url))
        soup = self._fetch_soup(detail_url)
        if soup is None:
            return 0

        # Prefer exact count by summing all `?num=` pages.
        page_urls = self._collect_detail_page_urls(detail_url=detail_url, first_soup=soup)
        seen: set[str] = set()
        total = 0
        for page_url in page_urls:
            if page_url == detail_url:
                page_soup = soup
            else:
                page_soup = self._fetch_soup(page_url)
                if page_soup is None:
                    continue

            for image_url in self._extract_topic_images_from_soup(page_soup=page_soup, detail_url=page_url):
                if image_url in seen:
                    continue
                seen.add(image_url)
                total += 1
        if total > 0:
            return total

        # Some pages expose explicit text like "Photos: 9" in detail section.
        text_candidates = [
            node.get_text(" ", strip=True)
            for node in soup.select(".mvi-content, #mv-info, .main-content.main-detail")
        ]
        text_candidates.append(soup.get_text(" ", strip=True)[:12000])
        for text in text_candidates:
            if not text:
                continue
            for pattern in [r"\bPhotos?\s*:\s*(\d+)\b", r"\bPic\s*(\d+)\b"]:
                match = re.search(pattern, text, re.IGNORECASE)
                if not match:
                    continue
                try:
                    count = int(match.group(1))
                except ValueError:
                    continue
                if count > 0:
                    return count

        return len(self.topic_images(detail_url))

    def _build_list_urls(self, page_no: int, keyword: str) -> list[str]:
        if keyword:
            encoded = quote(keyword, safe="")
            if page_no <= 1:
                return [
                    urljoin(self.base_url, f"?s={encoded}"),
                    urljoin(self.base_url, f"search/{encoded}"),
                ]
            return [
                urljoin(self.base_url, f"page/{page_no}/?s={encoded}"),
                urljoin(self.base_url, f"?s={encoded}&page={page_no}"),
                urljoin(self.base_url, f"search/{encoded}/page/{page_no}/"),
                urljoin(self.base_url, f"?s={encoded}"),
            ]

        if page_no <= 1:
            return [urljoin(self.base_url, "photos/"), self.base_url]
        return [
            urljoin(self.base_url, f"photos/page/{page_no}/"),
            urljoin(self.base_url, f"page/{page_no}/"),
        ]

    def _fetch_soup(self, url: str) -> BeautifulSoup | None:
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except Exception:  # noqa: BLE001
            return None
        return BeautifulSoup(response.text or "", "html.parser")

    def _normalize_detail_url(self, detail_url: str) -> str:
        parsed = urlparse(detail_url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.pop("num", None)
        query = urlencode(params)
        path = parsed.path or "/"
        if not path.endswith("/"):
            path = f"{path}/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, ""))

    def _collect_detail_page_urls(self, detail_url: str, first_soup: BeautifulSoup) -> list[str]:
        parsed_base = urlparse(detail_url)
        page_numbers: set[int] = {1}

        for anchor in first_soup.select(".pagination a[href], a.page[href], a[href*='?num=']"):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue

            full = urljoin(detail_url, href)
            parsed = urlparse(full)
            if (parsed.hostname or "").lower() != (parsed_base.hostname or "").lower():
                continue
            if parsed.path.rstrip("/") != parsed_base.path.rstrip("/"):
                continue

            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            raw_num = str(params.get("num", "")).strip()
            if not raw_num.isdigit():
                continue

            page_no = int(raw_num)
            if page_no > 1:
                page_numbers.add(page_no)

        return [
            self._build_detail_page_url(detail_url=detail_url, page_no=page_no)
            for page_no in sorted(page_numbers)
        ]

    def _build_detail_page_url(self, detail_url: str, page_no: int) -> str:
        parsed = urlparse(detail_url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page_no <= 1:
            params.pop("num", None)
        else:
            params["num"] = str(page_no)
        query = urlencode(params)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))

    def _extract_topic_images_from_soup(self, page_soup: BeautifulSoup, detail_url: str) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()

        for image in page_soup.select("div.galeria_img img"):
            image_url = self._extract_image_url(image, base_url=detail_url)
            if not self._is_valid_topic_image_url(image_url):
                continue
            if image_url in seen:
                continue
            seen.add(image_url)
            images.append(image_url)

        if images:
            return images

        # Conservative fallback: keep scope inside detail container to avoid
        # accidentally collecting recommendation/list thumbnails.
        for image in page_soup.select(".mvi-content .galeria_img img, #content-embed .galeria_img img"):
            image_url = self._extract_image_url(image, base_url=detail_url)
            if not self._is_valid_topic_image_url(image_url):
                continue
            if image_url in seen:
                continue
            seen.add(image_url)
            images.append(image_url)

        return images

    def _parse_topics_from_soup(self, soup: BeautifulSoup, base_url: str) -> list[dict]:
        topics: list[dict] = []
        seen: set[str] = set()

        for card in soup.select("div.ml-item"):
            anchor = card.select_one("a.ml-mask[href]") or card.select_one("a[href]")
            if anchor is None:
                continue

            href = str(anchor.get("href") or "").strip()
            if not href:
                continue
            detail_url = urljoin(base_url, href)
            if not self._is_valid_topic_url(detail_url):
                continue
            if detail_url in seen:
                continue

            title = str(anchor.get("oldtitle") or "").strip()
            if not title:
                cover_node = card.select_one("img.mli-thumb")
                if cover_node is not None:
                    title = str(cover_node.get("alt") or "").strip()
            if not title:
                title_node = card.select_one(".mli-info h2")
                if title_node is not None:
                    title = title_node.get_text(" ", strip=True)
            if not title:
                continue

            cover_url = ""
            image = card.select_one("img.mli-thumb")
            if image is not None:
                cover_url = self._extract_image_url(image, base_url=detail_url)

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

    def _extract_image_url(self, image, base_url: str) -> str:
        if image is None:
            return ""

        for attr in ["data-src", "data-original", "data-lazy-src", "src"]:
            value = str(image.get(attr) or "").strip()
            if not value:
                continue
            full = urljoin(base_url, value)
            if self._is_valid_image_url(full):
                return full
        return ""

    def _is_valid_topic_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if host not in {"hotgirl.asia", "www.hotgirl.asia"}:
            return False

        path = parsed.path.strip("/")
        if not path:
            return False

        segments = [segment for segment in path.split("/") if segment]
        if len(segments) != 1:
            return False

        slug = segments[0].lower()
        if slug in self._BLOCKED_SINGLE_SLUGS:
            return False
        if slug.startswith(("genre", "tag", "category", "author", "wp-")):
            return False
        return True

    def _is_valid_topic_image_url(self, url: str) -> bool:
        if not self._is_valid_image_url(url):
            return False

        lower = url.lower()
        if "wp-postratings" in lower:
            return False
        if "wp-content/themes/" in lower:
            return False
        if "loading.gif" in lower or "rating_off" in lower:
            return False
        if any(token in lower for token in ["avatar", "logo", "icon", "emoji", "gravatar", "spacer"]):
            return False

        parsed = urlparse(lower)
        path = parsed.path or ""
        return bool(re.search(r"\.(jpg|jpeg|png|webp|gif|avif)$", path))

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
