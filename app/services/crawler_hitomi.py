from __future__ import annotations

import json
import os
import re
import struct
import time
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

from app.config import USER_AGENT
from app.services.crawler_base import BaseCrawler

_GALLERY_ID_PATTERN = re.compile(r"(?:/reader/|/galleries/)(\d+)(?:\.html)?", re.IGNORECASE)
_NOZOMI_INT_SIZE = 4
_DEFAULT_GG_BASE = "1774080001/"
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif"}
_SEARCH_NODE_SIZE = 464
_SEARCH_B = 16


@dataclass
class _QueryPlan:
    language: str
    nozomi_key: str
    positive_terms: list[str]
    negative_terms: list[str]
    tag_filters: list[tuple[str, str]]

    @property
    def needs_filter(self) -> bool:
        return bool(self.positive_terms or self.negative_terms or self.tag_filters)

    @property
    def requires_meta(self) -> bool:
        return bool(self.tag_filters)


@dataclass
class _GgParams:
    base_prefix: str
    zero_cases: set[int]


class CrawlerHitomi(BaseCrawler):
    base_url = "https://hitomi.la/index-chinese.html"
    page_size = 25
    scan_batch = 120
    max_scan_ids = 1600

    asset_domains = (
        "ltn.gold-usergeneratedcontent.net",
        "ltn.hitomi.la",
    )
    search_domain = "ltn.gold-usergeneratedcontent.net"

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

        self._gg_cache: dict[str, Any] = {
            "at": 0.0,
            "params": _GgParams(base_prefix=_DEFAULT_GG_BASE, zero_cases=set()),
        }
        self._search_versions: dict[str, Any] = {"at": 0.0, "data": {}}
        self._search_term_cache: dict[str, list[int]] = {}

    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        page_no = max(1, int(page_no))
        normalized_query = self._normalize_search_query(query)
        if normalized_query:
            ids = self._search_gallery_ids(normalized_query)
            start = (page_no - 1) * self.page_size
            end = start + self.page_size
            page_ids = ids[start:end]
            return self._topics_from_ids(page_ids, include_meta=False)

        plan = self._parse_query(query)
        page_offset = (page_no - 1) * self.page_size

        if not plan.needs_filter:
            ids = self._fetch_nozomi_ids(plan.nozomi_key, start=page_offset, count=self.page_size)
            return self._topics_from_ids(ids, include_meta=False)

        wanted = page_offset + self.page_size
        matched: list[dict] = []
        scan_start = 0

        source_key = plan.nozomi_key
        include_meta = plan.requires_meta
        match_plan = plan

        if plan.tag_filters:
            # Tag query correctness takes priority over speed:
            # always fetch metadata and filter by tags.
            include_meta = True
            probe_ids = self._fetch_nozomi_ids(plan.nozomi_key, start=0, count=1, allow_fallback=False)
            if not probe_ids:
                source_key = f"index-{plan.language}"
        max_scan = 800 if include_meta else self.max_scan_ids

        started = time.monotonic()

        while len(matched) < wanted and scan_start < max_scan:
            if time.monotonic() - started > 25:
                break
            ids = self._fetch_nozomi_ids(
                source_key,
                start=scan_start,
                count=self.scan_batch,
                allow_fallback=True,
            )
            if not ids:
                break

            topics = self._topics_from_ids(ids, include_meta=include_meta)
            for topic in topics:
                if self._topic_matches(topic, match_plan):
                    matched.append(topic)

            scan_start += len(ids)
            if len(ids) < self.scan_batch:
                break

        return matched[page_offset : page_offset + self.page_size]

    def _normalize_search_query(self, query: str) -> str:
        text = str(query or "").strip()
        if not text:
            return ""

        lower = text.lower()
        if "hitomi.la/search.html?" in lower:
            text = text.split("?", 1)[-1].strip()

        # Frontend may pass a decoded query; users may also paste encoded form.
        if "%" in text:
            try:
                decoded = unquote(text)
                if decoded and decoded != text:
                    text = decoded
            except Exception:  # noqa: BLE001
                pass

        return text.strip()

    def _search_gallery_ids(self, query: str) -> list[int]:
        terms = [t.replace("_", " ").strip().lower() for t in query.strip().split() if t.strip()]
        if not terms:
            return []

        state = {
            "area": "all",
            "tag": "index",
            "language": "chinese",
            "orderby": "date",
            "orderbykey": "added",
            "orderbydirection": "desc",
        }

        positive_terms: list[str] = []
        negative_terms: list[str] = []
        for term in terms:
            if re.match(r"^(?:sort|order)by(?:key|direction)?:", term):
                left_side, right_side = (term.split(":", 1) + [""])[:2]
                if re.match(r"^(?:sort|order)(?:by)?key$", left_side):
                    state["orderbykey"] = re.sub(r"[^0-9a-z]", "", right_side)
                elif right_side in {"popular", "popularity"}:
                    state["orderby"] = "popular"
                elif right_side == "date":
                    state["orderby"] = "date"
                elif right_side == "datepublished":
                    state["orderby"] = "date"
                    state["orderbykey"] = "published"
                elif re.match(r"^(?:sort|order)by$", left_side) and right_side in {"random", "rand"}:
                    state["orderbydirection"] = "random"
                elif left_side in {"orderbydirection", "sortbydirection"}:
                    state["orderbydirection"] = re.sub(r"[^0-9a-z]", "", right_side)
                continue

            if term == "or":
                continue

            if term.startswith("-"):
                token = term[1:].strip()
                if token:
                    negative_terms.append(token)
            else:
                positive_terms.append(term)

        positive_terms.sort(key=lambda t: 0 if ":" in t else 1)
        if state["orderbykey"] == "":
            state["orderbykey"] = "year" if state["orderby"] == "popular" else "added"

        if not positive_terms or (":" not in positive_terms[0] and state["orderbykey"] != "added"):
            results = self._get_galleryids_from_nozomi_state(state)
        else:
            first = positive_terms.pop(0)
            results = self._get_galleryids_for_query_term(first, state)

        for term in positive_terms:
            subset = set(self._get_galleryids_for_query_term(term, state))
            if not subset:
                return []
            results = [gallery_id for gallery_id in results if gallery_id in subset]
            if not results:
                return []

        for term in negative_terms:
            excluded = set(self._get_galleryids_for_query_term(term, state))
            if not excluded:
                continue
            results = [gallery_id for gallery_id in results if gallery_id not in excluded]
            if not results:
                return []

        return results

    def _get_galleryids_for_query_term(self, query_term: str, state: dict[str, str]) -> list[int]:
        term = query_term.replace("_", " ").strip().lower()
        if not term:
            return []

        if ":" in term:
            left_side, right_side = (term.split(":", 1) + [""])[:2]
            scoped_state = dict(state)
            if left_side in {"female", "male"}:
                scoped_state["area"] = "tag"
                scoped_state["tag"] = term
            elif left_side == "language":
                scoped_state["language"] = right_side.strip() or state.get("language", "chinese")
            else:
                scoped_state["area"] = left_side.strip() or "all"
                scoped_state["tag"] = right_side.strip() or "index"
            return self._get_galleryids_from_nozomi_state(scoped_state)

        return self._get_galleryids_for_free_term(term)

    def _get_galleryids_for_free_term(self, term: str) -> list[int]:
        cache_key = term.strip().lower()
        if cache_key in self._search_term_cache:
            return list(self._search_term_cache[cache_key])

        key = hashlib.sha256(cache_key.encode("utf-8")).digest()[:4]
        versions = self._get_search_versions()
        index_version = str(versions.get("galleriesindex") or "").strip()
        if not index_version:
            return []

        root = self._fetch_search_node(field="galleries", version=index_version, address=0)
        data_entry = self._search_btree(field="galleries", version=index_version, key=key, node=root)
        ids = self._read_galleryids_from_data(version=index_version, data_entry=data_entry)

        if len(self._search_term_cache) > 120:
            self._search_term_cache.clear()
        self._search_term_cache[cache_key] = list(ids)
        return ids

    def _get_galleryids_from_nozomi_state(self, state: dict[str, str]) -> list[int]:
        area = str(state.get("area") or "all").strip().lower() or "all"
        tag = str(state.get("tag") or "index").strip().lower() or "index"
        language = str(state.get("language") or "chinese").strip().lower() or "chinese"
        orderby = str(state.get("orderby") or "date").strip().lower() or "date"
        orderbykey = str(state.get("orderbykey") or "added").strip().lower() or "added"

        if orderby != "date" or orderbykey == "published":
            if area == "all":
                path = f"n/{orderby}/{orderbykey}-{language}.nozomi"
            else:
                path = f"n/{area}/{orderby}/{orderbykey}/{tag}-{language}.nozomi"
        elif area == "all":
            path = f"n/{tag}-{language}.nozomi"
        else:
            path = f"n/{area}/{tag}-{language}.nozomi"

        url = f"https://{self.search_domain}/{path}"
        return self._fetch_nozomi_all(url)

    def _fetch_nozomi_all(self, nozomi_url: str) -> list[int]:
        try:
            resp = self.session.get(
                nozomi_url,
                timeout=12,
                allow_redirects=True,
                headers={"Referer": self.base_url},
            )
        except Exception:  # noqa: BLE001
            return []

        if resp.status_code != 200:
            return []

        payload = resp.content or b""
        usable = len(payload) - (len(payload) % _NOZOMI_INT_SIZE)
        if usable < _NOZOMI_INT_SIZE:
            return []

        ids: list[int] = []
        for idx in range(0, usable, _NOZOMI_INT_SIZE):
            (gallery_id,) = struct.unpack(">I", payload[idx : idx + _NOZOMI_INT_SIZE])
            if gallery_id > 0:
                ids.append(int(gallery_id))
        return ids

    def _get_search_versions(self) -> dict[str, str]:
        now = time.time()
        cached_at = float(self._search_versions.get("at") or 0.0)
        cached_data = self._search_versions.get("data")
        if now - cached_at <= 3600 and isinstance(cached_data, dict) and cached_data:
            return dict(cached_data)

        data: dict[str, str] = {}
        for name in ("galleriesindex", "languagesindex", "nozomiurlindex"):
            url = f"https://{self.search_domain}/{name}/version"
            text = self._get_text(url, referer=self.base_url).strip()
            if text and re.fullmatch(r"\d{6,20}", text):
                data[name] = text

        if data:
            self._search_versions["at"] = now
            self._search_versions["data"] = dict(data)
            return data

        if isinstance(cached_data, dict):
            return dict(cached_data)
        return {}

    def _fetch_search_node(self, field: str, version: str, address: int) -> dict | None:
        index_url = f"https://{self.search_domain}/galleriesindex/{field}.{version}.index"
        payload = self._range_get(index_url, address, address + _SEARCH_NODE_SIZE - 1)
        if not payload:
            return None
        return self._decode_search_node(payload)

    def _decode_search_node(self, payload: bytes) -> dict | None:
        try:
            view = memoryview(payload)
            pos = 0
            number_of_keys = int.from_bytes(view[pos : pos + 4], "big", signed=True)
            pos += 4

            if number_of_keys < 0 or number_of_keys > 256:
                return None

            keys: list[bytes] = []
            for _ in range(number_of_keys):
                key_size = int.from_bytes(view[pos : pos + 4], "big", signed=True)
                pos += 4
                if key_size <= 0 or key_size > 32:
                    return None
                keys.append(bytes(view[pos : pos + key_size]))
                pos += key_size

            number_of_datas = int.from_bytes(view[pos : pos + 4], "big", signed=True)
            pos += 4
            if number_of_datas < 0 or number_of_datas > 256:
                return None

            datas: list[tuple[int, int]] = []
            for _ in range(number_of_datas):
                offset = int.from_bytes(view[pos : pos + 8], "big", signed=False)
                pos += 8
                length = int.from_bytes(view[pos : pos + 4], "big", signed=True)
                pos += 4
                datas.append((int(offset), int(length)))

            subnodes: list[int] = []
            for _ in range(_SEARCH_B + 1):
                sub_addr = int.from_bytes(view[pos : pos + 8], "big", signed=False)
                pos += 8
                subnodes.append(int(sub_addr))

            return {"keys": keys, "datas": datas, "subnodes": subnodes}
        except Exception:  # noqa: BLE001
            return None

    def _search_btree(self, field: str, version: str, key: bytes, node: dict | None) -> tuple[int, int] | None:
        current = node
        while current:
            keys = list(current.get("keys") or [])
            datas = list(current.get("datas") or [])
            subnodes = list(current.get("subnodes") or [])
            if not keys:
                return None

            idx = 0
            cmp_result = -1
            for idx, existing in enumerate(keys):
                if key < existing:
                    cmp_result = -1
                    break
                if key == existing:
                    cmp_result = 0
                    break
                cmp_result = 1
            else:
                idx = len(keys)

            if cmp_result == 0:
                if idx < len(datas):
                    return datas[idx]
                return None

            if idx >= len(subnodes):
                return None
            next_address = int(subnodes[idx] or 0)
            if next_address <= 0:
                return None
            current = self._fetch_search_node(field=field, version=version, address=next_address)
        return None

    def _read_galleryids_from_data(self, version: str, data_entry: tuple[int, int] | None) -> list[int]:
        if not data_entry:
            return []

        offset, length = data_entry
        if length <= 0 or length > 150_000_000:
            return []

        data_url = f"https://{self.search_domain}/galleriesindex/galleries.{version}.data"
        payload = self._range_get(data_url, offset, offset + length - 1)
        if not payload or len(payload) < 8:
            return []

        view = memoryview(payload)
        number_of_galleryids = int.from_bytes(view[0:4], "big", signed=True)
        if number_of_galleryids <= 0:
            return []

        expected_length = 4 + number_of_galleryids * 4
        if expected_length > len(payload):
            return []

        ids: list[int] = []
        pos = 4
        for _ in range(number_of_galleryids):
            gallery_id = int.from_bytes(view[pos : pos + 4], "big", signed=True)
            pos += 4
            if gallery_id > 0:
                ids.append(int(gallery_id))
        return ids

    def _range_get(self, url: str, start: int, end: int) -> bytes:
        try:
            resp = self.session.get(
                url,
                timeout=12,
                allow_redirects=True,
                headers={
                    "Range": f"bytes={max(0, int(start))}-{max(int(start), int(end))}",
                    "Referer": self.base_url,
                },
            )
        except Exception:  # noqa: BLE001
            return b""

        if resp.status_code not in {200, 206}:
            return b""
        return bytes(resp.content or b"")

    def topic_images(self, detail_url: str) -> list[str]:
        gallery_id = self._extract_gallery_id(detail_url)
        if not gallery_id:
            raise RuntimeError("cannot parse hitomi gallery id")

        gallery = self._fetch_gallery_info(gallery_id)
        if not gallery:
            direct_urls = self._extract_direct_image_urls(gallery_id)
            if direct_urls:
                return direct_urls
            raise RuntimeError("hitomi gallery metadata unavailable")

        files = gallery.get("files") or []
        if not isinstance(files, list) or not files:
            return []

        gg_params = self._get_gg_params()

        images: list[str] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                continue
            hash_value = str(item.get("hash") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", hash_value):
                continue

            ext, dir_name = self._pick_image_variant(item)
            image_url = self._build_image_url(hash_value, dir_name, ext, gg_params)
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            images.append(image_url)

        return images

    def _parse_query(self, query: str) -> _QueryPlan:
        raw_terms = [t.strip() for t in (query or "").split() if t.strip()]
        language = "chinese"
        language_explicit = False
        tag_filters: list[tuple[str, str]] = []
        positive_terms: list[str] = []
        negative_terms: list[str] = []

        for term in raw_terms:
            neg = term.startswith("-")
            token = term[1:] if neg else term
            token = token.strip().lower()
            if not token:
                continue

            if ":" in token:
                ns, value = token.split(":", 1)
                ns = ns.strip().lower()
                value = self._normalize_term(value)
                if not ns or not value:
                    continue

                if ns in {"sortby", "orderby", "orderbykey", "orderbydirection"}:
                    continue

                if ns == "language":
                    language = self._normalize_language(value)
                    language_explicit = True
                    continue

                if ns in {
                    "female",
                    "male",
                    "tag",
                    "type",
                    "artist",
                    "group",
                    "series",
                    "character",
                    "parody",
                }:
                    tag_filters.append((ns, value))
                    continue

                positive_terms.append(f"{ns}:{value.replace('_', ' ')}")
                continue

            normalized = token.replace("_", " ")
            if neg:
                negative_terms.append(normalized)
            else:
                positive_terms.append(normalized)

        if tag_filters and not language_explicit:
            language = "all"

        nozomi_key = f"index-{language}"
        if tag_filters:
            ns, value = tag_filters[0]
            nozomi_key = f"{ns}/{value}-{language}"

        return _QueryPlan(
            language=language,
            nozomi_key=nozomi_key,
            positive_terms=positive_terms,
            negative_terms=negative_terms,
            tag_filters=tag_filters,
        )

    def _normalize_language(self, value: str) -> str:
        alias = {
            "zh": "chinese",
            "cn": "chinese",
            "chs": "chinese",
            "cht": "chinese",
            "jp": "japanese",
            "ja": "japanese",
            "en": "english",
            "all": "all",
        }
        key = value.strip().lower().replace(" ", "").replace("_", "")
        return alias.get(key, value.strip().lower().replace(" ", "_"))

    def _normalize_term(self, value: str) -> str:
        normalized = value.strip().lower().replace(" ", "_")
        normalized = re.sub(r"[^a-z0-9_\-]", "", normalized)
        return normalized

    def _fetch_nozomi_ids(
        self,
        nozomi_key: str,
        start: int,
        count: int,
        allow_fallback: bool = True,
    ) -> list[int]:
        start = max(0, start)
        count = max(1, count)
        start_byte = start * _NOZOMI_INT_SIZE
        end_byte = start_byte + count * _NOZOMI_INT_SIZE - 1

        for domain in self.asset_domains:
            url = f"https://{domain}/{nozomi_key}.nozomi"
            headers = {
                "Range": f"bytes={start_byte}-{end_byte}",
                "Referer": self.base_url,
            }
            try:
                resp = self.session.get(url, headers=headers, timeout=8, allow_redirects=True)
            except Exception:  # noqa: BLE001
                continue

            if resp.status_code not in {200, 206}:
                continue

            payload = resp.content or b""
            if resp.status_code == 200:
                payload = payload[start_byte : end_byte + 1]

            if len(payload) < _NOZOMI_INT_SIZE:
                continue

            ids: list[int] = []
            usable = len(payload) - (len(payload) % _NOZOMI_INT_SIZE)
            for idx in range(0, usable, _NOZOMI_INT_SIZE):
                (gallery_id,) = struct.unpack(">I", payload[idx : idx + _NOZOMI_INT_SIZE])
                if gallery_id > 0:
                    ids.append(int(gallery_id))
            if ids:
                return ids

        if nozomi_key != "index-chinese":
            return self._fetch_nozomi_ids("index-chinese", start=start, count=count)
        return []

    def _topics_from_ids(self, ids: list[int], include_meta: bool) -> list[dict]:
        topics: list[dict] = []
        for gallery_id in ids:
            topic = self._build_topic(gallery_id, include_meta=include_meta)
            if topic:
                topics.append(topic)
        return topics

    def _build_topic(self, gallery_id: int, include_meta: bool) -> dict | None:
        block_html = self._fetch_galleryblock_html(gallery_id)
        topic = self._parse_galleryblock(block_html, gallery_id)

        if not topic:
            topic = {
                "topic_id": str(gallery_id),
                "title": f"Hitomi #{gallery_id}",
                "cover_url": "",
                "detail_url": f"https://hitomi.la/galleries/{gallery_id}.html",
            }

        meta = None
        if include_meta:
            meta = self._fetch_gallery_info(gallery_id, max_urls=2 if include_meta else None)
            if meta:
                title = str(meta.get("japanese_title") or "").strip() or str(meta.get("title") or "").strip()
                if title:
                    topic["title"] = title
                gallery_url = str(meta.get("galleryurl") or "").strip()
                if gallery_url:
                    topic["detail_url"] = urljoin("https://hitomi.la/", gallery_url)

        topic["_search_blob"] = self._build_search_blob(topic, meta)
        if meta:
            topic["_meta"] = meta

        return topic

    def _fetch_galleryblock_html(self, gallery_id: int) -> str:
        for domain in self.asset_domains:
            url = f"https://{domain}/galleryblock/{gallery_id}.html"
            html = self._get_text(url, referer=self.base_url)
            if not html:
                continue
            if "404 not found" in html.lower():
                continue
            return html
        return ""

    def _parse_galleryblock(self, html: str, gallery_id: int) -> dict | None:
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        anchor = soup.select_one("a[href*='/galleries/'], a[href*='/reader/']")

        detail_url = ""
        if anchor:
            href = str(anchor.get("href") or "").strip()
            if href:
                detail_url = urljoin("https://hitomi.la/", href)
        if not detail_url:
            detail_url = f"https://hitomi.la/galleries/{gallery_id}.html"

        title_node = (
            soup.select_one("h1")
            or soup.select_one("h2")
            or soup.select_one(".gallery-title")
            or soup.select_one(".title")
            or anchor
        )
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title:
            title = f"Hitomi #{gallery_id}"

        cover_url = ""
        image = soup.select_one("img")
        if image:
            cover_url = self._extract_image_from_tag(image, base_url=detail_url)

        return {
            "topic_id": str(gallery_id),
            "title": title,
            "cover_url": cover_url,
            "detail_url": detail_url,
        }

    def _extract_image_from_tag(self, tag, base_url: str) -> str:
        candidates: list[str] = []

        for attr in ["data-src", "data-original", "data-lazy-src", "src"]:
            value = str(tag.get(attr) or "").strip()
            if value:
                candidates.append(value)

        for attr in ["data-srcset", "srcset"]:
            srcset = str(tag.get(attr) or "").strip()
            if not srcset:
                continue
            for part in srcset.split(","):
                piece = part.strip().split(" ")[0].strip()
                if piece:
                    candidates.append(piece)

        for item in candidates:
            full = urljoin(base_url, item).replace("\\/", "/").strip()
            if full.startswith("//"):
                full = "https:" + full
            if self._is_valid_image_url(full):
                return full

        return ""

    def _topic_matches(self, topic: dict, plan: _QueryPlan) -> bool:
        blob = str(topic.get("_search_blob") or "").lower()

        for term in plan.positive_terms:
            if term and term not in blob:
                return False

        for term in plan.negative_terms:
            if term and term in blob:
                return False

        if not plan.tag_filters:
            return True

        meta = topic.get("_meta")
        if not isinstance(meta, dict):
            return False

        return self._match_tag_filters(meta, plan.tag_filters)

    def _build_search_blob(self, topic: dict, meta: dict | None) -> str:
        pieces: list[str] = [str(topic.get("title") or "")]

        if isinstance(meta, dict):
            pieces.append(str(meta.get("title") or ""))
            pieces.append(str(meta.get("japanese_title") or ""))
            pieces.append(str(meta.get("type") or ""))
            pieces.append(str(meta.get("language") or ""))

            def add_list(values: Any, key: str) -> None:
                if not isinstance(values, list):
                    return
                for item in values:
                    if isinstance(item, dict):
                        pieces.append(str(item.get(key) or ""))

            add_list(meta.get("artists"), "artist")
            add_list(meta.get("groups"), "group")
            add_list(meta.get("parodys"), "parody")
            add_list(meta.get("characters"), "character")

            tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
            for item in tags:
                if not isinstance(item, dict):
                    continue
                tag_name = str(item.get("tag") or "")
                if not tag_name:
                    continue
                pieces.append(tag_name)
                if item.get("male"):
                    pieces.append(f"male:{tag_name}")
                if item.get("female"):
                    pieces.append(f"female:{tag_name}")

        return " ".join(pieces).strip().lower().replace("_", " ")

    def _match_tag_filters(self, meta: dict, filters: list[tuple[str, str]]) -> bool:
        tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []

        def normalized(v: str) -> str:
            return v.strip().lower().replace("_", " ")

        for namespace, value in filters:
            namespace = namespace.strip().lower()
            target = normalized(value)

            if namespace == "type":
                if normalized(str(meta.get("type") or "")) != target:
                    return False
                continue

            if namespace == "artist":
                artists = meta.get("artists") if isinstance(meta.get("artists"), list) else []
                if not any(normalized(str(it.get("artist") or "")) == target for it in artists if isinstance(it, dict)):
                    return False
                continue

            if namespace == "group":
                groups = meta.get("groups") if isinstance(meta.get("groups"), list) else []
                if not any(normalized(str(it.get("group") or "")) == target for it in groups if isinstance(it, dict)):
                    return False
                continue

            if namespace in {"series", "parody"}:
                parodys = meta.get("parodys") if isinstance(meta.get("parodys"), list) else []
                if not any(normalized(str(it.get("parody") or "")) == target for it in parodys if isinstance(it, dict)):
                    return False
                continue

            if namespace == "character":
                chars = meta.get("characters") if isinstance(meta.get("characters"), list) else []
                if not any(normalized(str(it.get("character") or "")) == target for it in chars if isinstance(it, dict)):
                    return False
                continue

            if namespace == "male":
                if not any(it.get("male") and normalized(str(it.get("tag") or "")) == target for it in tags if isinstance(it, dict)):
                    return False
                continue

            if namespace == "female":
                if not any(it.get("female") and normalized(str(it.get("tag") or "")) == target for it in tags if isinstance(it, dict)):
                    return False
                continue

            if namespace == "tag":
                if not any(normalized(str(it.get("tag") or "")) == target for it in tags if isinstance(it, dict)):
                    return False
                continue

            blob = self._build_search_blob({"title": ""}, meta)
            if f"{namespace}:{target}" not in blob and target not in blob:
                return False

        return True

    def _extract_gallery_id(self, detail_url: str) -> int | None:
        match = _GALLERY_ID_PATTERN.search(detail_url or "")
        if match:
            return int(match.group(1))
        raw = str(detail_url or "").strip()
        if raw.isdigit():
            return int(raw)
        return None

    def _fetch_gallery_info(self, gallery_id: int, max_urls: int | None = None) -> dict | None:
        urls = self._gallery_info_urls(gallery_id)
        if max_urls is not None:
            urls = urls[: max(1, int(max_urls))]

        for url in urls:
            script = self._get_text(url, referer=f"https://hitomi.la/galleries/{gallery_id}.html")
            info = self._parse_gallery_info_script(script)
            if info:
                return info

        if max_urls is not None:
            return None

        for page in (
            f"https://hitomi.la/galleries/{gallery_id}.html",
            f"https://hitomi.la/reader/{gallery_id}.html",
        ):
            html = self._get_text(page, referer=self.base_url)
            if not html:
                continue
            info = self._parse_gallery_info_script(html)
            if info:
                return info

        return None

    def _gallery_info_urls(self, gallery_id: int) -> list[str]:
        segments = [
            f"galleries/{gallery_id}.js",
            f"galleries/{gallery_id // 1000}/{gallery_id}.js",
            f"galleries/{gallery_id // 10000}/{gallery_id}.js",
            f"galleries/{gallery_id // 100000}/{gallery_id}.js",
        ]

        urls: list[str] = []
        seen: set[str] = set()
        for domain in self.asset_domains:
            for seg in segments:
                url = f"https://{domain}/{seg}"
                if url in seen:
                    continue
                seen.add(url)
                urls.append(url)
        return urls

    def _parse_gallery_info_script(self, script_text: str) -> dict | None:
        if not script_text:
            return None

        text = script_text.strip()
        if not text:
            return None

        if "404 not found" in text.lower() and "galleryinfo" not in text:
            return None

        candidate = ""
        if text.startswith("{") and text.endswith("}"):
            candidate = text
        else:
            idx = text.find("galleryinfo")
            if idx >= 0:
                brace_start = text.find("{", idx)
                if brace_start >= 0:
                    candidate = self._extract_braced_json(text, brace_start)

        if not candidate:
            return None

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("files"), list):
            return None
        return data

    def _extract_braced_json(self, text: str, start_idx: int) -> str:
        depth = 0
        in_string = False
        escaped = False
        quote_char = '"'

        for idx in range(start_idx, len(text)):
            ch = text[idx]

            if in_string:
                if escaped:
                    escaped = False
                    continue
                if ch == "\\":
                    escaped = True
                    continue
                if ch == quote_char:
                    in_string = False
                continue

            if ch in {"'", '"'}:
                in_string = True
                quote_char = ch
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : idx + 1]

        return ""

    def _extract_direct_image_urls(self, gallery_id: int) -> list[str]:
        pages = (
            f"https://hitomi.la/galleries/{gallery_id}.html",
            f"https://hitomi.la/reader/{gallery_id}.html",
        )
        pattern = re.compile(
            r"https?://[^\"'\s]+?\.(?:jpg|jpeg|png|webp|gif|avif)(?:\?[^\"'\s]*)?",
            re.IGNORECASE,
        )

        images: list[str] = []
        seen: set[str] = set()
        for page in pages:
            html = self._get_text(page, referer=self.base_url)
            if not html:
                continue

            for raw in pattern.findall(html):
                url = raw.strip().replace("\\/", "/")
                if not self._is_valid_image_url(url):
                    continue
                if "gold-usergeneratedcontent.net" not in url and "hitomi.la" not in url:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                images.append(url)

            if images:
                break

        return images

    def _pick_image_variant(self, file_item: dict) -> tuple[str, str]:
        # Prefer broad-compatible webp when filename already indicates it.
        name = str(file_item.get("name") or "").lower().strip()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""

        if ext == "webp":
            return "webp", "webp"
        if ext == "avif":
            return "avif", "avif"

        if bool(file_item.get("haswebp")):
            return "webp", "webp"
        if bool(file_item.get("hasavif")):
            return "avif", "avif"

        if ext not in _IMAGE_EXTS:
            ext = "jpg"
        if not ext:
            ext = "jpg"
        return ext, ext

    def _build_image_url(self, hash_value: str, dir_name: str, ext: str, gg: _GgParams) -> str:
        hash_value = hash_value.lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", hash_value):
            return ""

        x = int(hash_value[-1] + hash_value[-3:-1], 16)
        # In gg.js, listed "case N:" values map to m=1, default m=0.
        m = 1 if x in gg.zero_cases else 0

        if dir_name == "webp":
            subdomain = f"w{1 + m}"
        elif dir_name == "avif":
            subdomain = f"a{1 + m}"
        else:
            subdomain = str(1 + m)

        base_prefix = gg.base_prefix.strip("/") + "/"
        serial = str(int(hash_value[-1] + hash_value[-3:-1], 16))

        if dir_name in {"webp", "avif"}:
            path = f"{base_prefix}{serial}/{hash_value}.{ext}"
        else:
            path = f"{dir_name}/{base_prefix}{serial}/{hash_value}.{ext}"

        return f"https://{subdomain}.gold-usergeneratedcontent.net/{path}"

    def _get_gg_params(self) -> _GgParams:
        now = time.time()
        cached_at = float(self._gg_cache.get("at") or 0)
        if now - cached_at <= 1800:
            params = self._gg_cache.get("params")
            if isinstance(params, _GgParams):
                return params

        for domain in self.asset_domains:
            script = self._get_text(f"https://{domain}/gg.js", referer=self.base_url)
            params = self._parse_gg_script(script)
            if params:
                self._gg_cache["at"] = now
                self._gg_cache["params"] = params
                return params

        params = self._gg_cache.get("params")
        if isinstance(params, _GgParams):
            return params

        return _GgParams(base_prefix=_DEFAULT_GG_BASE, zero_cases=set())

    def _parse_gg_script(self, script_text: str) -> _GgParams | None:
        if not script_text:
            return None

        base_match = re.search(r"\bb:\s*'([^']+)'", script_text)
        base_prefix = base_match.group(1).strip() if base_match else _DEFAULT_GG_BASE
        if not base_prefix:
            base_prefix = _DEFAULT_GG_BASE
        if not base_prefix.endswith("/"):
            base_prefix += "/"

        zero_cases = {int(v) for v in re.findall(r"case\s+(\d+)\s*:", script_text)}
        return _GgParams(base_prefix=base_prefix, zero_cases=zero_cases)

    def _get_text(self, url: str, referer: str = "") -> str:
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer

        try:
            resp = self.session.get(url, headers=headers, timeout=8, allow_redirects=True)
        except Exception:  # noqa: BLE001
            return ""

        if resp.status_code != 200:
            return ""
        return resp.text or ""

    def _is_valid_image_url(self, url: str) -> bool:
        lower = (url or "").lower().strip()
        if not lower:
            return False
        if lower.startswith("data:") or lower.startswith("javascript:"):
            return False
        if "base64," in lower:
            return False
        return lower.startswith("http://") or lower.startswith("https://")



















