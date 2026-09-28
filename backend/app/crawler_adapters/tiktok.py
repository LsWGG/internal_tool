"""TikTok 数据源：关键词、账号、视频与公开评论。

原先散在 CrawlerTaskManager 上的同名方法整体搬到这里，逻辑未变。
抖音共用同一套行构造与 yt-dlp 抽取，只有分发入口不同，见 douyin.py。
"""
from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

from ..crawler_browser import browser_scope
from ..crawler_http import UA_DESKTOP_FULL, UA_MINIMAL
from .util import decode_response, walk_json


def username(value):
    text = html_lib.unescape(str(value or "")).strip()
    # 抖音账号昵称可能包含中文；允许 @ 后的 Unicode 字符，过滤空白和 URL 分隔符。
    match = re.search(r"(?:https?://(?:www\.)?(?:tiktok\.com|douyin\.com)/)?@([^\s/@?#]{2,40})", text, re.I)
    if not match and re.fullmatch(r"[^\s/@?#]{2,40}", text):
        match = re.match(r"([^\s/@?#]{2,40})", text)
    return match.group(1) if match else ""


def video_url(value):
    match = re.search(
        r"https?://(?:www\.)?(?:tiktok\.com/@[A-Za-z0-9._]+/video/\d+|douyin\.com/video/\d+|v\.douyin\.com/[A-Za-z0-9_-]+)",
        html_lib.unescape(str(value or "")), re.I,
    )
    return match.group(0).split("?", 1)[0] if match else ""


def video_row(info):
    info = info or {}
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or info.get("url") or "")
    if not webpage_url.startswith("http") and info.get("id") and info.get("uploader_id"):
        webpage_url = f"https://www.tiktok.com/@{info['uploader_id']}/video/{info['id']}"
    return {
        "title": info.get("title") or info.get("fulltitle") or "",
        "description": info.get("description") or "",
        "author": info.get("uploader") or info.get("creator") or "",
        "username": info.get("uploader_id") or info.get("channel_id") or "",
        "published_at": info.get("timestamp") or info.get("upload_date") or "",
        "duration": info.get("duration_string") or info.get("duration") or "",
        "views": info.get("view_count", ""),
        "likes": info.get("like_count", ""),
        "comments": info.get("comment_count", ""),
        "shares": info.get("repost_count", info.get("share_count", "")),
        "thumbnail": info.get("thumbnail") or "",
        "url": webpage_url,
    }


