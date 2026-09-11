"""在线浏览接口：主题列表、主题图片分页、主题图片数量、图片代理。"""
from __future__ import annotations

import logging

from flask import Blueprint, Response, jsonify, redirect, request

_LOGGER = logging.getLogger("app.online")

from app.api.helpers import (
    _get_cached_topics,
    _is_rule_online_enabled,
    _save_topics,
)
from app.database import query_one
from app.services.image_proxy import (
    _fetch_image_with_fallbacks,
    _get_or_fetch_images,
    _is_displayable_image_url,
    _is_safe_remote_url,
    _pick_direct_redirect_url,
    _proxy_remote_image_url,
)
from app.services.rule_registry import build_crawler

bp = Blueprint("online", __name__, url_prefix="/api/online")


@bp.get("/topics")
def api_online_topics():
    rule_id = request.args.get("rule", "4khd").strip()
    page_no = max(1, int(request.args.get("page", "1")))
    query = request.args.get("q", "").strip()
    category_raw = request.args.get("category", "").strip()
    category_id = int(category_raw) if category_raw.isdigit() else None
    effective_category_id = category_id
    if rule_id == "wnacg" and query:
        # WNACG search endpoint is global and should ignore category.
        effective_category_id = None

    if not _is_rule_online_enabled(rule_id):
        return jsonify({"error": "该规则在线浏览已关闭"}), 403

    topics: list[dict] = []
    try:
        crawler = build_crawler(rule_id)
        # Rules with category browsing should fetch per category directly.
        use_cache = (not query) and effective_category_id is None and (
            rule_id not in {"asmhentai-zh", "wnacg", "manxiangge"}
        )
        if query:
            if effective_category_id is None:
                topics = crawler.list_topics(page_no, query=query)
            else:
                try:
                    topics = crawler.list_topics(page_no, query=query, category_id=effective_category_id)
                except TypeError:
                    topics = crawler.list_topics(page_no, query=query)
        elif use_cache:
            topics = _get_cached_topics(rule_id, page_no)
            if not topics:
                topics = crawler.list_topics(page_no, query="")
                _save_topics(rule_id, page_no, topics)
        else:
            if effective_category_id is None:
                topics = crawler.list_topics(page_no, query="")
            else:
                try:
                    topics = crawler.list_topics(page_no, query="", category_id=effective_category_id)
                except TypeError:
                    topics = crawler.list_topics(page_no, query="")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"在线抓取失败: {exc}"}), 502

    output = []
    for topic in topics:
        item = dict(topic)
        item["cover_url"] = _proxy_remote_image_url(item.get("cover_url", ""), item.get("detail_url", ""))
        output.append(item)

    return jsonify({"items": output, "page": page_no, "q": query, "category": effective_category_id})


@bp.get("/topic-images")
def api_online_topic_images():
    rule_id = request.args.get("rule", "4khd").strip()
    if not _is_rule_online_enabled(rule_id):
        return jsonify({"error": "该规则在线浏览已关闭"}), 403

    topic_id = request.args.get("topic_id", "").strip()
    detail_url = request.args.get("detail_url", "").strip()
    offset = max(0, int(request.args.get("offset", "0")))
    limit = max(1, min(100, int(request.args.get("limit", "20"))))

    if not topic_id or not detail_url:
        return jsonify({"error": "topic_id and detail_url are required"}), 400

    try:
        crawler = build_crawler(rule_id)
        paged_fetcher = getattr(crawler, "topic_images_page", None)

        if callable(paged_fetcher):
            page_data = paged_fetcher(detail_url=detail_url, offset=offset, limit=limit)
            raw_items = list(page_data.get("items") or []) if isinstance(page_data, dict) else []

            images: list[str] = []
            seen: set[str] = set()
            for raw in raw_items:
                url = str(raw or "").strip()
                if not _is_displayable_image_url(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                images.append(url)

            has_more = bool(page_data.get("has_more")) if isinstance(page_data, dict) else False
            total_raw = page_data.get("total") if isinstance(page_data, dict) else None
            try:
                total = int(total_raw)
            except (TypeError, ValueError):
                total = offset + len(images) + (1 if has_more else 0)
            total = max(0, total)

            next_offset_raw = page_data.get("next_offset") if isinstance(page_data, dict) else None
            try:
                next_offset = int(next_offset_raw)
            except (TypeError, ValueError):
                next_offset = offset + len(images)

            if has_more and next_offset <= offset:
                next_offset = offset + max(1, len(images))
            if total >= 0 and next_offset >= total:
                has_more = False

            proxied = [_proxy_remote_image_url(url, detail_url) for url in images]
            return jsonify(
                {
                    "items": proxied,
                    "offset": offset,
                    "limit": limit,
                    "total": total,
                    "next_offset": next_offset,
                    "has_more": has_more,
                }
            )

        all_images = _get_or_fetch_images(rule_id, topic_id, detail_url)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"主题图片抓取失败: {exc}"}), 502

    chunk = all_images[offset : offset + limit]
    proxied = [_proxy_remote_image_url(url, detail_url) for url in chunk]
    return jsonify(
        {
            "items": proxied,
            "offset": offset,
            "limit": limit,
            "total": len(all_images),
            "next_offset": offset + len(chunk),
            "has_more": offset + len(chunk) < len(all_images),
        }
    )


