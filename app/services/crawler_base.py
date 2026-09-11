from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import requests

try:
    import cloudscraper
except Exception:  # noqa: BLE001
    cloudscraper = None

from app.config import USER_AGENT

_LOGGER = logging.getLogger("app.crawlers")

# Cloudflare / 反爬挑战页的典型特征，用于判断裸请求是否拿到了挑战页而非真实内容。
# 注意：/cdn-cgi/challenge-platform/... 埋点脚本会注入所有经过 Cloudflare 的正常页面，
# 不能单独作为拦截依据（见 looks_like_challenge 的页面长度启发式）。
_CHALLENGE_MARKERS = (
    "cf-chl",
    "cf-mitigated",
    "checking your browser",
    "challenge-platform",
    "enable javascript and cookies to continue",
    "just a moment",
    "ddos-guard",
    "captcha-delivery",
)

# 真正的挑战页只有几 KB 的跳转脚本；正常内容页即使带 CF 埋点也远大于该阈值。
_CHALLENGE_PAGE_MAX_BYTES = 4096


def make_session(proxy: str = "") -> requests.Session:
    """创建带 Cloudflare 绕过的 requests 兼容 session。

    优先使用 cloudscraper（自动解决 CF 挑战）；不可用时降级为普通 requests。
    返回值始终是 requests.Session 子类，.get / .headers / .proxies / .cookies 均可正常用。
    """
    proxy = str(proxy or "").strip()
    if cloudscraper is not None:
        try:
            sess = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            sess.headers.setdefault("User-Agent", USER_AGENT)
            if proxy:
                sess.proxies.update({"http": proxy, "https": proxy})
            return sess
        except Exception:  # noqa: BLE001
            _LOGGER.warning("cloudscraper 创建失败，降级为普通 requests")

    sess = requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})
    return sess


def looks_like_challenge(text: str, status_code: int) -> bool:
    if status_code in (403, 503, 429):
        return True
    low = (text or "").lower()
    if not any(marker in low for marker in _CHALLENGE_MARKERS):
        return False
    # challenge-platform 等标记也出现在正常页面的 CF 埋点脚本里。
    # 只有页面本身很小（几乎没有正文）时才认定为真正的拦截页。
    return len(low) <= _CHALLENGE_PAGE_MAX_BYTES


class BaseCrawler(ABC):
    # 能力声明：单一事实来源，rule_registry 据此派生 _CRAWLER_CAPABILITIES，
    # 不再维护与 crawler 类平行的字典，避免双轨失同步。
    supports_search: bool = False
    categories: list[int] = []

    @abstractmethod
    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def topic_images(self, detail_url: str) -> list[str]:
        raise NotImplementedError

    def _request_page(self, url: str, timeout: float = 25, headers: dict | None = None) -> tuple[int, str]:
        """用 Cloudflare 绕过 session 取页面，并记录诊断信息。返回 (status_code, text)。"""
        if not hasattr(self, "_last_status"):
            self._last_status = 0
            self._last_challenge = False
            self._last_error = ""

        self._last_status = 0
        self._last_challenge = False
        self._last_error = ""
        try:
            res = self.session.get(url, timeout=timeout, headers=headers or None)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            _LOGGER.warning("请求失败 %s: %s", url, exc)
            return 0, ""

        status = int(res.status_code or 0)
        text = res.text or ""
        self._last_status = status
        self._last_error = ""
        if looks_like_challenge(text, status):
            self._last_challenge = True
            _LOGGER.warning("疑似挑战页(被拦截) %s status=%s", url, status)
            return status, ""
        return status, text
