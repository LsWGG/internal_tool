"""数据源适配器协议：管理器与各数据源模块之间唯一的边界。

适配器不 import 管理器 —— 任务状态、网络、进度上报都从 DiscoverContext 取，
所以每个数据源能独立成模块，也不会形成循环导入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ManagerHttpBridge:
    """把适配器的网络请求接回管理器的 `_request`。

    管理器的 `_request` 本身就是 HTTP 客户端的薄委托，统一走它有两个好处：
    测试里 `manager._request = MagicMock(...)` 仍能拦住适配器发出的请求；
    限流、重试、代理轮换始终只有一个咽喉口。
    """

    def __init__(self, manager):
        self._manager = manager

    def get(self, url, proxies=(), **options):
        return self._manager._request(url, proxies, **options)


@dataclass
class DiscoverContext:
    """发现阶段适配器能看到的管理器能力。"""
    request: dict
    http: Any
    # 任务目录，供需要落缓存的源（微信索引 30 分钟复用）自己读写。
    data_dir: Any
    update: Callable
    checkpoint: Callable
    # 取页（含 Playwright 动态页与降级）由管理器统一提供，适配器不自己拉起浏览器：
    # fetch_html 是通用取页，fetch_listing_html 是新闻那套带滚动的列表页取页。
    fetch_html: Any
    fetch_listing_html: Any
    browser: Any = None
    task_id: str | None = None


@dataclass(frozen=True)
class SourceAdapter:
    name: str
    label: str
    modes: tuple[str, ...] = ()
    supports_video: bool = False
    requires_keyword: bool = False
    # 填了 discover 就由适配器自己发现（签名 value, ctx）；留空的数据源没有发现阶段。
    discover: Callable[[str, DiscoverContext], list] | None = None

    def validate(self, request: dict) -> None:
        mode = request.get(f"{self.name}_mode")
        if self.modes and mode not in self.modes:
            raise ValueError(f"{self.label}暂不支持该采集模式")