def user_from_html(username_value, html):
    soup = BeautifulSoup(html or "", "html.parser")
    documents = []
    for node in soup.select("script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, script[type='application/json']"):
        try:
            documents.append(json.loads(node.string or node.get_text() or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
    normalized = username_value.lower()
    candidates = []
    for document in documents:
        for item in walk_json(document):
            handle = str(item.get("uniqueId") or item.get("unique_id") or item.get("username") or "").lstrip("@").lower()
            if handle == normalized:
                candidates.append(item)
            nested_user = item.get("user") if isinstance(item.get("user"), dict) else None
            nested_stats = item.get("stats") if isinstance(item.get("stats"), dict) else None
            if nested_user:
                nested_handle = str(nested_user.get("uniqueId") or nested_user.get("username") or "").lstrip("@").lower()
                if nested_handle == normalized:
                    candidates.append({**nested_user, **(nested_stats or {})})
    user = max(candidates, key=lambda item: sum(key in item for key in (
        "nickname", "signature", "followerCount", "followingCount", "videoCount", "heartCount",
    )), default={})
    stats = next((item for item in candidates if any(key in item for key in (
        "followerCount", "followingCount", "videoCount", "heartCount",
    ))), {})
    row = {
        "username": user.get("uniqueId") or user.get("unique_id") or username_value,
        "display_name": user.get("nickname") or username_value,
        "bio": user.get("signature") or "",
        "followers": stats.get("followerCount", user.get("followerCount", "")),
        "following": stats.get("followingCount", user.get("followingCount", "")),
        "videos": stats.get("videoCount", user.get("videoCount", "")),
        "likes": stats.get("heartCount", stats.get("heart", user.get("heartCount", ""))),
        "verified": user.get("verified", ""),
        "avatar": user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb") or "",
        "url": f"https://www.tiktok.com/@{username_value}",
    }
    return row if candidates else None


def comment_rows(items, video_url_value):
    rows, seen = [], set()
    for index, item in enumerate(items or [], 1):
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        username_value = str(item.get("username") or "").lstrip("@").strip()
        key = str(item.get("id") or f"{username_value}|{text}")
        if not text or key in seen:
            continue
        seen.add(key)
        rows.append({
            "author": item.get("author") or username_value, "username": username_value, "text": text,
            "published_at": item.get("published_at") or "", "likes": item.get("likes") or "",
            "replies": item.get("replies") or "", "url": f"{video_url_value}#comment-{item.get('id') or index}",
        })
    return rows


def extract(url, request, flat=False):
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("TikTok 采集组件未安装，请先安装 yt-dlp") from exc
    requested = int(request.get("max_items") or 50)
    maximum = 500 if requested < 0 else min(500, max(1, requested))
    options = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "socket_timeout": 30, "retries": 3, "playlistend": maximum,
        "extract_flat": "in_playlist" if flat else False,
    }
    # yt-dlp handles both TikTok and Douyin URLs; keep the extractor
    # generic so shared task/export logic remains unchanged.
    proxies = request.get("proxies") or []
    if proxies:
        proxy = str(proxies[0]).strip()
        options["proxy"] = proxy if "://" in proxy else "http://" + proxy
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.extract_info(url, download=False) or {}
    except Exception as exc:
        message = str(exc).splitlines()[-1]
        if re.search(r"login|captcha|verify|sign in", message, re.I):
            raise RuntimeError("TikTok 要求登录或访问验证，无法读取该公开内容") from exc
        raise RuntimeError(f"TikTok 暂时无法访问：{message[:240]}。请检查网络或代理配置") from exc


def oembed_row(url, request, http):
    """使用 TikTok 官方公开 oEmbed 补全搜索结果，不依赖登录态。"""
    response = http.get(
        "https://www.tiktok.com/oembed?" + urlencode({"url": url}),
        request.get("proxies", []), timeout=18,
        headers={"Accept": "application/json"},
    )
    payload = response.json()
    author_url = str(payload.get("author_url") or "")
    username_value = username(author_url)
    return {
        "title": payload.get("title") or "",
        "description": payload.get("title") or "",
        "author": payload.get("author_name") or username_value,
        "username": username_value,
        "published_at": "", "duration": "", "views": "", "likes": "",
        "comments": "", "shares": "",
        "thumbnail": payload.get("thumbnail_url") or "",
        "url": url,
    }


def index_urls(keyword, request, maximum, http):
    """TikTok 站内搜索不可用时，从公开网页索引发现视频链接。"""
    # 不同搜索引擎对 `site:`/`inurl:` 组合的处理并不一致；先用
    # 精确查询，再用宽查询兜底，避免因为搜索语法被忽略而得到空结果。
    queries = [
        f"site:tiktok.com/@ inurl:/video/ {keyword}",
        f"site:tiktok.com {keyword} video",
        f"TikTok {keyword}",
    ]
    endpoints = []
    for query_text in queries:
        query = quote_plus(query_text)
        endpoints.extend([
            f"https://html.duckduckgo.com/html/?q={query}",
            f"https://www.google.com/search?q={query}&num=50&hl=zh-CN",
            f"https://www.bing.com/search?q={query}&count=50",
        ])
    # 搜索引擎经常返回相对链接、转义斜杠或不带协议的 TikTok 链接。
    # 统一在这里恢复为可访问的公开视频地址，避免因链接表现形式变化而误判为空。
    pattern = re.compile(
        r"(?:(?:https?:)?//)?(?:www\.|m\.)?tiktok\.com/@[A-Za-z0-9._]+/video/\d+",
        re.I,
    )
    urls = []

    def add(value):
        text = unquote(html_lib.unescape(str(value or ""))).replace("\\u002F", "/").replace("\\/", "/")
        for found in pattern.findall(text):
            clean = found.split("?", 1)[0]
            if clean.startswith("//"):
                clean = "https:" + clean
            elif not clean.startswith("http"):
                clean = "https://" + clean
            clean = clean.replace("https://m.tiktok.com/", "https://www.tiktok.com/")
            if clean not in urls:
                urls.append(clean)

    for endpoint in endpoints:
        if len(urls) >= maximum:
            break
        try:
            response = http.get(
                endpoint, request.get("proxies", []), timeout=18,
                headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
            )
        except requests.RequestException:
            continue
        page_html = decode_response(response)
        add(page_html)
        soup = BeautifulSoup(page_html, "html.parser")
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "")
            parsed = urlparse(href)
            for key in ("uddg", "q", "url", "u"):
                value = parse_qs(parsed.query).get(key, [])
                if value:
                    add(value[0])
            add(href)
    return urls[:maximum]


