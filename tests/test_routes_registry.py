"""蓝图拆分后，所有端点路径与方法必须与原始单体 routes.py 完全一致。"""
from __future__ import annotations


EXPECTED = [
    ("GET", "/"),
    ("POST", "/api/download"),
    ("POST", "/api/download/jobs/<job_id>/cancel"),
    ("GET", "/api/download/jobs"),
    ("GET", "/api/library/image/<int:image_id>"),
    ("GET", "/api/library/topic-cover/<int:topic_id>"),
    ("GET", "/api/online/image-proxy"),
    ("GET", "/api/online/topic-count"),
    ("GET", "/api/online/topic-images"),
    ("GET", "/api/online/topics"),
    ("GET", "/api/rules"),
    ("POST", "/api/rules/<rule_id>/download_dir"),
    ("POST", "/api/rules/<rule_id>/enabled"),
    ("GET", "/api/shelves"),
    ("POST", "/api/shelves"),
    ("DELETE", "/api/shelves/<int:shelf_id>"),
    ("POST", "/api/shelves/<int:shelf_id>/refresh"),
    ("GET", "/api/shelves/<int:shelf_id>/topics"),
    ("GET", "/api/shelves/topic/<int:topic_id>/images"),
    ("GET", "/api/system/directories"),
    ("POST", "/api/system/select-folder"),
]


def test_all_expected_routes_registered(app):
    by_rule: dict[str, set[str]] = {}
    for r in app.url_map.iter_rules():
        by_rule.setdefault(r.rule, set()).update(r.methods - {"HEAD", "OPTIONS"})

    for method, rule in EXPECTED:
        assert rule in by_rule, f"缺失路由: {rule}"
        assert method in by_rule[rule], f"路由 {rule} 缺少方法 {method}（实际: {sorted(by_rule[rule])}）"


def test_no_duplicate_url_rules(app):
    """蓝图拆分不应产生重复 URL 注册。"""
    seen: set[tuple[str, str]] = set()
    for r in app.url_map.iter_rules():
        for m in r.methods - {"HEAD", "OPTIONS"}:
            key = (m, r.rule)
            assert key not in seen, f"重复路由: {key}"
            seen.add(key)
