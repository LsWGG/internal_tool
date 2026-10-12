"""统一的数据源适配器协议。

适配器描述数据源能力与输入校验；需要按关键词发现的源都在本包内实现 discover，
只有 generic 不填 —— 它采集用户直接给出的链接，没有发现阶段。
"""
from . import douyin, news, telegram, tiktok, twitter, wechat, youtube, ytdlp
from .base import SourceAdapter

ADAPTERS = {
    "generic": SourceAdapter("generic", "普通网页"),
    "news": SourceAdapter("news", "新闻网站", discover=news.discover),
    "wechat": SourceAdapter("wechat", "微信公众号", discover=wechat.discover),
    "youtube": SourceAdapter("youtube", "YouTube", ("keyword", "comments", "user", "videos"), True, True,
                             discover=youtube.discover),
    "tiktok": SourceAdapter("tiktok", "TikTok", ("keyword", "comments", "user", "videos"), True, True,
                             discover=tiktok.discover),
    "douyin": SourceAdapter("tiktok", "抖音", ("keyword", "comments", "user", "videos"), True, True,
                             discover=douyin.discover),
    "twitter": SourceAdapter("twitter", "X / Twitter", ("keyword", "user"), requires_keyword=True,
                              discover=twitter.posts),
    # 通用视频下载：任意 yt-dlp 支持的站点，输入链接而非关键词。
    "ytdlp": SourceAdapter("ytdlp", "yt-dlp 视频", ("videos", "playlist"), True,
                            discover=ytdlp.discover),
    "telegram": SourceAdapter("telegram", "Telegram", ("channel", "group", "members", "search"), requires_keyword=True,
                               discover=telegram.discover),
}

def get_adapter(source: str) -> SourceAdapter:
    return ADAPTERS.get(source, ADAPTERS["generic"])