@bp.get("/topic-count")
def api_online_topic_count():
    rule_id = request.args.get("rule", "4khd").strip()
    if not _is_rule_online_enabled(rule_id):
        return jsonify({"error": "该规则在线浏览已关闭"}), 403

    topic_id = request.args.get("topic_id", "").strip()
    detail_url = request.args.get("detail_url", "").strip()
    if not topic_id or not detail_url:
        return jsonify({"error": "topic_id and detail_url are required"}), 400

    row = query_one(
        """
        SELECT MAX(image_index) AS cnt
        FROM online_image_cache
        WHERE rule_id = ? AND topic_id = ?
        """,
        (rule_id, topic_id),
    )
    cached_count = int(row["cnt"]) if row and row["cnt"] is not None else 0

    try:
        crawler = build_crawler(rule_id)

        count_fetcher = getattr(crawler, "topic_image_count", None)
        if callable(count_fetcher):
            count = max(0, int(count_fetcher(detail_url)))
            if rule_id == "hotgirl" and count <= 0:
                images = _get_or_fetch_images(rule_id, topic_id, detail_url)
                if images:
                    return jsonify({"count": len(images), "cached": False})
            return jsonify({"count": count, "cached": False})

        paged_fetcher = getattr(crawler, "topic_images_page", None)
        if callable(paged_fetcher):
            page_data = paged_fetcher(detail_url=detail_url, offset=0, limit=1)
            if isinstance(page_data, dict):
                total_raw = page_data.get("total")
                try:
                    total = int(total_raw)
                except (TypeError, ValueError):
                    total = -1
                if total >= 0:
                    return jsonify({"count": total, "cached": False})

        if rule_id == "hotgirl" and cached_count > 0:
            images = _get_or_fetch_images(rule_id, topic_id, detail_url)
            if images:
                return jsonify({"count": len(images), "cached": False})

        if cached_count > 0:
            return jsonify({"count": cached_count, "cached": True})

        images = _get_or_fetch_images(rule_id, topic_id, detail_url)
        return jsonify({"count": len(images), "cached": False})
    except Exception as exc:  # noqa: BLE001
        if rule_id == "hotgirl" and cached_count > 0:
            try:
                images = _get_or_fetch_images(rule_id, topic_id, detail_url)
                if images:
                    return jsonify({"count": len(images), "cached": False})
            except Exception:  # noqa: BLE001
                pass
        if cached_count > 0:
            return jsonify({"count": cached_count, "cached": True})
        return jsonify({"error": f"主题数量获取失败: {exc}"}), 502


@bp.get("/image-proxy")
def api_online_image_proxy():
    image_url = request.args.get("url", "").strip()
    referer = request.args.get("referer", "").strip()

    if not image_url:
        return jsonify({"error": "url is required"}), 400
    if not _is_safe_remote_url(image_url):
        return jsonify({"error": "unsafe image url"}), 400

    resp, warn = _fetch_image_with_fallbacks(image_url=image_url, referer=referer)
    if resp is None:
        direct_url = _pick_direct_redirect_url(image_url)
        if direct_url:
            out = redirect(direct_url, code=302)
            if warn:
                out.headers["X-PicCrawler-Proxy-Warn"] = warn[:200]
            out.headers["X-PicCrawler-Proxy-Fallback"] = "direct-redirect"
            # 远端图片内容不变，允许浏览器/局域网客户端缓存一整天，
            # 翻回上一页或重开查看器时无需再次经过代理回退链。
            out.headers["Cache-Control"] = "public, max-age=86400"
            return out
        return jsonify({"error": f"image proxy failed: {warn}"}), 502

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    out_headers: dict[str, str] = {
        "Cache-Control": "public, max-age=86400",
    }
    if warn:
        out_headers["X-PicCrawler-Proxy-Warn"] = warn[:200]

    def _generate():
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(_generate(), content_type=content_type, headers=out_headers)
