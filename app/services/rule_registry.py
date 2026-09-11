from __future__ import annotations

from app.database import query_all, query_one
from app.services.crawler_4khd import Crawler4KHD
from app.services.crawler_asmhentai import CrawlerAsmhentai
from app.services.crawler_hotgirl import CrawlerHotgirl
from app.services.crawler_hitomi import CrawlerHitomi
from app.services.crawler_wnacg import CrawlerWnacg
from app.services.crawler_youwu import CrawlerYouwu
from app.services.crawler_xchina import CrawlerXChina

_CRAWLER_MAP = {
    "crawler_4khd": Crawler4KHD,
    "crawler_asmhentai": CrawlerAsmhentai,
    "crawler_wnacg": CrawlerWnacg,
    "crawler_hitomi": CrawlerHitomi,
    "crawler_youwu": CrawlerYouwu,
    "crawler_hotgirl": CrawlerHotgirl,
    "crawler_xchina": CrawlerXChina,
}

# 能力字典由 crawler 类属性派生（单一事实来源），不再手工维护平行表。
# 校验：_CRAWLER_MAP 中的每个类都应声明 supports_search / categories。
def _derive_capabilities() -> dict[str, dict]:
    caps: dict[str, dict] = {}
    for name, cls in _CRAWLER_MAP.items():
        caps[name] = {
            "supports_search": int(getattr(cls, "supports_search", False)),
            "categories": list(getattr(cls, "categories", [])),
        }
    return caps


_CRAWLER_CAPABILITIES = _derive_capabilities()


def _with_capabilities(rule: dict) -> dict:
    rule = dict(rule)
    caps = _CRAWLER_CAPABILITIES.get(str(rule.get("crawler")), {})
    for key, value in caps.items():
        rule[key] = value
    return rule


def list_rules(include_disabled: bool = False) -> list[dict]:
    if include_disabled:
        rows = query_all(
            "SELECT rule_id, name, base_url, crawler, enabled FROM rules ORDER BY rule_id"
        )
    else:
        rows = query_all(
            """
            SELECT rule_id, name, base_url, crawler, enabled
            FROM rules
            WHERE enabled = 1
            ORDER BY rule_id
            """
        )
    return [_with_capabilities(dict(row)) for row in rows]


def get_rule(rule_id: str) -> dict | None:
    row = query_one(
        "SELECT rule_id, name, base_url, crawler, enabled FROM rules WHERE rule_id = ?",
        (rule_id,),
    )
    return _with_capabilities(dict(row)) if row else None


def build_crawler(rule_id: str):
    rule = get_rule(rule_id)
    if not rule:
        raise ValueError(f"Unknown rule: {rule_id}")

    crawler_name = rule["crawler"]
    crawler_cls = _CRAWLER_MAP.get(crawler_name)
    if crawler_cls is None:
        raise ValueError(f"Unsupported crawler: {crawler_name}")
    return crawler_cls()
