"""hitomi 图片 URL 行为测试：变体选择与共享候选回退链。"""
from __future__ import annotations

from app.services.crawler_hitomi import CrawlerHitomi
from app.services.hitomi_urls import hitomi_candidate_urls
from app.services.image_proxy import _candidate_fetch_urls


def _make_crawler() -> CrawlerHitomi:
    # 不经过 __init__（避免网络会话构建），只测纯函数。
    return CrawlerHitomi.__new__(CrawlerHitomi)


def test_variant_prefers_flags_over_filename():
    crawler = _make_crawler()
    # name 是 .webp 但只有 avif：必须按 hasavif 生成 avif URL，
    # 否则整本画廊的图片全量 404（hitomi 很多画廊只有 AVIF）。
    assert crawler._pick_image_variant({"name": "03.webp", "hasavif": 1}) == ("avif", "avif")
    assert crawler._pick_image_variant({"name": "03.webp", "haswebp": 1, "hasavif": 1}) == ("webp", "webp")
    assert crawler._pick_image_variant({"name": "03.jpg", "haswebp": 1}) == ("webp", "webp")
    # 两个标志都没有：只可能存在原始位图，webp/avif 名一律回退 jpg。
    assert crawler._pick_image_variant({"name": "03.webp"}) == ("jpg", "jpg")
    assert crawler._pick_image_variant({"name": "04.png"}) == ("png", "png")
    assert crawler._pick_image_variant({"name": "05"}) == ("jpg", "jpg")


HASH = "bb1e88a3bc2d5e56cc064ca8185b5a3577c767b71146c927b2c3b09a62fb58c0"


def test_hitomi_candidates_swap_extension_and_host():
    url = f"https://w1.gold-usergeneratedcontent.net/1789142401/140/{HASH}.webp"
    cands = hitomi_candidate_urls(url)
    assert url in cands
    # webp 不存在时回退 avif
    assert f"https://w2.gold-usergeneratedcontent.net/1789142401/140/{HASH}.webp" in cands
    assert any(c.endswith(f"/{HASH}.avif") for c in cands)
    # 原始位图路径兜底
    assert any(f"/images/1789142401/140/{HASH}.jpg" in c for c in cands)
    # 无重复
    assert len(cands) == len(set(cands))


def test_hitomi_candidates_ignore_other_hosts():
    assert hitomi_candidate_urls("https://example.com/a/b/1.webp") == []
    assert hitomi_candidate_urls("https://w1.hitomi.la/1789142401/140/x.webp") == []


def test_image_proxy_candidates_include_hitomi_fallbacks():
    url = f"https://w1.gold-usergeneratedcontent.net/1789142401/140/{HASH}.webp"
    cands = _candidate_fetch_urls(url)
    # 主 URL 之后应出现 avif 互换候选，浏览 404 时可自愈
    assert any(c.endswith(f"/{HASH}.avif") for c in cands)
