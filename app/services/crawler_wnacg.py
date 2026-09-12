from __future__ import annotations

import hashlib
import os
import re
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler

try:
    import cloudscraper
except Exception:  # noqa: BLE001
    cloudscraper = None

_IMAGE_PATTERN = re.compile(
    r"(?:https?:)?\/\/[^\"'\s]+?\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\"'\s]*)?",
    re.IGNORECASE,
)
_VIEW_PAGE_PATTERN = re.compile(r"\/(?:photos|photo)-view-id-\d+\.html", re.IGNORECASE)
_TOPIC_INDEX_PATTERN = re.compile(
    r"\/(?:photos|photo)-index(?:-page-(\d+))?-aid-(\d+)\.html",
    re.IGNORECASE,
)
_HOST_THUMB_PATTERN = re.compile(r"^t(\d+)\.", re.IGNORECASE)
_EMBEDDED_HOST_PATH_PATTERN = re.compile(r"^//([a-z0-9.-]+\.[a-z]{2,})(/.*)$", re.IGNORECASE)
_NUMERIC_IMAGE_PATH_PATTERN = re.compile(
    r"^(.*?/)(\d+)\.(jpg|jpeg|png|webp|gif|avif)$",
    re.IGNORECASE,
)
_ALT_SUFFIX_PATTERN = re.compile(r"_(\d{1,3}(?:_[a-z0-9]{2,})?)$", re.IGNORECASE)

# 进程级 TTL 缓存：Flask 每个请求都会新建爬虫实例，主题数量与分页浏览会
# 反复重抓同一主题的详情页 / 索引页 / 阅读页，请求量过大会触发 WNACG 限流，
# 导致主题内图片整批加载失败。缓存放在类级别（跨实例共享）并带 TTL。
_CACHE_MAX_ENTRIES = 512
_DETAIL_TTL_SECONDS = 600.0
_GALLERY_TTL_SECONDS = 600.0
_VIEW_IMAGE_TTL_SECONDS = 1800.0
_COUNT_TTL_SECONDS = 86400.0

_DETAIL_CACHE: dict[str, tuple[float, str]] = {}
_GALLERY_CACHE: dict[str, tuple[float, list[str], dict[str, str]]] = {}
_VIEW_IMAGE_CACHE: dict[str, tuple[float, str]] = {}
_COUNT_CACHE: dict[str, tuple[float, int]] = {}

# WNACG 限流表现为 403/429/挑战页。检测到限流后进入短暂全局冷却，
# 避免重试链立刻打出更多请求放大封锁。
_RATE_COOLDOWN_SECONDS = 5.0
_RATE_LOCK = threading.Lock()
_RATE_COOLDOWN_UNTIL = 0.0

# 阅读页解析相互独立，用小线程池并行抓取，缩短首个批次的等待时间。
# 并发数保持保守，过高会在限流边缘触发整批失败。
_PAGE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="piccrawler-wnacg")


