"""采集字段表。

字段表要被任务管理器和各数据源适配器同时引用。留在 crawler_manager 里会让
适配器反向 import 管理器而形成循环导入，所以单独成模块。内容未作任何修改。
"""
from __future__ import annotations


BUILTIN_FIELDS = {
    "generic": ["title", "description", "author", "published_at", "content", "image", "url"],
    "news": ["title", "description", "author", "published_at", "content", "image", "source", "url"],
    "twitter": ["text", "author", "published_at", "likes", "reposts", "replies", "media", "url"],
    "tiktok": ["title", "description", "author", "username", "published_at", "duration", "views", "likes", "comments", "shares", "thumbnail", "url"],
    "douyin": ["title", "description", "author", "username", "published_at", "duration", "views", "likes", "comments", "shares", "thumbnail", "url"],
    "telegram": ["message_id", "text", "author", "published_at", "views", "forwards", "replies", "media", "chat", "url"],
    "youtube": ["title", "description", "channel", "published_at", "duration", "views", "likes", "thumbnail", "url"],
    "wechat": ["title", "author", "published_at", "content", "image", "account", "url"],
}

TWITTER_USER_FIELDS = [
    "username", "display_name", "bio", "location", "followers", "following",
    "posts", "joined_at", "verified", "avatar", "url",
]

TIKTOK_USER_FIELDS = [
    "username", "display_name", "bio", "followers", "following", "videos",
    "likes", "verified", "avatar", "url",
]

TIKTOK_COMMENT_FIELDS = [
    "author", "username", "text", "published_at", "likes", "replies", "url",
]

YOUTUBE_USER_FIELDS = [
    "username", "display_name", "bio", "followers", "videos", "verified", "avatar", "url",
]

YOUTUBE_COMMENT_FIELDS = [
    "author", "username", "text", "published_at", "likes", "replies", "url",
]

TELEGRAM_MEMBER_FIELDS = [
    "user_id", "username", "display_name", "bio", "bot", "verified", "status", "url",
]
