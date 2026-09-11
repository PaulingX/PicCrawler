"""规则能力单一来源测试：_CRAWLER_CAPABILITIES 必须直接由 crawler 类属性派生。"""
from __future__ import annotations

from app.services.rule_registry import _CRAWLER_CAPABILITIES, _CRAWLER_MAP


def test_capabilities_derived_from_classes():
    for name, cls in _CRAWLER_MAP.items():
        expected = {
            "supports_search": int(getattr(cls, "supports_search", False)),
            "categories": list(getattr(cls, "categories", [])),
        }
        assert _CRAWLER_CAPABILITIES[name] == expected, f"{name} 能力未与类属性同步"


def test_every_crawler_declares_capabilities():
    for name, cls in _CRAWLER_MAP.items():
        assert hasattr(cls, "supports_search"), f"{name} 缺少 supports_search"
        assert hasattr(cls, "categories"), f"{name} 缺少 categories"


def test_every_crawler_can_be_instantiated():
    """实例化全部注册爬虫：__init__ 只建会话、不发网络请求，
    能当场暴露缺导入/未定义名称等装配错误（如 hitomi 忘导 make_session）。"""
    for name, cls in _CRAWLER_MAP.items():
        instance = cls()
        assert getattr(instance, "session", None) is not None, f"{name} 未初始化 session"
