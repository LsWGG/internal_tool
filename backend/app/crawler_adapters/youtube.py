"""YouTube 数据源：关键词搜索、视频/账号/评论发现与页面字段补充。

原先散在 CrawlerTaskManager 上的同名方法整体搬到这里，逻辑未变；与管理器的接触
只剩 DiscoverContext（网络、进度上报），其余公共件来自 crawler_fields 和 util。
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote_plus, urlparse

import requests

from ..crawler_fields import BUILTIN_FIELDS
from .util import decode_response, normalize_row_datetimes


def search_urls(keyword, ctx):
    """关键词搜索视频：优先走官方 API，没有密钥就抓搜索结果页。"""
    request = ctx.request
    maximum = int(request.get("max_items") or 50)
    if maximum < 0: maximum = 10**9
    if ctx.task_id:
        ctx.update(ctx.task_id, status="running", message=f"正在 YouTube 搜索“{keyword}”", progress=10)
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    video_ids = []
    try:
        if api_key:
            response = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "snippet", "type": "video", "q": keyword,
                        "maxResults": min(maximum, 50), "key": api_key}, timeout=30,
            )
            response.raise_for_status()
            video_ids = [item.get("id", {}).get("videoId") for item in response.json().get("items", [])]
        else:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(keyword)}&hl=zh-CN"
            html = decode_response(ctx.http.get(search_url, request.get("proxies", []), timeout=30))
            video_ids = re.findall(r'"videoId"\s*:\s*"([\w-]{11})"', html)
    except requests.RequestException as exc:
        raise RuntimeError("YouTube 搜索暂时无法连接，请检查网络或代理配置后重试") from exc
    video_ids = list(dict.fromkeys(item for item in video_ids if item))[:maximum]
    if not video_ids:
        raise RuntimeError("YouTube 没有返回公开视频结果。可稍后重试，或由管理员配置 YOUTUBE_API_KEY。")
    return [f"https://www.youtube.com/watch?v={video_id}" for video_id in video_ids]


def account_url(value):
    text = str(value or '').strip()
    match = re.search(r'https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)', text, re.I)
    if match:
        return match.group(0).rstrip('/')
    if re.fullmatch(r'@[A-Za-z0-9._-]{3,}', text):
        return 'https://www.youtube.com/' + text
    return ''


def extract(url, request, flat=False, comments=False):
    """交给 yt-dlp 取视频/频道/评论元数据。"""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError('YouTube 采集组件未安装，请先安装 yt-dlp') from exc
    maximum = int(request.get('max_items') or 50)
    maximum = 500 if maximum < 0 else min(500, max(1, maximum))
    options = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'socket_timeout': 30,
               'retries': 3, 'playlistend': maximum,
               'extract_flat': 'in_playlist' if flat else False}
    if comments:
        options.update(getcomments=True, max_comments={'all': [int(request.get('max_items') or 50)]})
    proxies = request.get('proxies') or []
    if proxies:
        proxy = str(proxies[0]).strip()
        options['proxy'] = proxy if '://' in proxy else 'http://' + proxy
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.extract_info(url, download=False) or {}
    except Exception as exc:
        raise RuntimeError(f'YouTube 暂时无法访问：{str(exc).splitlines()[-1][:240]}') from exc


def video_row(info):
    return fill_row({key: '' for key in BUILTIN_FIELDS['youtube']}, info or {})


def fill_row(row, info):
    """把 yt-dlp 的元数据填进结果行（下载路径也用它）。"""
    upload_date = str(info.get("upload_date") or "")
    if len(upload_date) == 8 and upload_date.isdigit():
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    values = {
        "title": info.get("title"), "description": info.get("description"),
        "channel": info.get("channel") or info.get("uploader"),
        "published_at": info.get("timestamp") or upload_date,
        "duration": info.get("duration_string") or info.get("duration"),
        "views": info.get("view_count"), "likes": info.get("like_count"),
        "thumbnail": info.get("thumbnail"), "url": info.get("webpage_url"),
    }
    for key in list(row):
        if key in values and values[key] not in (None, ""):
            row[key] = values[key]
    return normalize_row_datetimes(row)


def page_values(url, page_source):
    """视频页里没有对应的 meta 标签，频道名和播放量只能从内嵌 JSON 里取。"""
    values = {}
    if "youtube.com" in urlparse(url).netloc.lower():
        for field_name, json_name in (("channel", "ownerChannelName"), ("views", "viewCount")):
            match = re.search(rf'"{json_name}"\s*:\s*"((?:\\.|[^"\\])*)"', page_source)
            if match:
                try:
                    values[field_name] = json.loads(f'"{match.group(1)}"')
                except json.JSONDecodeError:
                    values[field_name] = match.group(1)
    return values


def discover(value, ctx):
    """按 youtube_mode 发现目标：关键词视频、视频评论、账号信息、账号视频。"""
    request = ctx.request
    mode = str(request.get('youtube_mode') or 'keyword').lower()
    maximum = int(request.get('max_items') or 50)
    maximum = 500 if maximum < 0 else min(500, max(1, maximum))
    if mode == 'keyword':
        return search_urls(value, ctx)
    if mode == 'comments':
        video_url = str(value or '').strip()
        if not re.match(r'https?://(?:www\.)?(?:youtube\.com/watch\?[^\s]*v=|youtu\.be/)', video_url, re.I):
            raise ValueError('请输入完整的 YouTube 视频链接')
        info = extract(video_url, request, comments=True)
        rows = []
        for index, item in enumerate(info.get('comments') or [], 1):
            author = str(item.get('author') or item.get('author_id') or '')
            text = str(item.get('text') or '').strip()
            if not text: continue
            cid = str(item.get('id') or index)
            rows.append({'author': author, 'username': item.get('author_id') or '', 'text': text,
                         'published_at': item.get('timestamp') or item.get('time_text') or '',
                         'likes': item.get('like_count', ''), 'replies': item.get('reply_count', ''),
                         'url': f'{video_url}#comment-{cid}'})
            if len(rows) >= maximum: break
        if not rows: raise RuntimeError('YouTube 未返回可公开读取的评论')
        return [{'url': row['url'], 'detail_url': '', 'prefill': row} for row in rows]
    account = account_url(value)
    if not account: raise ValueError('请输入 YouTube 账号，例如 @YouTube 或频道链接')
    extract_url = account.rstrip('/') + '/videos' if mode == 'videos' and not account.rstrip('/').endswith('/videos') else account
    info = extract(extract_url, request, flat=(mode == 'videos'))
    if mode == 'user':
        row = {'username': info.get('channel_id') or info.get('uploader_id') or account.rsplit('/', 1)[-1],
               'display_name': info.get('channel') or info.get('uploader') or info.get('title') or '',
               'bio': info.get('description') or '', 'followers': info.get('channel_follower_count', ''),
               'videos': info.get('playlist_count') or info.get('channel_video_count', ''),
               'verified': info.get('channel_is_verified', ''), 'avatar': info.get('channel_thumbnail') or info.get('thumbnail') or '', 'url': account}
        return [{'url': account, 'detail_url': '', 'prefill': row}]
    targets = []
    for entry in info.get('entries') or []:
        if not entry: continue
        url = str(entry.get('webpage_url') or entry.get('url') or '')
        if not url.startswith('http') and entry.get('id'): url = f'https://www.youtube.com/watch?v={entry["id"]}'
        if not url: continue
        row = video_row(entry); row['url'] = url
        targets.append({'url': url, 'detail_url': '', 'prefill': row})
        if len(targets) >= maximum: break
    if not targets: raise RuntimeError('YouTube 未返回该账号的公开视频')
    return targets
