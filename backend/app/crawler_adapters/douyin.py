"""抖音数据源：签名请求走 douyin_profiles，行构造与 yt-dlp 抽取复用 tiktok。

原先写在 CrawlerTaskManager._discover_douyin_data 里的分发整体搬到这里，逻辑未变。
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from ..douyin_profiles import aweme_comments, discover_profiles, search_awemes, user_awemes
from . import tiktok


def discover(value, ctx):
    request, task_id = ctx.request, ctx.task_id
    mode = str(request.get("tiktok_mode") or "keyword").lower()
    requested = int(request.get("max_items") or 50)
    maximum = 500 if requested < 0 else min(500, max(1, requested))
    if task_id:
        labels = {"keyword": "搜索关键词视频", "comments": "读取视频评论", "user": "读取账号信息", "videos": "读取账号视频"}
        ctx.update(task_id, status="running", message=f"正在抖音{labels.get(mode, '采集')}", progress=10)
    if mode == "user":
        return discover_profiles(value, request, browser=ctx.browser)
    if mode == "comments":
        video_url = tiktok.video_url(value)
        match = re.search(r"/video/(\d+)", video_url)
        if not video_url or not match:
            raise ValueError("请输入完整的抖音视频链接")
        rows = aweme_comments(match.group(1), video_url, request, maximum)
        if not rows:
            raise RuntimeError("这条视频没有可读取的公开评论；也可能视频已不可访问")
        return [{"url": row["url"], "detail_url": "", "prefill": row} for row in rows]
    if mode == "videos":
        profiles = discover_profiles(value, request, browser=ctx.browser)
        targets = []
        for profile in profiles:
            sec_uid = unquote(urlparse(profile["url"]).path.rsplit('/', 1)[-1])
            rows = user_awemes(sec_uid, request, maximum)
            for row in rows:
                targets.append({"url": row["url"], "detail_url": "", "prefill": row})
        if not targets:
            raise RuntimeError("这个账号没有可读取的公开视频；也可能主页链接有误")
        return targets[:maximum]
    direct = tiktok.video_url(value)
    if direct:
        try:
            row = tiktok.video_row(tiktok.extract(direct, request))
        except RuntimeError:
            row = {"url": direct}
        row["url"] = row.get("url") or direct
        return [{"url": direct, "detail_url": "", "prefill": row}]
    rows = search_awemes(str(value or "").strip(), request, maximum)
    if not rows:
        # 走到这里说明没被风控拦（拦了 search_awemes 会直接抛），是抖音真的没结果。
        raise RuntimeError("抖音没有搜到与这个关键词相关的视频，换个关键词试试")
    return [{"url": row["url"], "detail_url": "", "prefill": row} for row in rows]