def browser_urls(keyword, request, maximum, browser=None):
    """从浏览器 DOM 和站内公开搜索响应中同时发现视频。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401  仅探测运行环境
    except ImportError:
        return []
    urls = []

    def add(url):
        clean = video_url(url)
        if clean and clean not in urls:
            urls.append(clean)

    def add_payload(payload):
        for item in walk_json(payload):
            video_id = str(item.get("id") or item.get("aweme_id") or "")
            author = item.get("author") if isinstance(item.get("author"), dict) else {}
            username_value = str(author.get("uniqueId") or author.get("unique_id") or "")
            if video_id.isdigit() and username_value:
                add(f"https://www.tiktok.com/@{username_value}/video/{video_id}")

    with browser_scope(browser, request.get("proxies")) as engine:
        context = engine.new_context(
            user_agent=UA_DESKTOP_FULL,
            locale="zh-CN", viewport={"width": 1440, "height": 1000},
        )
        page = context.new_page()
        try:
            def handle_response(response):
                if "search" not in response.url.lower():
                    return
                try:
                    if "json" in str(response.headers.get("content-type") or "").lower():
                        add_payload(response.json())
                except Exception:
                    pass

            page.on("response", handle_response)
            page.goto(
                f"https://www.tiktok.com/search?q={quote_plus(keyword)}",
                wait_until="domcontentloaded", timeout=45000,
            )
            for _ in range(8):
                page.wait_for_timeout(1000)
                for href in page.locator('a[href*="/video/"]').evaluate_all(
                        "nodes => nodes.map(node => node.href)"):
                    add(href)
                if len(urls) >= maximum:
                    break
                page.mouse.wheel(0, 850)
            return urls[:maximum]
        finally:
            context.close()


def user(account, ctx):
    request, task_id = ctx.request, ctx.task_id
    accounts = [item.strip() for item in re.split(r"[\n,，;；]+", str(account or "")) if item.strip()]
    if len(accounts) > 1:
        rows = []
        for item in accounts[:500]:
            rows.extend(user(item, ctx))
        return rows
    username_value = username(account)
    if not username_value:
        raise ValueError("请输入有效的 TikTok 账号，例如 @tiktok")
    if task_id:
        ctx.update(task_id, status="running", message=f"正在读取 @{username_value} 的公开账号信息", progress=10)
    platform = str(request.get("source") or "tiktok").lower()
    profile_url = (f"https://www.douyin.com/user/{username_value}"
                   if platform == "douyin" else f"https://www.tiktok.com/@{username_value}")
    try:
        html = ctx.fetch_html(profile_url, {**request, "dynamic": True, "preview_mode": True},
                              browser=ctx.browser)
        row = user_from_html(username_value, html)
    except Exception:
        row = None
    if not row:
        info = extract(profile_url, request, flat=True)
        entry = next((item for item in (info.get("entries") or []) if item), {})
        row = {
            "username": entry.get("uploader_id") or username_value,
            "display_name": entry.get("uploader") or info.get("uploader") or username_value,
            "bio": info.get("description") or "", "followers": "", "following": "",
            "videos": info.get("playlist_count") or "", "likes": "", "verified": "",
            "avatar": info.get("thumbnail") or entry.get("thumbnail") or "", "url": profile_url,
        }
    return [{"url": profile_url, "detail_url": "", "prefill": row}]


def account_videos(account, ctx):
    request, task_id = ctx.request, ctx.task_id
    accounts = [item.strip() for item in re.split(r"[\n,，;；]+", str(account or "")) if item.strip()]
    if len(accounts) > 1:
        targets, seen = [], set()
        for item in accounts[:500]:
            for target in account_videos(item, ctx):
                url = target.get("url", "") if isinstance(target, dict) else str(target)
                if url and url not in seen:
                    seen.add(url)
                    targets.append(target)
        return targets
    username_value = username(account)
    if not username_value:
        raise ValueError("请输入有效的 TikTok 账号，例如 @tiktok")
    if task_id:
        ctx.update(task_id, status="running", message=f"正在查找 @{username_value} 的公开视频", progress=10)
    platform = str(request.get("source") or "tiktok").lower()
    profile_url = (f"https://www.douyin.com/user/{username_value}"
                   if platform == "douyin" else f"https://www.tiktok.com/@{username_value}")
    info = extract(profile_url, request, flat=True)
    targets = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        row = video_row(entry)
        url = row["url"] or video_url(entry.get("url"))
        if not url and entry.get("id"):
            url = (f"https://www.douyin.com/video/{entry['id']}"
                   if platform == "douyin" else f"https://www.tiktok.com/@{username_value}/video/{entry['id']}")
        if url:
            row["url"] = url
            targets.append({"url": url, "detail_url": "", "prefill": row})
    if not targets:
        raise RuntimeError(f"没有读取到 @{username_value} 的公开视频，请检查账号或代理配置")
    return targets


def keyword_videos(keyword, ctx):
    request, task_id = ctx.request, ctx.task_id
    keyword = str(keyword or "").strip()
    direct = video_url(keyword)
    if direct:
        try:
            row = video_row(extract(direct, request))
        except RuntimeError:
            row = oembed_row(direct, request, ctx.http)
        row["url"] = row["url"] or direct
        return [{"url": direct, "detail_url": "", "prefill": row}]
    if not keyword:
        raise ValueError("请输入 TikTok 搜索关键词")
    if task_id:
        ctx.update(task_id, status="running", message=f"正在搜索 TikTok 关键词“{keyword}”", progress=10)
    requested = int(request.get("max_items") or 50)
    maximum = 500 if requested < 0 else min(500, max(1, requested))
    search_urls = [
        f"https://www.tiktok.com/search?q={quote_plus(keyword)}",
        f"https://www.tiktok.com/search?lang=en&q={quote_plus(keyword)}",
    ]
    html = ""
    for search_url in search_urls:
        try:
            html = ctx.fetch_html(search_url, {**request, "dynamic": True, "preview_mode": True},
                                  browser=ctx.browser)
        except Exception:
            html = ""
        html = html_lib.unescape(html).replace("\\u002F", "/").replace("\\/", "/")
        if re.search(r"(?:tiktok\.com|/video/)" , html, re.I):
            break
    pattern = re.compile(r"(?:https?://(?:www\.)?tiktok\.com)?(/@[A-Za-z0-9._]+/video/\d+)", re.I)
    urls = list(dict.fromkeys("https://www.tiktok.com" + path for path in pattern.findall(html)))
    if len(urls) < maximum:
        try:
            for url in browser_urls(keyword, request, maximum, browser=ctx.browser):
                if url not in urls:
                    urls.append(url)
                if len(urls) >= maximum:
                    break
        except Exception:
            pass
    if len(urls) < maximum:
        for url in index_urls(keyword, request, maximum, ctx.http):
            if url not in urls:
                urls.append(url)
            if len(urls) >= maximum:
                break
    targets = []
    for url in urls[:maximum]:
        ctx.checkpoint(task_id)
        try:
            row = video_row(extract(url, request))
        except RuntimeError:
            try:
                row = oembed_row(url, request, ctx.http)
            except (requests.RequestException, ValueError):
                row = {"url": url}
        row["url"] = row.get("url") or url
        targets.append({"url": url, "detail_url": "", "prefill": row})
    if not targets:
        raise RuntimeError(
            "TikTok 当前未返回可公开访问的视频结果（站内搜索可能需要登录或受地区限制）。"
            "请粘贴一个 TikTok 视频链接，或在高级设置中配置可用代理后重试。"
        )
    return targets


def comments(value, ctx):
    request, task_id = ctx.request, ctx.task_id
    video_url_value = video_url(value)
    if not video_url_value:
        raise ValueError("请输入完整的 TikTok 视频链接")
    if task_id:
        ctx.update(task_id, status="running", message="正在加载公开视频评论", progress=10)
    requested = int(request.get("max_items") or 50)
    maximum = 500 if requested < 0 else min(500, max(1, requested))
    # 先尝试 TikTok 公开评论接口（页面 DOM 不稳定时仍可获取评论）。
    video_id = video_url_value.rstrip("/").split("/")[-1].split("?")[0]
    try:
        api = f"https://www.tiktok.com/api/comment/list/?aid=1988&aweme_id={video_id}&count={min(maximum,100)}&cursor=0"
        response = ctx.http.get(api, request.get("proxies", []), timeout=20,
                                headers={"Referer": video_url_value, "User-Agent": UA_MINIMAL})
        payload = response.json()
        comments_payload = payload.get("comments") or payload.get("data", {}).get("comments") or []
        if comments_payload:
            items = [{"id": item.get("cid"), "text": item.get("text") or item.get("share_info", {}).get("desc"),
                      "username": (item.get("user") or {}).get("unique_id") or (item.get("user") or {}).get("nickname"),
                      "author": (item.get("user") or {}).get("nickname"), "likes": item.get("digg_count"),
                      "published_at": item.get("create_time")} for item in comments_payload]
            rows = comment_rows(items, video_url_value)[:maximum]
            if rows:
                return [{"url": row["url"], "detail_url": "", "prefill": row} for row in rows]
    except Exception:
        pass
    try:
        with browser_scope(ctx.browser, request.get("proxies")) as engine:
            # 这个调用点原本不带桌面 UA，保持原样。
            context = engine.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
            try:
                page = context.new_page()
                page.goto(video_url_value, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2200)
                items = []
                stable = 0
                while len(items) < maximum and stable < 3:
                    current = page.locator('[data-e2e="comment-level-1"], [data-e2e="comment-item"], div[class*="DivCommentItemContainer"], div[class*="CommentItem"]').evaluate_all("""nodes => nodes.map((node, index) => ({
                      id: node.getAttribute('data-comment-id') || node.id || '',
                      username: (node.querySelector('[data-e2e="comment-username-1"], a[href^="/@"]')?.textContent || '').trim(),
                      author: (node.querySelector('[data-e2e="comment-username-1"], a[href^="/@"]')?.textContent || '').trim(),
                      text: (node.querySelector('[data-e2e="comment-level-1"] p, [data-e2e="comment-text"], p')?.textContent || node.textContent || '').trim(),
                      published_at: (node.querySelector('[data-e2e="comment-time-1"], time, span[class*="SpanCreatedTime"]')?.textContent || '').trim(),
                      likes: (node.querySelector('[data-e2e="comment-like-count"], span[class*="SpanLikeCount"]')?.textContent || '').trim(),
                      replies: (node.querySelector('[data-e2e="view-more-1"], div[class*="DivReplyActionContainer"]')?.textContent || '').trim()
                    }))""")
                    stable = stable + 1 if len(current) <= len(items) else 0
                    items = current
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1000)
                content = page.content()
                if not items and re.search(r"captcha|verify|challenge|robot.?check|验证码", content, re.I):
                    # TikTok 评论接口经常要求登录/验证。保留目标视频作为可预览记录，
                    # 避免整个预览流程卡死；正式采集时会在结果中标注评论不可用。
                    return [{"url": video_url_value, "detail_url": "", "prefill": {
                        "url": video_url_value, "text": "", "author": "", "username": "",
                        "published_at": "", "likes": "", "replies": "",
                        "collection_note": "TikTok 要求登录或访问验证，未能读取公开评论"
                    }}]
            finally:
                context.close()
    except ImportError as exc:
        raise RuntimeError("评论采集需要安装 Playwright 浏览器运行环境") from exc
    rows = comment_rows(items, video_url_value)[:maximum]
    if not rows:
        # 页面可正常展示评论时，评论接口可能延迟或 DOM 结构变化；
        # 不再将其升级为预览失败，返回视频占位记录供字段点选。
        return [{"url": video_url_value, "detail_url": "", "prefill": {
            "url": video_url_value, "collection_note": "页面未返回可解析的评论内容，请稍后重试"
        }}]
    return [{"url": row["url"], "detail_url": "", "prefill": row} for row in rows]


def discover(value, ctx):
    request = ctx.request
    mode = str(request.get("tiktok_mode") or "keyword").lower()
    if mode == "user":
        return user(value, ctx)
    if mode == "videos":
        return account_videos(value, ctx)
    if mode == "comments":
        return comments(value, ctx)
    return keyword_videos(value, ctx)
