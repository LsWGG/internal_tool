"""统一的数据源适配器协议。

适配器只负责描述数据源能力与输入校验；具体抓取实现暂由
CrawlerTaskManager 保留，逐步迁移时无需改变任务/API 格式。
"""
from dataclasses import dataclass
from typing import Callable, Any

@dataclass(frozen=True)
class SourceAdapter:
    name: str
    label: str
    modes: tuple[str, ...] = ()
    supports_video: bool = False
    requires_keyword: bool = False

    def validate(self, request: dict) -> None:
        mode = request.get(f"{self.name}_mode")
        if self.modes and mode not in self.modes:
            raise ValueError(f"{self.label}暂不支持该采集模式")

ADAPTERS = {
    "generic": SourceAdapter("generic", "普通网页"),
    "news": SourceAdapter("news", "新闻网站"),
    "wechat": SourceAdapter("wechat", "微信公众号"),
    "youtube": SourceAdapter("youtube", "YouTube", supports_video=True, requires_keyword=True),
    "tiktok": SourceAdapter("tiktok", "TikTok", ("keyword", "comments", "user", "videos"), True, True),
    "douyin": SourceAdapter("douyin", "抖音", ("keyword", "comments", "user", "videos"), True, True),
    "twitter": SourceAdapter("twitter", "X / Twitter", ("keyword", "user"), requires_keyword=True),
    "telegram": SourceAdapter("telegram", "Telegram", ("channel", "group", "members", "search"), requires_keyword=True),
}

def get_adapter(source: str) -> SourceAdapter:
    return ADAPTERS.get(source, ADAPTERS["generic"])
