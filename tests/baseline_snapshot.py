"""基线快照：在重构前对 routes.py / rule_registry / download_worker 中的纯函数做行为快照。

运行方式（依赖安装完成后）：
    .venv_dev/Scripts/python.exe tests/baseline_snapshot.py

期望输出全部 "PASS"。本文件只读当前代码，不做任何修改，用于"改前"行为锚定。
重构后由 tests/test_*.py 用相同预期值回归。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 让项目根目录在 sys.path 中，便于直接导入 app 包。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 在任何 import create_app 之前，把数据目录重定向到临时目录，避免触碰真实 data/。
import app.config as cfg  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="piccrawler_baseline_"))
cfg.DB_PATH = _TMP / "piccrawler.db"
cfg.DOWNLOAD_ROOT = _TMP / "downloads"

import app.routes as routes  # noqa: E402
from app import create_app  # noqa: E402
from app.services.rule_registry import list_rules, _CRAWLER_CAPABILITIES, _CRAWLER_MAP  # noqa: E402
from app.services.download_worker import (  # noqa: E402
    _normalize_download_image_url,
    _candidate_download_urls,
)

app = create_app()
ctx = app.app_context()
ctx.push()

CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


# ---- _normalize_remote_image_url ----
check(
    "normalize wp->4khd pic",
    routes._normalize_remote_image_url("https://i0.wp.com/pic.4khd.com/2021/01/abc.jpg")
    == "https://img.uuss.uk/2021/01/abc.jpg",
)
check(
    "normalize pic.4khd",
    routes._normalize_remote_image_url("https://pic.4khd.com/x/y.jpg")
    == "https://img.uuss.uk/x/y.jpg",
)
check(
    "normalize img.4khd",
    routes._normalize_remote_image_url("https://img.4khd.com/x/y.jpg")
    == "https://img.uuss.uk/x/y.jpg",
)
check(
    "normalize wp->other",
    routes._normalize_remote_image_url("https://i0.wp.com/other.com/x.jpg")
    == "https://other.com/x.jpg",
)
check(
    "normalize passthrough",
    routes._normalize_remote_image_url("https://example.com/a.jpg")
    == "https://example.com/a.jpg",
)
check("normalize empty", routes._normalize_remote_image_url("") == "")

# ---- _wrap_wp_proxy_url ----
check(
    "wrap pic.4khd",
    routes._wrap_wp_proxy_url("https://pic.4khd.com/x/y.jpg")
    == "https://i0.wp.com/pic.4khd.com/x/y.jpg",
)
check(
    "wrap passthrough wp",
    routes._wrap_wp_proxy_url("https://i0.wp.com/x/y.jpg")
    == "https://i0.wp.com/x/y.jpg",
)
check(
    "wrap passthrough other",
    routes._wrap_wp_proxy_url("https://example.com/x/y.jpg")
    == "https://example.com/x/y.jpg",
)

# ---- _prefer_display_image_url ----
check(
    "prefer passthrough pic.4khd",
    routes._prefer_display_image_url("https://pic.4khd.com/x/y.jpg")
    == "https://pic.4khd.com/x/y.jpg",
)

# ---- _proxy_remote_image_url ----
check(
    "proxy with referer",
    routes._proxy_remote_image_url("https://example.com/a.jpg", "https://e.com/d")
    == "/api/online/image-proxy?url=https%3A%2F%2Fexample.com%2Fa.jpg&referer=https%3A%2F%2Fe.com%2Fd",
)
check(
    "proxy already proxied passthrough",
    routes._proxy_remote_image_url("/api/online/image-proxy?url=x")
    == "/api/online/image-proxy?url=x",
)
check("proxy empty", routes._proxy_remote_image_url("") == "")

# ---- _is_displayable_image_url ----
check("displayable ok", routes._is_displayable_image_url("https://x.com/a.jpg") is True)
check("displayable data", routes._is_displayable_image_url("data:image/png;base64,xxx") is False)
check("displayable base64", routes._is_displayable_image_url("https://x.com/a.jpg?b=base64,xxx") is False)
check("displayable ftp", routes._is_displayable_image_url("ftp://x.com/a.jpg") is False)

# ---- _is_low_quality_gallery_url ----
check("lq hotgirl theme", routes._is_low_quality_gallery_url("hotgirl", "https://hotgirl.asia/wp-content/themes/x.jpg") is True)
check("lq hotgirl resized", routes._is_low_quality_gallery_url("hotgirl", "https://hotgirl.asia/x-300x200.jpg") is True)
check("lq hotgirl real", routes._is_low_quality_gallery_url("hotgirl", "https://hotgirl.asia/real/photo.jpg") is False)
check("lq wnacg data/t", routes._is_low_quality_gallery_url("wnacg", "https://www.wnacg.com/data/t/a/b/1.jpg") is True)
check("lq wnacg clean", routes._is_low_quality_gallery_url("wnacg", "https://www.wnacg.com/data/a/b/1.jpg") is False)
check("lq 4khd not", routes._is_low_quality_gallery_url("4khd", "https://pic.4khd.com/x.jpg") is False)

# ---- _cached_images_need_refresh ----
check("refresh hitomi", routes._cached_images_need_refresh("hitomi-chinese", ["https://x/1.jpg"]) is True)
check("refresh wnacg clean", routes._cached_images_need_refresh("wnacg", ["https://www.wnacg.com/data/a/b/1.jpg"]) is False)
check("refresh wnacg lq", routes._cached_images_need_refresh("wnacg", ["https://www.wnacg.com/data/t/a/b/1.jpg"]) is True)
check("refresh 4khd", routes._cached_images_need_refresh("4khd", ["https://pic.4khd.com/x.jpg"]) is False)
check("refresh empty", routes._cached_images_need_refresh("wnacg", []) is False)

# ---- _is_safe_remote_url ----
check("safe ok", routes._is_safe_remote_url("https://example.com/a.jpg") is True)
check("safe localhost", routes._is_safe_remote_url("http://localhost/a.jpg") is False)
check("safe 127", routes._is_safe_remote_url("http://127.0.0.1/a.jpg") is False)
check("safe ftp", routes._is_safe_remote_url("ftp://x.com/a.jpg") is False)

# ---- _candidate_fetch_urls ----
cands = routes._candidate_fetch_urls("https://pic.4khd.com/p.jpg")
check(
    "candidates set",
    set(cands)
    == {
        "https://img.uuss.uk/p.jpg",
        "https://pic.4khd.com/p.jpg",
        "http://img.uuss.uk/p.jpg",
        "http://pic.4khd.com/p.jpg",
    },
)

# ---- _pick_direct_redirect_url ----
check(
    "pick redirect",
    routes._pick_direct_redirect_url("https://pic.4khd.com/p.jpg")
    == "https://img.uuss.uk/p.jpg",
)

# ---- download_worker normalization ----
check(
    "dl normalize wp->pic",
    _normalize_download_image_url("https://i0.wp.com/pic.4khd.com/x/y.jpg")
    == "https://img.4khd.com/x/y.jpg",
)
check(
    "dl normalize pic",
    _normalize_download_image_url("https://pic.4khd.com/x/y.jpg")
    == "https://img.4khd.com/x/y.jpg",
)
dl_cands = _candidate_download_urls("https://img.4khd.com/x/y.jpg")
check("dl candidates non-empty", len(dl_cands) > 0)

# ---- rule_registry capabilities (DB-backed) ----
rules = list_rules(include_disabled=True)
rule_map = {r["rule_id"]: r for r in rules}
check("rules non-empty", len(rules) > 0)
check("wnacg supports_search", bool(rule_map["wnacg"].get("supports_search")))
check("wnacg has categories", bool(rule_map["wnacg"].get("categories")))
check("4khd supported search", bool(rule_map["4khd"].get("supports_search")))

# capability map consistency checks (no DB needed)
check("capabilities map covers all crawlers", set(_CRAWLER_CAPABILITIES) == set(_CRAWLER_MAP))
check("capabilities has wnacg categories", _CRAWLER_CAPABILITIES["crawler_wnacg"]["categories"] == [1, 9, 10, 20])


def main() -> int:
    failed = [name for name, ok in CHECKS if not ok]
    print("\n" + "=" * 40)
    print(f"TOTAL {len(CHECKS)}  PASS {len(CHECKS) - len(failed)}  FAIL {len(failed)}")
    if failed:
        print("FAILED:")
        for f in failed:
            print("  -", f)
        return 1
    print("ALL BASELINE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
