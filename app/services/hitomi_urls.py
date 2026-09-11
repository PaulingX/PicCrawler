"""hitomi 图片 URL 的候选回退生成（浏览代理与下载器共用）。

hitomi 的图片可用性由 per-file 的 haswebp / hasavif 标志决定，且历史上
多次更换路径前缀（gg.b 轮换）与子域规则。此处基于一个已知 URL 推导出
等价候选（扩展名互换、子域互换、原始位图路径），供抓取失败时逐个回退，
避免单一 URL 404 导致整页图片加载失败。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

HITOMI_IMAGE_HOSTS = {
    "gold-usergeneratedcontent.net",
    "ltn.gold-usergeneratedcontent.net",
    "a.gold-usergeneratedcontent.net",
    "a1.gold-usergeneratedcontent.net",
    "a2.gold-usergeneratedcontent.net",
    "w1.gold-usergeneratedcontent.net",
    "w2.gold-usergeneratedcontent.net",
    "1.gold-usergeneratedcontent.net",
    "2.gold-usergeneratedcontent.net",
    "atn.gold-usergeneratedcontent.net",
    "btn.gold-usergeneratedcontent.net",
    "tn.hitomi.la",
}


def hitomi_candidate_urls(url: str) -> list[str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host not in HITOMI_IMAGE_HOSTS:
        return []

    candidates: list[str] = []

    def _push(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _push(url)

    alt_hosts: list[str] = []
    if host.startswith("atn."):
        alt_hosts.append("btn." + host[4:])
    elif host.startswith("btn."):
        alt_hosts.append("atn." + host[4:])
    elif host.startswith("a1."):
        alt_hosts.append("a2." + host[3:])
    elif host.startswith("a2."):
        alt_hosts.append("a1." + host[3:])
    elif host.startswith("w1."):
        alt_hosts.append("w2." + host[3:])
    elif host.startswith("w2."):
        alt_hosts.append("w1." + host[3:])
    elif host.startswith("1."):
        alt_hosts.append("2." + host[2:])
    elif host.startswith("2."):
        alt_hosts.append("1." + host[2:])
    elif host == "a.gold-usergeneratedcontent.net":
        alt_hosts.extend(["a1.gold-usergeneratedcontent.net", "a2.gold-usergeneratedcontent.net"])

    for alt_host in alt_hosts:
        netloc = alt_host if parsed.port is None else f"{alt_host}:{parsed.port}"
        _push(parsed._replace(netloc=netloc).geturl())

    # webp / avif 互换：可用性标志可能缺失或过期，另一种编码往往存在。
    m = re.match(r"^/([^/]+/\d+/[0-9a-f]{64})\.(avif|webp)$", path, re.IGNORECASE)
    if m:
        stem = m.group(1)
        ext = m.group(2).lower()
        if ext == "avif":
            _push(parsed._replace(path=f"/{stem}.webp").geturl())
        else:
            _push(parsed._replace(path=f"/{stem}.avif").geturl())
        for image_ext in ("jpg", "jpeg", "png", "gif"):
            _push(parsed._replace(path=f"/images/{stem}.{image_ext}").geturl())

    # gg.js 分段算法的等价分段（s 值的其他位组合），历史上路径分段变化时兜底。
    m = re.match(r"^/(images/)?([^/]+)/(\d+)/([0-9a-f]{64})\.(\w+)$", path, re.IGNORECASE)
    if m:
        prefix = m.group(1) or ""
        b_value = m.group(2)
        current_seg = m.group(3)
        hash_value = m.group(4).lower()
        ext = m.group(5)

        segment_values = {
            str(int(hash_value[-3:], 16)),
            str(int(hash_value[-1] + hash_value[-3:-1], 16)),
            str(int(hash_value[-2:], 16)),
        }
        segment_values.discard(current_seg)
        for seg in segment_values:
            _push(parsed._replace(path=f"/{prefix}{b_value}/{seg}/{hash_value}.{ext}").geturl())

    return candidates