class CrawlerWnacg(BaseCrawler):
    base_url = "https://www.wnacg.com/"
    base_candidates = (
        "https://www.wnacg.com/",
        "https://wnacg.com/",
    )
    category_ids = (1, 9, 10, 20)
    categories = [1, 9, 10, 20]
    supports_search = True
    site_label = "wnacg"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        proxy = str(os.getenv("PICCRAWLER_PROXY", "")).strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        self._cf_session = self._build_cloudflare_session(proxy=proxy)
        self._cf_warmed = False
        # 阅读页并行解析时多个线程共用 session，cookie 同步与 CF 预热需要互斥。
        self._cookie_lock = threading.Lock()

        cookie_text = str(os.getenv("PICCRAWLER_WNACG_COOKIE", "")).strip()
        if cookie_text:
            self._load_cookie_string(cookie_text)

        self._resolved_base_url: str | None = None

    @staticmethod
    def _cache_get(cache: dict, key: str, ttl: float):
        entry = cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > ttl:
            cache.pop(key, None)
            return None
        return value

    @staticmethod
    def _cache_put(cache: dict, key: str, value) -> None:
        if key not in cache and len(cache) >= _CACHE_MAX_ENTRIES:
            for old_key in list(cache.keys())[:64]:
                cache.pop(old_key, None)
        cache[key] = (time.time(), value)

    @staticmethod
    def _note_rate_limit() -> None:
        global _RATE_COOLDOWN_UNTIL
        with _RATE_LOCK:
            _RATE_COOLDOWN_UNTIL = time.time() + _RATE_COOLDOWN_SECONDS

    @staticmethod
    def _rate_limit_wait() -> float:
        """命中限流冷却时需要等待的秒数（有上限，避免拖死请求线程）。"""
        with _RATE_LOCK:
            remaining = _RATE_COOLDOWN_UNTIL - time.time()
        if remaining <= 0:
            return 0.0
        return min(remaining, 2.0)

    def _get_topic_page_html(self, detail_url: str) -> str:
        cached = self._cache_get(_DETAIL_CACHE, detail_url, _DETAIL_TTL_SECONDS)
        if cached is not None:
            return cached
        html = self._fetch_page_html(detail_url, timeout=10.0, max_attempts=10)
        if html and not self._looks_like_challenge(html, 200):
            self._cache_put(_DETAIL_CACHE, detail_url, html)
        return html

    def list_topics(self, page_no: int, query: str = "", category_id: int | None = None) -> list[dict]:
        page_no = max(1, int(page_no))
        keyword = query.strip()
        selected_category = int(category_id) if category_id is not None else None

        if keyword:
            topics = self._list_topics_from_search(keyword=keyword, page_no=page_no)
            if topics:
                return topics
            if self._is_search_challenged(keyword=keyword, page_no=page_no):
                hint = ""
                if self._cf_session is None:
                    hint = "，请安装 cloudscraper 或设置 PICCRAWLER_WNACG_COOKIE"
                raise RuntimeError(f"WNACG 搜索被 Cloudflare 拦截{hint}")
            # Real no-result search should return empty list; do not fallback
            # to category filtering, which would produce misleading results.
            return []

        return self._list_topics_from_categories(
            page_no=page_no,
            keyword="",
            category_id=selected_category,
        )

    def topic_image_count(self, detail_url: str) -> int:
        detail_url = urljoin(self._get_base_url(), detail_url)
        # 页数是静态属性，长缓存可避免网格每张卡片都打一次详情页请求
        # （一页最多 200+ 卡片，正是触发 WNACG 限流的主要来源）。
        cached_count = self._cache_get(_COUNT_CACHE, detail_url, _COUNT_TTL_SECONDS)
        if cached_count is not None:
            return cached_count

        html = self._get_topic_page_html(detail_url)
        if not html:
            raise RuntimeError("wnacg topic page fetch failed")

        parsed_count = self._extract_declared_image_count(html)
        if parsed_count > 0:
            self._cache_put(_COUNT_CACHE, detail_url, parsed_count)
            return parsed_count

        view_pages, _ = self._collect_gallery_pages(detail_url, html)
        if view_pages:
            self._cache_put(_COUNT_CACHE, detail_url, len(view_pages))
        return len(view_pages)

    def topic_images_page(self, detail_url: str, offset: int = 0, limit: int = 20) -> dict:
        detail_url = urljoin(self._get_base_url(), detail_url)
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))

        html = self._get_topic_page_html(detail_url)
        if not html:
            raise RuntimeError("wnacg topic page fetch failed")

        view_pages, thumb_by_page = self._collect_gallery_pages(detail_url, html)
        declared_total = self._extract_declared_image_count(html)
        total = max(len(view_pages), declared_total)

        if total == 0:
            return {"items": [], "offset": offset, "limit": limit, "total": 0, "has_more": False}

        end = min(total, offset + limit)
        seed_candidates: list[str] = []
        seen_seed: set[str] = set()
        probe_end = min(len(view_pages), 12)
        probe_images: list[str] = []
        if probe_end > 0:
            probe_images = self._extract_images_from_pages(
                detail_url=detail_url,
                view_pages=view_pages,
                thumb_by_page=thumb_by_page,
                start=0,
                end=probe_end,
            )
            for candidate in probe_images:
                value = str(candidate or "").strip()
                if value and value not in seen_seed:
                    seen_seed.add(value)
                    seed_candidates.append(value)

        for page_url in view_pages:
            candidate = str(thumb_by_page.get(page_url, "")).strip()
            if candidate and candidate not in seen_seed:
                seen_seed.add(candidate)
                seed_candidates.append(candidate)
        for candidate in thumb_by_page.values():
            value = str(candidate or "").strip()
            if value and value not in seen_seed:
                seen_seed.add(value)
                seed_candidates.append(value)

        for seed_url in seed_candidates:
            sequential = self._build_sequential_images_from_seed(seed_url=seed_url, total=total)
            if not sequential:
                continue
            if not self._sequence_matches_probe(sequence=sequential, probe_images=probe_images):
                continue
            return {
                "items": sequential[offset:end],
                "offset": offset,
                "limit": limit,
                "total": total,
                "next_offset": end,
                "has_more": end < total,
            }

        images = self._extract_images_from_pages(
            detail_url=detail_url,
            view_pages=view_pages,
            thumb_by_page=thumb_by_page,
            start=offset,
            end=end,
        )

        return {
            "items": images,
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": end,
            "has_more": end < total,
        }

    def topic_images(self, detail_url: str) -> list[str]:
        detail_url = urljoin(self._get_base_url(), detail_url)
        page_data = self.topic_images_page(detail_url=detail_url, offset=0, limit=5000)
        return list(page_data.get("items") or [])

    def _collect_gallery_pages(self, detail_url: str, html: str) -> tuple[list[str], dict[str, str]]:
        cached = self._cache_get(_GALLERY_CACHE, detail_url, _GALLERY_TTL_SECONDS)
        if cached is not None:
            return cached

        result = self._collect_gallery_pages_uncached(detail_url, html)
        if result[0]:
            self._cache_put(_GALLERY_CACHE, detail_url, result)
        return result

    def _collect_gallery_pages_uncached(self, detail_url: str, html: str) -> tuple[list[str], dict[str, str]]:
        view_pages: list[str] = []
        seen_pages: set[str] = set()
        thumb_by_page: dict[str, str] = {}
        detail_aid = self._extract_topic_aid(detail_url)
        declared_total = self._extract_declared_image_count(html)

        visited_index_pages: set[str] = set()
        pending_index_pages: list[str] = [detail_url]
        index_html_cache: dict[str, str] = {detail_url: html}
        max_index_pages = 160

        # WNACG often serves only the first batch of view links in HTML and hides
        # subsequent index pages, so we proactively derive page-2/page-3 URLs.
        if declared_total > 12 and detail_aid:
            expected_pages = max(2, int(math.ceil(declared_total / 12.0)))
            for page_no in range(2, expected_pages + 1):
                index_page_url = self._build_topic_index_page_url(detail_url=detail_url, page_no=page_no)
                if index_page_url and index_page_url not in pending_index_pages:
                    pending_index_pages.append(index_page_url)

        def add_page(raw_href: str) -> str:
            href = str(raw_href or "").strip()
            if not href:
                return ""

            page_url = urljoin(detail_url, href)
            if not self._is_view_page_url(page_url):
                return ""

            if page_url not in seen_pages:
                seen_pages.add(page_url)
                view_pages.append(page_url)
            return page_url

        while pending_index_pages and len(visited_index_pages) < max_index_pages:
            index_url = pending_index_pages.pop(0)
            if index_url in visited_index_pages:
                continue

            visited_index_pages.add(index_url)
            page_html = index_html_cache.get(index_url) or self._fetch_page_html(
                index_url,
                referer=detail_url,
                timeout=8.0,
                max_attempts=6,
            )
            if not page_html:
                continue

            soup = BeautifulSoup(page_html, "html.parser")

            for anchor in soup.select("div.pic_box a[href], a[href*='photos-view-id-'], a[href*='photo-view-id-']"):
                page_url = add_page(anchor.get("href") or "")
                if not page_url:
                    continue

                img = anchor.select_one("img") or anchor.find_next("img")
                thumb_url = self._extract_image_url(img, index_url) if img else ""
                alt_text = (img.get("alt") or "").strip() if img else ""
                full_guess = self._build_image_from_thumb_alt(thumb_url=thumb_url, alt_text=alt_text)
                if not full_guess:
                    full_guess = self._to_full_image_url(thumb_url)
                if self._is_gallery_image_url(full_guess):
                    thumb_by_page[page_url] = full_guess

            for match in _VIEW_PAGE_PATTERN.findall(page_html):
                add_page(match)

            index_candidates: list[str] = []
            for anchor in soup.select("a[href*='-index-aid-'], a[href*='-index-page-']"):
                index_candidates.append(anchor.get("href") or "")
            for match in _TOPIC_INDEX_PATTERN.finditer(page_html):
                index_candidates.append(match.group(0))

            for raw_href in index_candidates:
                next_index_url = urljoin(index_url, str(raw_href or "").strip())
                if not self._is_topic_index_url(next_index_url, detail_aid):
                    continue
                if next_index_url in visited_index_pages or next_index_url in pending_index_pages:
                    continue
                pending_index_pages.append(next_index_url)

        return view_pages, thumb_by_page

    def _resolve_view_page_image(self, page_url: str, detail_url: str) -> str:
        """解析单个阅读页的大图地址；结果带 TTL 缓存，空结果也缓存避免反复重试。"""
        cached = self._cache_get(_VIEW_IMAGE_CACHE, page_url, _VIEW_IMAGE_TTL_SECONDS)
        if cached is not None:
            return cached

        chosen = ""
        page_html = self._fetch_page_html(page_url, referer=detail_url, timeout=6.0, max_attempts=4)

        if page_html and not self._looks_like_challenge(page_html, 200):
            page_soup = BeautifulSoup(page_html, "html.parser")
            for selector in [
                "#photo_body img#picarea",
                "#photo_body img.photo",
                "img#picarea",
                "#picarea img",
                ".photo_body img.photo",
                ".photo img",
                "#posselect img",
            ]:
                page_img = page_soup.select_one(selector)
                if not page_img:
                    continue
                candidate = self._extract_image_url(page_img, page_url)
                if self._is_gallery_image_url(candidate) and not self._is_thumbnail_url(candidate):
                    chosen = candidate
                    break

            if not chosen:
                for match in _IMAGE_PATTERN.findall(page_html):
                    candidate = self._normalize_image_url(match)
                    if not self._is_gallery_image_url(candidate):
                        continue
                    if self._is_thumbnail_url(candidate):
                        continue
                    chosen = candidate
                    break

            if not chosen:
                # 页面正常但没有大图时才做二次直取；限流页不值得再打 8 次请求。
                chosen = self._fetch_view_image_direct(page_url, referer=detail_url)

        self._cache_put(_VIEW_IMAGE_CACHE, page_url, chosen or "")
        return chosen

    def _extract_images_from_pages(
        self,
        detail_url: str,
        view_pages: list[str],
        thumb_by_page: dict[str, str],
        start: int,
        end: int,
    ) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()

        def push(url: str) -> None:
            normalized = self._normalize_image_url(url)
            if not self._is_gallery_image_url(normalized):
                return

            if self._is_thumbnail_url(normalized):
                normalized = self._normalize_image_url(self._to_full_image_url(normalized))

            if not self._is_gallery_image_url(normalized):
                return
            if self._is_thumbnail_url(normalized):
                return
            if normalized in seen:
                return

            seen.add(normalized)
            images.append(normalized)

        upper = min(len(view_pages), max(start, end))
        indexes = range(max(0, start), upper)

        def resolve(index: int) -> tuple[int, str]:
            page_url = view_pages[index]
            chosen = self._resolve_view_page_image(page_url, detail_url)
            if not chosen:
                fallback = thumb_by_page.get(page_url, "")
                if not self._is_unstable_qy0_url(fallback):
                    chosen = fallback
            return index, chosen

        if len(indexes) > 1:
            futures = [_PAGE_EXECUTOR.submit(resolve, idx) for idx in indexes]
            for future in futures:
                _, chosen = future.result()
                push(chosen)
        else:
            for idx in indexes:
                _, chosen = resolve(idx)
                push(chosen)

        return images

    def _list_topics_from_search(self, keyword: str, page_no: int) -> list[dict]:
        topics: list[dict] = []

        for page_url in self._build_search_urls(keyword, page_no):
            html = self._fetch_page_html(page_url, timeout=8.0, max_attempts=6)
            if not html:
                continue

            parsed = self._parse_topics(html, page_url)
            if parsed:
                topics = parsed
                break

        return topics

    def _is_search_challenged(self, keyword: str, page_no: int) -> bool:
        urls = self._build_search_urls(keyword, page_no)
        for page_url in urls[:2]:
            status_code, text = self._request_page(
                page_url=page_url,
                timeout=8.0,
                headers=None,
            )
            if self._looks_like_challenge(text, status_code=status_code):
                return True
            if status_code == 200 and text.strip():
                return False
        return False

    def _list_topics_from_categories(self, page_no: int, keyword: str, category_id: int | None = None) -> list[dict]:
        topics: list[dict] = []
        seen_detail_urls: set[str] = set()
        keyword_lower = keyword.lower().strip()
        fetched_any = False

        if category_id is not None and category_id in self.category_ids:
            category_list = [category_id]
        else:
            category_list = list(self.category_ids)

        for cat_id in category_list:
            page_url = self._build_category_url(category_id=cat_id, page_no=page_no)
            html = self._fetch_page_html(page_url, timeout=8.0, max_attempts=6)
            if not html:
                continue

            fetched_any = True
            parsed = self._parse_topics(html, page_url)
            for item in parsed:
                detail_url = str(item.get("detail_url", ""))
                if not detail_url or detail_url in seen_detail_urls:
                    continue
                if keyword_lower and keyword_lower not in str(item.get("title", "")).lower():
                    continue
                seen_detail_urls.add(detail_url)
                topics.append(item)

                if len(topics) >= 220:
                    return topics

        if not fetched_any:
            raise RuntimeError(f"{self.site_label} category pages are unreachable or blocked")

        return topics

    def _build_category_url(self, category_id: int, page_no: int) -> str:
        base = self._get_base_url()
        if page_no <= 1:
            return urljoin(base, f"albums-index-page-1-cate-{category_id}.html")
        return urljoin(base, f"albums-index-page-{page_no}-cate-{category_id}.html")

    def _build_search_urls(self, keyword: str, page_no: int) -> list[str]:
        base = self._get_base_url()
        q = quote_plus(keyword)
        common = f"q={q}&m=&syn=yes&f=_all&s=create_time_DESC"
        if page_no <= 1:
            return [
                urljoin(base, f"search/index.php?{common}&p=1"),
                urljoin(base, f"search/index.php?{common}"),
            ]
        return [
            urljoin(base, f"search/index.php?{common}&p={page_no}"),
        ]

    def _parse_topics(self, html: str, base_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")

        topics: list[dict] = []
        seen: set[str] = set()

        selectors = [
            "a[href*='photos-index-aid-']",
            "a[href*='photo-index-aid-']",
            "a[href*='-aid-'][href$='.html']",
        ]

        candidates = []
        for selector in selectors:
            candidates.extend(soup.select(selector))

        for anchor in candidates:
            href = (anchor.get("href") or "").strip()
            if not href:
                continue

            detail_url = urljoin(base_url, href)
            if not self._is_topic_url(detail_url):
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

            cover_url = self._extract_image_url(image, detail_url) if image else ""

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

        candidates: list[str] = []
        for attr in ["data-src", "data-original", "data-lazy-src", "src"]:
            value = (image.get(attr) or "").strip()
            if value:
                candidates.append(value)

        for attr in ["data-srcset", "srcset"]:
            srcset = (image.get(attr) or "").strip()
            if srcset:
                for part in srcset.split(","):
                    url_part = part.strip().split(" ")[0].strip()
                    if url_part:
                        candidates.append(url_part)

        for candidate in candidates:
            normalized_candidate = self._normalize_image_url(candidate)
            if not normalized_candidate:
                continue

            if normalized_candidate.startswith("http://") or normalized_candidate.startswith("https://"):
                full = normalized_candidate
            else:
                full = urljoin(base_url, normalized_candidate)

            normalized = self._normalize_image_url(full)
            if self._is_valid_image_url(normalized):
                return normalized

        return ""

    def _get_base_url(self) -> str:
        if self._resolved_base_url:
            return self._resolved_base_url

        candidates = list(self.base_candidates) if self.base_candidates else [self.base_url]
        probe_category = int(self.category_ids[0]) if self.category_ids else 1

        for candidate in candidates:
            probe_url = urljoin(candidate, f"albums-index-page-1-cate-{probe_category}.html")
            html = self._fetch_page_html(probe_url, timeout=8.0, max_attempts=6)
            if html:
                self._resolved_base_url = candidate
                return candidate

        self._resolved_base_url = candidates[0]
        return self._resolved_base_url

    def _fetch_page_html(
        self,
        page_url: str,
        referer: str = "",
        timeout: float = 12.0,
        max_attempts: int = 8,
    ) -> str:
        last_text = ""
        candidates = self._candidate_page_urls(page_url)
        header_variants = self._build_request_header_variants(page_url=page_url, referer=referer)
        max_attempts = max(1, int(max_attempts))
        attempt_count = 0

        for idx, candidate in enumerate(candidates):
            header_variants = self._build_request_header_variants(page_url=candidate, referer=referer)
            for header_idx, headers in enumerate(header_variants):
                attempt_count += 1
                if attempt_count > max_attempts:
                    break
                cooldown_wait = self._rate_limit_wait()
                if cooldown_wait > 0:
                    time.sleep(cooldown_wait)
                if idx > 0 or header_idx > 0:
                    time.sleep(0.08)
                status_code, text = self._request_page(
                    page_url=candidate,
                    timeout=timeout,
                    headers=headers,
                )
                if self._looks_like_challenge(text, status_code=status_code):
                    self._note_rate_limit()
                    last_text = text
                    continue
                if text:
                    last_text = text
                if status_code >= 500:
                    continue
                if text:
                    return text
            if attempt_count > max_attempts:
                break

        return last_text

    def _build_cloudflare_session(self, proxy: str) -> requests.Session | None:
        if cloudscraper is None:
            return None
        try:
            session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False},
            )
        except Exception:  # noqa: BLE001
            return None

        session.headers.update(self.session.headers)
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    def _load_cookie_string(self, cookie_text: str) -> None:
        jar = SimpleCookie()
        try:
            jar.load(cookie_text)
        except Exception:  # noqa: BLE001
            return

        for key, morsel in jar.items():
            value = str(morsel.value or "").strip()
            if not value:
                continue

            domain = str(morsel["domain"] or "").strip()
            path = str(morsel["path"] or "").strip() or "/"
            if not domain:
                domain = ".wnacg.com"

            self.session.cookies.set(key, value, domain=domain, path=path)
            if self._cf_session is not None:
                self._cf_session.cookies.set(key, value, domain=domain, path=path)

    def _request_page(self, page_url: str, timeout: float, headers: dict[str, str] | None) -> tuple[int, str]:
        clients: list[requests.Session] = [self.session]
        if self._cf_session is not None:
            clients.append(self._cf_session)

        last_status = 0
        last_text = ""
        for client in clients:
            if client is self._cf_session:
                self._warmup_cloudflare_session(timeout=max(4.0, min(timeout, 10.0)))
            try:
                res = client.get(page_url, timeout=timeout, headers=headers or None)
            except Exception:  # noqa: BLE001
                continue

            status_code = int(res.status_code or 0)
            text = res.text or ""
            if text:
                last_text = text
            last_status = status_code
            self._sync_cookies_from(client)

            if self._looks_like_challenge(text, status_code=status_code):
                continue
            if status_code >= 500:
                continue
            return status_code, text

        return last_status, last_text

    def _sync_cookies_from(self, source_session: requests.Session) -> None:
        with self._cookie_lock:
            if source_session is self.session:
                if self._cf_session is not None:
                    self._cf_session.cookies.update(self.session.cookies)
                return

            if source_session is self._cf_session:
                self.session.cookies.update(self._cf_session.cookies)

    def _warmup_cloudflare_session(self, timeout: float) -> None:
        with self._cookie_lock:
            warmed = self._cf_warmed
        if self._cf_session is None or warmed:
            return

        probe_urls = [
            self.base_url,
            urljoin(self.base_url, "albums-index-cate-1.html"),
        ]
        for probe_url in probe_urls:
            try:
                res = self._cf_session.get(probe_url, timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            self._sync_cookies_from(self._cf_session)
            status_code = int(res.status_code or 0)
            text = res.text or ""
            if status_code == 200 and text.strip() and not self._looks_like_challenge(text, status_code):
                with self._cookie_lock:
                    self._cf_warmed = True
                break

    def _build_request_header_variants(self, page_url: str, referer: str = "") -> list[dict[str, str]]:
        parsed_page = urlparse(page_url)
        referers: list[str] = []

        def push_ref(value: str) -> None:
            v = str(value or "").strip()
            if not v:
                return
            if v not in referers:
                referers.append(v)

        push_ref(referer)

        host = (parsed_page.hostname or "").lower()
        path = (parsed_page.path or "").lower()
        scheme = parsed_page.scheme or "https"
        netloc = parsed_page.netloc
        if host.endswith("wnacg.com") and netloc:
            if any(token in path for token in ["-index-aid-", "-index-page-", "-view-id-", "/search/"]):
                for cat_id in self.category_ids:
                    push_ref(f"{scheme}://{netloc}/albums-index-cate-{int(cat_id)}.html")
            if "-view-id-" in path:
                push_ref(f"{scheme}://{netloc}/")

        variants: list[dict[str, str]] = [{}]
        for ref in referers:
            parsed_ref = urlparse(ref)
            headers: dict[str, str] = {"Referer": ref}
            if parsed_ref.scheme and parsed_ref.netloc:
                headers["Origin"] = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
            variants.append(headers)
        return variants

    def _candidate_page_urls(self, page_url: str) -> list[str]:
        parsed = urlparse(page_url)
        if not parsed.netloc:
            return [page_url]

        candidate_hosts: list[str] = []

        def push_host(host: str) -> None:
            value = (host or "").strip().lower()
            if not value:
                return
            if value not in candidate_hosts:
                candidate_hosts.append(value)

        push_host(parsed.netloc)

        if self._resolved_base_url:
            push_host(urlparse(self._resolved_base_url).netloc)

        for candidate in self.base_candidates:
            push_host(urlparse(candidate).netloc)

        host_no_www = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
        if host_no_www.endswith("wnacg.com"):
            push_host(host_no_www)
            push_host(f"www.{host_no_www}")

        schemes = [parsed.scheme or "https", "https", "http"]

        candidates: list[str] = []
        seen: set[str] = set()
        for scheme in schemes:
            for host in candidate_hosts:
                if not host:
                    continue
                candidate_url = urlunparse(
                    (scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment)
                )
                if candidate_url in seen:
                    continue
                seen.add(candidate_url)
                candidates.append(candidate_url)

        if not candidates:
            return [page_url]
        return candidates

    def _looks_like_challenge(self, html: str, status_code: int) -> bool:
        if status_code in {401, 403, 429, 503}:
            return True

        lower = (html or "").lower()
        challenge_keys = [
            "just a moment",
            "cf-challenge",
            "cf-browser-verification",
            "cloudflare",
            "captcha",
        ]
        hits = sum(1 for key in challenge_keys if key in lower)
        return hits >= 2

    def _is_topic_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False

        path = parsed.path.lower()
        if not path.endswith(".html"):
            return False
        return "-aid-" in path

    def _is_view_page_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False

        path = parsed.path.lower()
        return "photos-view-id-" in path or "photo-view-id-" in path

    def _extract_topic_aid(self, url: str) -> str:
        match = re.search(r"-aid-(\d+)\.html", urlparse(url).path or "", re.IGNORECASE)
        if not match:
            return ""
        return match.group(1)

    def _is_topic_index_url(self, url: str, expected_aid: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False

        match = _TOPIC_INDEX_PATTERN.search(parsed.path or "")
        if not match:
            return False

        aid = match.group(2)
        if expected_aid and aid != expected_aid:
            return False
        return True

    def _build_topic_index_page_url(self, detail_url: str, page_no: int) -> str:
        if page_no <= 1:
            return detail_url

        parsed = urlparse(detail_url)
        path = parsed.path or ""
        match = re.search(
            r"^/(photos|photo)-index(?:-page-\d+)?-aid-(\d+)\.html$",
            path,
            re.IGNORECASE,
        )
        if not match:
            return ""

        kind = match.group(1)
        aid = match.group(2)
        new_path = f"/{kind}-index-page-{page_no}-aid-{aid}.html"
        return urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))

    def _extract_declared_image_count(self, html: str) -> int:
        if not html:
            return 0

        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select("label"):
            text = node.get_text(" ", strip=True)
            match = re.search(r"(?:頁數|页数)\s*[:：]\s*(\d+)\s*[Pp]?", text)
            if match:
                try:
                    value = int(match.group(1))
                except ValueError:
                    continue
                if value > 0:
                    return value

        match = re.search(r"(?:頁數|页数)\s*[:：]\s*(\d+)\s*[Pp]?", html)
        if match:
            try:
                value = int(match.group(1))
            except ValueError:
                return 0
            if value > 0:
                return value
        return 0

    def _build_sequential_images_from_seed(self, seed_url: str, total: int) -> list[str]:
        if total <= 0:
            return []

        normalized_seed = self._normalize_image_url(seed_url)
        if not self._is_gallery_image_url(normalized_seed):
            return []

        parsed = urlparse(normalized_seed)
        path = parsed.path or ""
        match = _NUMERIC_IMAGE_PATH_PATTERN.search(path)
        if not match:
            return []

        prefix, number_text, ext = match.group(1), match.group(2), match.group(3)
        first_number = int(number_text)
        if first_number not in {0, 1}:
            return []

        width = len(number_text)
        query = parsed.query
        fragment = parsed.fragment

        images: list[str] = []
        for index in range(1, total + 1):
            num = str(index).zfill(width) if width > 1 else str(index)
            img_path = f"{prefix}{num}.{ext}"
            url = urlunparse((parsed.scheme, parsed.netloc, img_path, parsed.params, query, fragment))
            normalized = self._normalize_image_url(url)
            if not self._is_gallery_image_url(normalized):
                return []
            images.append(normalized)

        return images

    def _sequence_matches_probe(self, sequence: list[str], probe_images: list[str]) -> bool:
        if not sequence:
            return False
        if not probe_images:
            return True

        compare = min(len(sequence), len(probe_images), 12)
        if compare <= 0:
            return False

        exact = 0
        for idx in range(compare):
            if sequence[idx] == probe_images[idx]:
                exact += 1

        threshold = max(3, int(compare * 0.45))
        return exact >= threshold

    def _normalize_image_url(self, url: str) -> str:
        if not url:
            return ""

        fixed = url.replace("\\/", "/").strip().strip('"\'')

        while fixed.startswith("////"):
            fixed = fixed[2:]
        if fixed.startswith("//"):
            fixed = "https:" + fixed

        parsed = urlparse(fixed)
        if parsed.scheme in {"http", "https"} and parsed.netloc in {"www.wnacg.com", "wnacg.com"}:
            match = _EMBEDDED_HOST_PATH_PATTERN.match(parsed.path or "")
            if match:
                fixed = urlunparse(
                    (parsed.scheme, match.group(1), match.group(2), parsed.params, parsed.query, parsed.fragment)
                )

        return fixed

    def _to_full_image_url(self, url: str) -> str:
        normalized = self._normalize_image_url(url)
        if not normalized:
            return ""

        parsed = urlparse(normalized)
        path = parsed.path
        path = path.replace("/data/t/", "/data/")
        path = path.replace("/thumb/", "/data/")
        path = path.replace("/thumbnail/", "/data/")

        host = parsed.netloc
        match = _HOST_THUMB_PATTERN.search(host)
        if match:
            host = _HOST_THUMB_PATTERN.sub(f"img{match.group(1)}.", host, count=1)

        return urlunparse((parsed.scheme, host, path, parsed.params, parsed.query, parsed.fragment))

    def _build_image_from_thumb_alt(self, thumb_url: str, alt_text: str) -> str:
        normalized = self._normalize_image_url(thumb_url)
        if not normalized or not alt_text:
            return ""

        parsed = urlparse(normalized)
        parts = [p for p in (parsed.path or "").split("/") if p]
        # expected thumbnail path: /data/t/<dir1>/<dir2>/<file>
        if len(parts) < 5 or parts[0] != "data" or parts[1] != "t":
            return ""

        thumb_file = parts[-1]
        if "." not in thumb_file:
            return ""
        ext = thumb_file.rsplit(".", 1)[-1].lower()
        if ext not in {"jpg", "jpeg", "png", "webp", "gif", "avif"}:
            return ""

        alt_match = _ALT_SUFFIX_PATTERN.search(alt_text.strip())
        if not alt_match:
            return ""
        suffix = alt_match.group(1)

        host = parsed.netloc
        thumb_host = (parsed.hostname or "").lower()
        host_match = _HOST_THUMB_PATTERN.search(thumb_host)
        if host_match:
            try:
                host_num = int(host_match.group(1))
            except ValueError:
                host_num = 4
            host = _HOST_THUMB_PATTERN.sub(f"img{host_num + 1}.", parsed.netloc, count=1)
        elif not thumb_host.startswith("img"):
            host = "img5.qy0.ru"

        path = f"/data/{parts[2]}/{parts[3]}/{suffix}.{ext}"
        built = urlunparse(("https", host, path, "", "", ""))
        return built if self._is_gallery_image_url(built) else ""

    def _is_thumbnail_url(self, url: str) -> bool:
        normalized = self._normalize_image_url(url).lower()
        parsed = urlparse(normalized)
        path = parsed.path or ""
        host = parsed.hostname or ""

        if "/data/t/" in path:
            return True
        if "/thumb/" in path or "/thumbnail/" in path or "/preview/" in path:
            return True
        if re.search(r"(^|[-_/])thumb(?:[-_.]|$)", path):
            return True
        if re.match(r"^t\d+\.", host):
            return True
        if host.startswith("t") and "/data/" in path:
            return True
        return False

    def _is_gallery_image_url(self, url: str) -> bool:
        normalized = self._normalize_image_url(url)
        if not self._is_valid_image_url(normalized):
            return False

        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()

        if not host.endswith("qy0.ru"):
            return False
        if "/data/" not in path:
            return False
        return True

    def _is_valid_image_url(self, url: str) -> bool:
        lower = (url or "").lower()
        if lower.startswith("data:") or lower.startswith("javascript:"):
            return False
        if "base64," in lower:
            return False

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False

        if any(token in lower for token in ["avatar", "logo", "icon", "blank.", "load_error", "guanzhupic", "sixinpin"]):
            return False

        return any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"])

    def _fetch_view_image_direct(self, page_url: str, referer: str = "") -> str:
        for attempt in range(2):
            if attempt > 0:
                time.sleep(0.12 * attempt)
            html = self._fetch_page_html(page_url, referer=referer, timeout=6.0, max_attempts=4)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for selector in ["#photo_body img#picarea", "img#picarea", "#picarea img"]:
                img = soup.select_one(selector)
                if not img:
                    continue
                candidate = self._extract_image_url(img, page_url)
                if self._is_gallery_image_url(candidate) and not self._is_thumbnail_url(candidate):
                    return candidate
        return ""

    def _is_unstable_qy0_url(self, url: str) -> bool:
        normalized = self._normalize_image_url(url)
        if not normalized:
            return False
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()
        if not host.endswith("qy0.ru"):
            return False
        filename = (parsed.path or "").split("/")[-1].lower()
        if "_" in filename:
            return False
        match = re.match(r"^(\d+)\.(jpg|jpeg|png|webp|gif|avif)$", filename)
        if not match:
            return False
        return len(match.group(1)) >= 8
