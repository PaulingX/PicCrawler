from __future__ import annotations

import hashlib
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.services.crawler_wnacg import CrawlerWnacg


class CrawlerManxiangge(CrawlerWnacg):
    # Keep display value consistent with user expectation.
    base_url = "https://漫香阁.com/"

    # Use entry and known mirrors; crawler will probe these and discover
    # real active domains from "official address publish" pages automatically.
    base_candidates = (
        "https://xn--t4b-mxgmxgcom-yp8ve33bkpevz1kpxq.mxgmh.com/",
        "https://漫香阁.com/",
        "https://成漫.com/",
        "https://www.漫香阁.com/",
        "https://xn--qexm24f3mc.com/",
        "https://www.xn--qexm24f3mc.com/",
        "https://xn--vnur50a.com/",
        "https://www.xn--vnur50a.com/",
        # Navigation/return domains from official publish image.
        "https://c4.n66e.com/",
        "https://tfone3.com/",
        "https://tfone4.com/",
        "https://tfone5.com/",
        "https://com.f69e.com/",
        "https://f3.f44e.com/",
        "https://爱国敬业.com/",
        "https://文明和谐.com/",
        "https://xn--vhq89szqmtnm.com/",
        "https://xn--0trz80bulaq96j.com/",
        # Keep compatible mirrors as fallback for detail/image fetch stability.
        "https://www.wnacg.com/",
        "https://wnacg.com/",
        "http://xn--wgv69rba1382b.com/",
        "http://www.xn--wgv69rba1382b.com/",
    )

    # User required categories:
    #   2: 单行本
    #   4: 同人志
    category_ids = (2, 4)
    supports_search = True
    site_label = "manxiangge"

    def _get_base_url(self) -> str:
        if self._resolved_base_url:
            return self._resolved_base_url

        candidates: list[str] = []

        def push(value: str) -> None:
            url = str(value or "").strip()
            if not url:
                return
            if not url.endswith("/"):
                url = f"{url}/"
            if url not in candidates:
                candidates.append(url)

        for item in self.base_candidates:
            push(item)
        push(self.base_url)

        # Resolve potential redirect domain from entry points.
        for entry in list(candidates):
            try:
                res = self.session.get(entry, timeout=6, allow_redirects=True)
                final = str(res.url or "").strip()
                if final:
                    parsed = urlparse(final)
                    if parsed.scheme and parsed.netloc:
                        push(f"{parsed.scheme}://{parsed.netloc}/")
                for discovered in self._extract_discovered_base_urls(res.text or "", final or entry):
                    push(discovered)
            except Exception:  # noqa: BLE001
                continue

        probe_category = int(self.category_ids[0]) if self.category_ids else 2
        for candidate in candidates:
            for probe_url in self._build_category_page_candidates(candidate, probe_category, 1):
                html = self._fetch_page_html(
                    probe_url,
                    referer=candidate,
                    timeout=4.0,
                    max_attempts=2,
                )
                if html:
                    self._resolved_base_url = candidate
                    return candidate

        self._resolved_base_url = candidates[0]
        return self._resolved_base_url

    def _build_category_url(self, category_id: int, page_no: int) -> str:
        base = self._get_base_url()
        return self._build_category_page_candidates(base, category_id, page_no)[0]

    def _extract_discovered_base_urls(self, html: str, current_url: str) -> list[str]:
        if not html:
            return []

        current_host = (urlparse(current_url).hostname or "").lower()
        discovered: list[str] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue

            text = anchor.get_text(" ", strip=True)
            if ("访问" not in text) and ("visit" not in text.lower()):
                continue

            full = urljoin(current_url, href)
            parsed = urlparse(full)
            if parsed.scheme not in {"http", "https"}:
                continue
            if not parsed.netloc:
                continue

            host = (parsed.hostname or "").lower()
            if not host or host == current_host:
                continue

            candidate = f"{parsed.scheme}://{parsed.netloc}/"
            if candidate in seen:
                continue
            seen.add(candidate)
            discovered.append(candidate)

            if len(discovered) >= 20:
                break

        return discovered

    def _build_category_page_candidates(self, base: str, category_id: int, page_no: int) -> list[str]:
        base = str(base or "").strip()
        if not base.endswith("/"):
            base = f"{base}/"

        page_no = max(1, int(page_no))
        category_id = int(category_id)

        urls: list[str] = []

        def push(raw: str) -> None:
            u = urljoin(base, raw)
            if u not in urls:
                urls.append(u)

        if page_no <= 1:
            push(f"list-{category_id}.html")
            return urls

        push(f"list-{category_id}-page-{page_no}.html")
        push(f"list-{category_id}-{page_no}.html")
        push(f"list-{category_id}.html?page={page_no}")
        push(f"list-{category_id}.html?p={page_no}")
        return urls

    def _list_topics_from_categories(self, page_no: int, keyword: str, category_id: int | None = None) -> list[dict]:
        topics: list[dict] = []
        seen_detail_urls: set[str] = set()
        keyword_lower = keyword.lower().strip()
        fetched_any = False
        base = self._get_base_url()

        if category_id is not None and category_id in self.category_ids:
            category_list = [category_id]
        else:
            category_list = list(self.category_ids)

        for cat_id in category_list:
            page_urls = self._build_category_page_candidates(base, cat_id, page_no)
            html = ""
            for page_url in page_urls:
                html = self._fetch_page_html(
                    page_url,
                    referer=urljoin(base, f"list-{cat_id}.html"),
                    timeout=8.0,
                    max_attempts=6,
                )
                if html:
                    break

            if not html:
                continue

            fetched_any = True
            parsed = self._parse_topics(html, page_urls[0])
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

    def _build_search_urls(self, keyword: str, page_no: int) -> list[str]:
        base = self._get_base_url()
        q = quote_plus(keyword)

        if page_no <= 1:
            return [
                f"{base}search.php?key={q}",
                f"{base}search/?q={q}",
            ]

        return [
            f"{base}search.php?key={q}&page={page_no}",
            f"{base}search.php?key={q}&p={page_no}",
            f"{base}search.php?key={q}",
            f"{base}search/?q={q}&page={page_no}",
            f"{base}search/?q={q}&p={page_no}",
            f"{base}search/?q={q}",
        ]

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

        scheme = parsed_page.scheme or "https"
        netloc = parsed_page.netloc
        path = (parsed_page.path or "").lower()
        if netloc:
            push_ref(f"{scheme}://{netloc}/")
            if any(token in path for token in ["-aid-", "-view-id-"]):
                # Gallery/detail pages are more sensitive to referer; keep short and targeted.
                push_ref(f"{scheme}://{netloc}/albums-index-cate-10.html")
            else:
                for cat in self.category_ids:
                    push_ref(f"{scheme}://{netloc}/list-{int(cat)}.html")

        variants: list[dict[str, str]] = [{}]
        for ref in referers:
            parsed_ref = urlparse(ref)
            headers: dict[str, str] = {"Referer": ref}
            if parsed_ref.scheme and parsed_ref.netloc:
                headers["Origin"] = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
            variants.append(headers)
        return variants

    def _candidate_page_urls(self, page_url: str) -> list[str]:
        base_candidates = super()._candidate_page_urls(page_url)
        parsed = urlparse(page_url)
        path = (parsed.path or "").lower()
        is_gallery = any(token in path for token in ["-aid-", "-view-id-"])

        if not is_gallery:
            return base_candidates

        preferred_hosts: list[str] = []

        def push_host(host: str) -> None:
            h = (host or "").strip().lower()
            if h and h not in preferred_hosts:
                preferred_hosts.append(h)

        push_host(parsed.netloc)
        push_host("www.wnacg.com")
        push_host("wnacg.com")

        ordered: list[str] = []
        seen: set[str] = set()

        def push_url(raw: str) -> None:
            u = str(raw or "").strip()
            if not u or u in seen:
                return
            seen.add(u)
            ordered.append(u)

        for host in preferred_hosts:
            candidate = urlunparse(("https", host, parsed.path, parsed.params, parsed.query, parsed.fragment))
            if candidate in base_candidates:
                push_url(candidate)

        for host in preferred_hosts:
            candidate = urlunparse(("http", host, parsed.path, parsed.params, parsed.query, parsed.fragment))
            if candidate in base_candidates:
                push_url(candidate)

        for item in base_candidates:
            push_url(item)

        return ordered[:14]

    def _fetch_page_html(
        self,
        page_url: str,
        referer: str = "",
        timeout: float = 12.0,
        max_attempts: int = 8,
    ) -> str:
        parsed = urlparse(page_url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        is_gallery = any(token in path for token in ["-aid-", "-view-id-", "-index-page-"])
        is_mxg_host = any(token in host for token in ["mxgmh.com", "xn--", "wnacg.com"])

        effective_attempts = int(max_attempts)
        effective_timeout = float(timeout)
        if is_gallery and is_mxg_host:
            effective_attempts = max(effective_attempts, 16)
            effective_timeout = min(effective_timeout, 3.5)

        return super()._fetch_page_html(
            page_url=page_url,
            referer=referer,
            timeout=effective_timeout,
            max_attempts=effective_attempts,
        )

    def _is_topic_url(self, url: str) -> bool:
        # Primary compatibility with wnacg-like paths.
        if super()._is_topic_url(url):
            return True

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False

        path = (parsed.path or "").lower()
        return path.endswith(".html") and any(token in path for token in ["/detail-", "/book-", "/album-"])

    def _parse_topics(self, html: str, base_url: str) -> list[dict]:
        # First use parent parser for wnacg-compatible markup.
        topics = super()._parse_topics(html, base_url)
        if topics:
            return topics

        # Fallback parser for mirror template variants.
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        items: list[dict] = []

        selectors = [
            "a[href*='detail-'][href$='.html']",
            "a[href*='book-'][href$='.html']",
            "a[href*='album-'][href$='.html']",
            "a[href$='.html']",
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
            items.append(
                {
                    "topic_id": hashlib.md5(detail_url.encode("utf-8")).hexdigest(),
                    "title": title,
                    "cover_url": cover_url,
                    "detail_url": detail_url,
                }
            )
            if len(items) >= 120:
                break

        return items
