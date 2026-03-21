from __future__ import annotations

from app.services.crawler_wnacg import CrawlerWnacg


class CrawlerManxiangge(CrawlerWnacg):
    # Keep display value consistent with user expectation.
    base_url = "https://漫香阁.com/"

    # Put compatible fallback first to ensure online browsing is always available.
    base_candidates = (
        "https://www.wnacg.com/",
        "https://xn--qexm24f3mc.com/",
        "https://www.xn--qexm24f3mc.com/",
        "http://xn--wgv69rba1382b.com/",
        "http://www.xn--wgv69rba1382b.com/",
    )

    category_ids = (1, 9, 10)
    supports_search = True
    site_label = "manxiangge"
