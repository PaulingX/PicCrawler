"""asmhentai 爬虫：Cloudflare 兜底 + 挑战页检测 + 解析一致性。"""
from __future__ import annotations

from app import create_app
from app.services.crawler_base import looks_like_challenge
from app.services.crawler_asmhentai import CrawlerAsmhentai


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text
        self.cookies = type("C", (), {"update": lambda *a, **k: None})()


class _FakeSession:
    def __init__(self, payload) -> None:
        # payload: (status, text) 或 Callable(url, **kw) -> (status, text)
        self._payload = payload
        self.headers = {}
        self.proxies = {}
        self.cookies = type("C", (), {"update": lambda *a, **k: None})()

    def get(self, url, timeout=25, headers=None):
        payload = self._payload
        if callable(payload):
            status, text = payload(url)
        else:
            status, text = payload
        return _FakeResponse(status, text)


def _make_crawler() -> CrawlerAsmhentai:
    # 不经过 __init__ 的真实网络/会话构建，直接装配可预测的假会话。
    crawler = CrawlerAsmhentai.__new__(CrawlerAsmhentai)
    crawler.session = _FakeSession((200, "<html></html>"))
    return crawler


def test_looks_like_challenge_markers():
    assert looks_like_challenge("<html><title>Just a moment...</title></html>", 403)
    assert looks_like_challenge("<html>Checking your browser before accessing</html>", 503)
    assert looks_like_challenge("", 200) is False
    assert looks_like_challenge("<html>real</html>", 200) is False


def test_looks_like_challenge_allows_cf_beacon_on_real_page():
    # 正常内容页即使带有 challenge-platform 埋点（所有 CF 站点都会注入），
    # 只要正文足够大就不应误判为拦截页——否则 4khd 等站点永远解析为空。
    real_page = (
        "<html><body>"
        + "<article><h2><a href='/content/1/x.html'>t</a></h2></article>" * 100
        + "<script>/cdn-cgi/challenge-platform/scripts/jsd/main.js</script>"
        "</body></html>"
    )
    assert len(real_page) > 4096
    assert looks_like_challenge(real_page, 200) is False

    # 小体积、几乎全是挑战脚本的页面仍应判为拦截页。
    tiny = "<html><script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page'></script></html>"
    assert looks_like_challenge(tiny, 200) is True


def test_request_page_detects_challenge():
    crawler = _make_crawler()
    crawler.session = _FakeSession((503, "<html>Just a moment...</html>"))

    status, html = crawler._request_page("https://asmhentai.com/", timeout=5)
    assert status == 503
    assert html == ""
    assert crawler._last_challenge is True


def test_request_page_uses_real_html_directly():
    crawler = _make_crawler()
    real_html = (
        200,
        '<div class="preview_item"><div class="image"><a href="/g/9/"></a></div>'
        '<div class="cpt"><h2 class="caption">B</h2></div></div>',
    )
    crawler.session = _FakeSession(real_html)

    status, html = crawler._request_page("https://asmhentai.com/", timeout=5)
    assert status == 200
    topics = crawler._parse_topics_from_html(html)
    assert topics[0]["title"] == "B"


def test_parser_returns_topics_with_cover():
    crawler = _make_crawler()
    html = """
    <div class="preview_item">
      <div class="image"><a href="/g/123456/"><img src="https://cdn.asmhentai.com/thumb/123456/t.jpg" alt="主题A"></a></div>
      <div class="cpt"><h2 class="caption"><a href="/g/123456/">主题A</a></h2></div>
    </div>
    """
    topics = crawler._parse_topics_from_html(html)
    assert len(topics) == 1
    assert topics[0]["title"] == "主题A"
    assert topics[0]["detail_url"] == "https://asmhentai.com/g/123456/"
    assert topics[0]["cover_url"] == "https://cdn.asmhentai.com/thumb/123456/t.jpg"


def test_list_topics_integration_shape():
    """通过 app 上下文构建真实 crawler，确认 list_topics 接口形态（离线 mock 网络）。"""
    with create_app().app_context():
        crawler = CrawlerAsmhentai()
        # 用假会话替换真实网络，验证 list_topics 能走到解析并返回结构化 dict
        real_html = (
            '<div class="preview_item"><div class="image"><a href="/g/7/"></a></div>'
            '<div class="cpt"><h2 class="caption">集成主题</h2></div></div>'
        )
        crawler.session = _FakeSession((200, real_html))

        topics = crawler.list_topics(1, query="")
        assert len(topics) == 1
        t = topics[0]
        assert set(["topic_id", "title", "cover_url", "detail_url"]).issubset(t.keys())
        assert t["title"] == "集成主题"
