"""image_proxy 纯函数回归测试（与 baseline_snapshot 同源，迁移到 pytest 形式）。"""
from __future__ import annotations

import app.services.image_proxy as ip


def test_normalize_wp_to_4khd_pic():
    assert (
        ip._normalize_remote_image_url("https://i0.wp.com/pic.4khd.com/2021/01/abc.jpg")
        == "https://img.uuss.uk/2021/01/abc.jpg"
    )


def test_normalize_pic_4khd():
    assert (
        ip._normalize_remote_image_url("https://pic.4khd.com/x/y.jpg")
        == "https://img.uuss.uk/x/y.jpg"
    )


def test_normalize_img_4khd():
    assert (
        ip._normalize_remote_image_url("https://img.4khd.com/x/y.jpg")
        == "https://img.uuss.uk/x/y.jpg"
    )


def test_normalize_wp_other_keeps_wrapper():
    # 非 4khd 来源不再解包：imgbox 直链已返回占位图，Photon 包装仍可取到真图。
    assert (
        ip._normalize_remote_image_url("https://i0.wp.com/other.com/x.jpg")
        == "https://i0.wp.com/other.com/x.jpg"
    )


def test_normalize_imgbox_wraps_to_photon():
    assert (
        ip._normalize_remote_image_url("https://images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg")
        == "https://i1.wp.com/images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg"
    )
    assert (
        ip._normalize_remote_image_url("https://images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg?x=1")
        == "https://i1.wp.com/images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg?x=1"
    )


def test_normalize_wp_imgbox_keeps_wrapper():
    assert (
        ip._normalize_remote_image_url("https://i1.wp.com/images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg")
        == "https://i1.wp.com/images2.imgbox.com/2c/e6/EKj0WMmE_o.jpg"
    )


def test_normalize_passthrough():
    assert ip._normalize_remote_image_url("https://example.com/a.jpg") == "https://example.com/a.jpg"


def test_normalize_empty():
    assert ip._normalize_remote_image_url("") == ""


def test_wrap_pic_4khd():
    assert (
        ip._wrap_wp_proxy_url("https://pic.4khd.com/x/y.jpg")
        == "https://i0.wp.com/pic.4khd.com/x/y.jpg"
    )


def test_wrap_passthrough_wp():
    assert (
        ip._wrap_wp_proxy_url("https://i0.wp.com/x/y.jpg")
        == "https://i0.wp.com/x/y.jpg"
    )


def test_wrap_passthrough_other():
    assert (
        ip._wrap_wp_proxy_url("https://example.com/x/y.jpg")
        == "https://example.com/x/y.jpg"
    )


def test_prefer_passthrough_pic_4khd():
    assert (
        ip._prefer_display_image_url("https://pic.4khd.com/x/y.jpg")
        == "https://pic.4khd.com/x/y.jpg"
    )


def test_proxy_with_referer():
    assert (
        ip._proxy_remote_image_url("https://example.com/a.jpg", "https://e.com/d")
        == "/api/online/image-proxy?url=https%3A%2F%2Fexample.com%2Fa.jpg&referer=https%3A%2F%2Fe.com%2Fd"
    )


def test_proxy_already_proxied_passthrough():
    assert (
        ip._proxy_remote_image_url("/api/online/image-proxy?url=x")
        == "/api/online/image-proxy?url=x"
    )


def test_proxy_empty():
    assert ip._proxy_remote_image_url("") == ""


def test_is_displayable():
    assert ip._is_displayable_image_url("https://x.com/a.jpg") is True
    assert ip._is_displayable_image_url("data:image/png;base64,xxx") is False
    assert ip._is_displayable_image_url("https://x.com/a.jpg?b=base64,xxx") is False
    assert ip._is_displayable_image_url("ftp://x.com/a.jpg") is False


def test_low_quality_gallery():
    assert ip._is_low_quality_gallery_url("hotgirl", "https://hotgirl.asia/wp-content/themes/x.jpg") is True
    assert ip._is_low_quality_gallery_url("hotgirl", "https://hotgirl.asia/x-300x200.jpg") is True
    assert ip._is_low_quality_gallery_url("hotgirl", "https://hotgirl.asia/real/photo.jpg") is False
    assert ip._is_low_quality_gallery_url("wnacg", "https://www.wnacg.com/data/t/a/b/1.jpg") is True
    assert ip._is_low_quality_gallery_url("wnacg", "https://www.wnacg.com/data/a/b/1.jpg") is False
    assert ip._is_low_quality_gallery_url("4khd", "https://pic.4khd.com/x.jpg") is False


def test_cached_images_need_refresh():
    assert ip._cached_images_need_refresh("hitomi-chinese", ["https://x/1.jpg"]) is True
    assert ip._cached_images_need_refresh("wnacg", ["https://www.wnacg.com/data/a/b/1.jpg"]) is False
    assert ip._cached_images_need_refresh("wnacg", ["https://www.wnacg.com/data/t/a/b/1.jpg"]) is True
    assert ip._cached_images_need_refresh("4khd", ["https://pic.4khd.com/x.jpg"]) is False
    assert ip._cached_images_need_refresh("wnacg", []) is False


def test_is_safe_remote_url():
    assert ip._is_safe_remote_url("https://example.com/a.jpg") is True
    assert ip._is_safe_remote_url("http://localhost/a.jpg") is False
    assert ip._is_safe_remote_url("http://127.0.0.1/a.jpg") is False
    assert ip._is_safe_remote_url("ftp://x.com/a.jpg") is False


def test_candidate_fetch_urls_set():
    cands = set(ip._candidate_fetch_urls("https://pic.4khd.com/p.jpg"))
    assert cands == {
        "https://img.uuss.uk/p.jpg",
        "https://pic.4khd.com/p.jpg",
        "http://img.uuss.uk/p.jpg",
        "http://pic.4khd.com/p.jpg",
    }


def test_pick_direct_redirect_url():
    assert (
        ip._pick_direct_redirect_url("https://pic.4khd.com/p.jpg")
        == "https://img.uuss.uk/p.jpg"
    )
