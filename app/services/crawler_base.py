from __future__ import annotations

from abc import ABC, abstractmethod


class BaseCrawler(ABC):
    @abstractmethod
    def list_topics(self, page_no: int, query: str = "") -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def topic_images(self, detail_url: str) -> list[str]:
        raise NotImplementedError

