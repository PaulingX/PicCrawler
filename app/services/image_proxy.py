"""图片 URL 归一化、代理与抓取回退的共享逻辑。

从原来的 routes.py 中抽离，便于在多个蓝图间复用，也便于离线单测。
本模块不依赖 Flask 的 request/response，只依赖标准库与 requests。
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests import Response as RequestsResponse
from requests.exceptions import RequestException

from app.config import USER_AGENT
from app.database import execute, query_all
from app.services.hitomi_urls import hitomi_candidate_urls
from app.services.rule_registry import build_crawler
from app.services.utils import header_safe_url

# 共享连接池 Session：浏览页一次会请求几十张代理图片，
# 复用 TCP/TLS 连接能显著降低延迟，也减少对远端的握手压力。
# 具体的失败重试由 _fetch_image_with_fallbacks 的候选链负责，适配层不重试。
_HTTP_SESSION = requests.Session()
_POOL_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32)
_HTTP_SESSION.mount("https://", _POOL_ADAPTER)
_HTTP_SESSION.mount("http://", _POOL_ADAPTER)


def _is_low_quality_gallery_url(rule_id: str, url: str) -> bool:
    if rule_id not in {"wnacg", "manxiangge", "hotgirl"}:
        return False

    lower = str(url or "").lower()
    if not lower:
        return True

    parsed = urlparse(lower)
    host = parsed.hostname or ""
    path = parsed.path or ""

    if rule_id == "hotgirl":
        if "wp-postratings" in path:
            return True
        if "/wp-content/themes/" in path:
            return True
        if host in {"hotgirl.asia", "www.hotgirl.asia"} and re.search(
            r"-\d+x\d+\.(jpg|jpeg|png|webp|avif)$",
            path,
        ):
            # Typical list/related thumbnail pattern, not gallery original.
            return True
        return False

    # Legacy malformed output like https://www.wnacg.com//t4.xxx/data/t/...
    if host in {"www.wnacg.com", "wnacg.com"} and path.startswith("//"):
        return True

    if "/data/t/" in path:
        return True
    if "/thumb/" in path or "/thumbnail/" in path or "/preview/" in path:
        return True
    if re.match(r"^t\d+\.", host):
        return True
    if host.startswith("t") and "/data/" in path:
        return True

    return False


def _cached_images_need_refresh(rule_id: str, urls: list[str]) -> bool:
    if not urls:
        return False
    if rule_id == "hitomi-chinese":
        # Hitomi URL algorithm updates can invalidate cached domains/paths;
        # always refresh to avoid serving stale 404 links.
        return True
    if rule_id not in {"wnacg", "manxiangge", "hotgirl"}:
        return False

    return any(_is_low_quality_gallery_url(rule_id, u) for u in urls)


def _get_or_fetch_images(rule_id: str, topic_id: str, detail_url: str) -> list[str]:
    crawler = None
    rows = query_all(
        """
        SELECT image_url
        FROM online_image_cache
        WHERE rule_id = ? AND topic_id = ?
        ORDER BY image_index
        """,
        (rule_id, topic_id),
    )
    if rows:
        cached = [str(r["image_url"]) for r in rows]
        cached = [u for u in cached if _is_displayable_image_url(u)]
        # Auto-refresh stale cache by comparing declared gallery total.
        if rule_id in {"4khd", "hotgirl"} and cached:
            try:
                crawler = build_crawler(rule_id)
                count_fetcher = getattr(crawler, "topic_image_count", None)
                if callable(count_fetcher):
                    declared_count = max(0, int(count_fetcher(detail_url)))
                    if declared_count > 0 and declared_count != len(cached):
                        cached = []
            except Exception:  # noqa: BLE001
                pass
        if cached and not _cached_images_need_refresh(rule_id, cached):
            return cached

    if crawler is None:
        crawler = build_crawler(rule_id)
    raw_images = crawler.topic_images(detail_url)

    preferred: list[str] = []
    fallback: list[str] = []
    seen_urls: set[str] = set()

    for raw_url in raw_images:
        if not _is_displayable_image_url(raw_url):
            continue
        if raw_url in seen_urls:
            continue

        seen_urls.add(raw_url)
        fallback.append(raw_url)

        if _is_low_quality_gallery_url(rule_id, raw_url):
            continue

        preferred.append(raw_url)

    images = preferred if preferred else fallback

    execute(
        "DELETE FROM online_image_cache WHERE rule_id = ? AND topic_id = ?",
        (rule_id, topic_id),
    )
    for idx, image_url in enumerate(images, start=1):
        execute(
            """
            INSERT INTO online_image_cache(rule_id, topic_id, image_index, image_url, cached_at)
            VALUES(?, ?, ?, ?, datetime('now'))
            """,
            (rule_id, topic_id, idx, image_url),
        )
    return images


def _is_displayable_image_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    if lower.startswith("data:") or lower.startswith("javascript:"):
        return False
    if "base64," in lower:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return bool(parsed.netloc)


_IMGBOX_HOST_PATTERN = re.compile(r"^(?:images|thumbs)\d*\.imgbox\.com$")


def _is_imgbox_image_host(host: str) -> bool:
    return bool(_IMGBOX_HOST_PATTERN.match(str(host or "").strip().lower()))


def _normalize_remote_image_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    def _is_4khd_origin_host(origin_host: str) -> bool:
        origin = str(origin_host or "").strip().lower().strip(".")
        return bool(origin) and (origin == "4khd.com" or origin.endswith(".4khd.com"))

    def _rewrite_4khd_wp_to_uuss(origin_path: str, query: str) -> str:
        # 4KHD mirror injects a service worker that rewrites wp.com image URLs
        # to img.uuss.uk. Mirror this rewrite server-side so crawler/proxy can
        # fetch stable image URLs without relying on remote JS execution.
        return urlunparse(("https", "img.uuss.uk", origin_path, "", query, ""))

    # WordPress CDN wrapper: i0.wp.com/<origin-host>/<path>?w=1300
    if host.endswith(".wp.com"):
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) == 2 and "." in parts[0]:
            origin_host = parts[0].strip().lower()
            origin_path = "/" + parts[1]
            # 4KHD wp wrapper currently fails directly (400 in many regions).
            # Rewrite to img.uuss.uk path as done by upstream service worker.
            if _is_4khd_origin_host(origin_host):
                return _rewrite_4khd_wp_to_uuss(origin_path, parsed.query)
            # Other origins: keep the wrapper. Photon(i*.wp.com) caches the
            # real image bytes; the unwrapped origin may no longer serve them
            # (imgbox answers every direct request with a placeholder JPEG).
            return url

    # imgbox direct hotlinks now serve a 240x240 "Thumbnail Temporarily
    # Unavailable" placeholder to every client; Photon still holds the real
    # image, so route imgbox through the wp.com wrapper.
    if _is_imgbox_image_host(host):
        return urlunparse(("https", "i1.wp.com", f"/{host}{parsed.path or '/'}", "", parsed.query, ""))

    # Normalize legacy 4khd image hosts to current mirror image host.
    if host == "pic.4khd.com" or host == "img.4khd.com":
        return _rewrite_4khd_wp_to_uuss(parsed.path, parsed.query)

    return url


def _wrap_wp_proxy_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or host.endswith(".wp.com"):
        return url

    # Keep WNACG raw image host to avoid wp.com upstream 400/502.
    if host.endswith("qy0.ru"):
        return url

    # Only use WordPress CDN wrapper for known unstable sources.
    if not host.endswith("4khd.com"):
        return url

    # Wrap origin image by WordPress image CDN as a fallback display URL.
    width = ""
    match = re.search(r"/w(\d+)-rw/", parsed.path)
    if match:
        width = match.group(1)

    query = parsed.query.strip()
    if width and "w=" not in query:
        query = f"w={width}" if not query else f"{query}&w={width}"

    wrapped_path = f"/{host}{parsed.path}"
    return urlunparse(("https", "i0.wp.com", wrapped_path, "", query, ""))


def _prefer_display_image_url(url: str) -> str:
    if not url:
        return ""

    normalized = _normalize_remote_image_url(url)
    wrapped = _wrap_wp_proxy_url(normalized)
    if _is_displayable_image_url(wrapped) and wrapped != normalized:
        return wrapped
    return url


def _proxy_remote_image_url(image_url: str, referer: str = "") -> str:
    if not image_url:
        return ""
    if image_url.startswith("/api/online/image-proxy"):
        return image_url

    normalized = _normalize_remote_image_url(image_url)
    target_url = normalized if _is_displayable_image_url(normalized) else image_url
    encoded = quote(target_url, safe="")
    if referer:
        encoded_ref = quote(referer, safe="")
        return f"/api/online/image-proxy?url={encoded}&referer={encoded_ref}"
    return f"/api/online/image-proxy?url={encoded}"


def _is_safe_remote_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    except ValueError:
        pass

    return True


def _build_proxy_headers(referer: str = "", minimal: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if minimal:
        return headers
    if referer and _is_safe_remote_url(referer):
        # hitomi 中文站详情页含非 ASCII 字符，Referer 头必须先做头部安全化，
        # 否则 requests 发送时抛 UnicodeEncodeError，代理直接 500。
        safe_referer = header_safe_url(referer)
        parsed_referer = urlparse(safe_referer)
        headers["Referer"] = safe_referer
        if parsed_referer.scheme and parsed_referer.netloc:
            headers["Origin"] = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
    return headers


def _http_fallback_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return url
    return urlunparse(("http", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _candidate_fetch_urls(image_url: str) -> list[str]:
    raw = image_url
    normalized = _normalize_remote_image_url(raw)
    wrapped = _wrap_wp_proxy_url(normalized)
    display = _prefer_display_image_url(raw)

    # Prefer origin/normalized URL first; wp.com wrapper is only fallback.
    candidates: list[str] = [normalized, raw, wrapped, display]

    for u in [normalized, raw, wrapped, display]:
        if u and urlparse(u).scheme == "https":
            candidates.append(_http_fallback_url(u))

    # hitomi 图片可用性由 haswebp/hasavif 决定且路径前缀会轮换，
    # 主 URL 404 时按共享候选链回退（webp/avif 互换、子域互换等）。
    for u in [normalized, raw]:
        candidates.extend(hitomi_candidate_urls(u))

    unique: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _pick_direct_redirect_url(image_url: str) -> str:
    candidates = _candidate_fetch_urls(image_url)

    for url in candidates:
        host = (urlparse(url).hostname or "").lower()
        if host.endswith(".wp.com") and _is_safe_remote_url(url):
            return url

    for url in candidates:
        if urlparse(url).scheme == "https" and _is_safe_remote_url(url):
            return url

    for url in candidates:
        if _is_safe_remote_url(url):
            return url

    return ""


def _try_fetch_image(url: str, headers: dict[str, str]) -> RequestsResponse:
    return _HTTP_SESSION.get(
        url,
        headers=headers,
        timeout=25,
        stream=True,
        allow_redirects=True,
        verify=True,
    )


def _looks_like_image_response(resp: RequestsResponse, request_url: str) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if content_type.startswith("image/"):
        return True
    if resp.status_code == 200 and content_type.startswith("application/octet-stream"):
        return True

    path_lower = urlparse(request_url).path.lower()
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
    if any(path_lower.endswith(ext) for ext in image_exts) and resp.status_code == 200:
        return True

    return False


def _fetch_image_with_fallbacks(image_url: str, referer: str) -> tuple[RequestsResponse | None, str]:
    attempt_errors: list[str] = []

    for candidate in _candidate_fetch_urls(image_url):
        if not _is_safe_remote_url(candidate):
            continue

        plans = [
            (candidate, _build_proxy_headers(referer=referer, minimal=False), "referer"),
            (candidate, _build_proxy_headers(referer=referer, minimal=True), "minimal"),
        ]

        for url, headers, label in plans:
            try:
                resp = _try_fetch_image(url=url, headers=headers)
            except RequestException as exc:
                attempt_errors.append(f"{label}:{urlparse(url).netloc}:{exc.__class__.__name__}")
                continue

            if resp.status_code == 200 and _looks_like_image_response(resp, request_url=url):
                warn = ""
                if url != image_url or label != "referer":
                    warn = f"fallback={label}:{urlparse(url).netloc}"
                return resp, warn

            content_type = (resp.headers.get("Content-Type") or "").lower()
            attempt_errors.append(
                f"{label}:{urlparse(url).netloc}:status={resp.status_code}:ct={content_type[:32]}"
            )
            resp.close()

    return None, ",".join(attempt_errors[:8]) or "unknown"
