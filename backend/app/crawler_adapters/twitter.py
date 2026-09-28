"""X / Twitter 数据源：公开时间线、公开索引与 oEmbed，无需 Bearer Token。

原先散在 CrawlerTaskManager 上的同名方法整体搬到这里，逻辑未变。
"""
from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .util import decode_response, text, walk_json


def parse_username(value):
    text = str(value or "").strip()
    match = re.search(r"(?:https?://(?:www\.)?(?:x|twitter)\.com/|(?:^|\s)(?:from:|@))([A-Za-z0-9_]{1,15})", text, re.I)
    if not match and re.fullmatch(r"[A-Za-z0-9_]{1,15}", text):
        match = re.match(r"([A-Za-z0-9_]{1,15})", text)
    return match.group(1) if match else ""

def profile_document(username, ctx):
    request = ctx.request
    response = ctx.http.get(
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}?lang=zh-cn",
        request.get("proxies", []), timeout=18,
        headers={"Referer": "https://platform.twitter.com/"},
    )
    html = decode_response(response)
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#__NEXT_DATA__")
    data = {}
    if node:
        try:
            data = json.loads(node.get_text())
        except json.JSONDecodeError:
            pass
    return html, soup, data

def discover_user(account, ctx):
    task_id = ctx.task_id
    accounts = [item.strip() for item in re.split(r"[\n,，;；]+", str(account or "")) if item.strip()]
    if len(accounts) > 1:
        rows = []
        for item in accounts[:500]:
            rows.extend(discover_user(item, ctx))
        return rows
    username = parse_username(account)
    if not username:
        raise ValueError("请输入有效的 X 用户名，例如 @OpenAI")
    if task_id:
        ctx.update(task_id, status="running", message=f"正在读取 @{username} 的公开用户信息", progress=10)
    html, soup, data = profile_document(username, ctx)
    normalized = username.lower()
    users = []
    for item in walk_json(data):
        handle = str(item.get("screen_name") or item.get("username") or "").lstrip("@").lower()
        if handle == normalized:
            users.append(item)
    user = max(users, key=lambda item: sum(key in item for key in (
        "name", "description", "followers_count", "friends_count", "statuses_count",
        "profile_image_url_https", "created_at", "verified",
    )), default={})
    title_node = soup.select_one("meta[property='og:title']")
    page_title = str(title_node.get("content") or "") if title_node else text(soup.title)
    page_description = ""
    description_node = soup.select_one("meta[property='og:description'], meta[name='description']")
    if description_node:
        page_description = str(description_node.get("content") or "")
    avatar_node = soup.select_one("meta[property='og:image']")
    avatar = str(avatar_node.get("content") or "") if avatar_node else ""
    display_name = str(user.get("name") or "").strip()
    if not display_name and page_title:
        display_name = re.sub(r"\s*\(@[^)]+\).*", "", page_title).strip()
    row = {
        "username": username,
        "display_name": display_name or username,
        "bio": str(user.get("description") or page_description).strip(),
        "location": str(user.get("location") or "").strip(),
        "followers": user.get("followers_count", ""),
        "following": user.get("friends_count", ""),
        "posts": user.get("statuses_count", ""),
        "joined_at": user.get("created_at", ""),
        "verified": user.get("verified", ""),
        "avatar": user.get("profile_image_url_https") or user.get("profile_image_url") or avatar,
        "url": f"https://x.com/{username}",
    }
    if not user and not page_title and username.lower() not in html.lower():
        raise RuntimeError(f"没有读取到 @{username} 的公开用户信息")
    return [{"url": row["url"], "detail_url": "", "prefill": row}]

def posts(keyword, ctx):
    """无需 API Token，从公开索引发现推文并通过 X oEmbed 读取内容。"""
    request, task_id = ctx.request, ctx.task_id
    mode = str(request.get("twitter_mode") or "keyword").lower()
    if mode == "user":
        return discover_user(keyword, ctx)
    if mode == "history":
        username = parse_username(keyword)
        if not username:
            raise ValueError("请输入有效的 X 用户名，例如 @OpenAI")
        keyword = f"@{username}"
    requested = int(request.get("max_items") or 50)
    maximum = 500 if requested < 0 else min(500, max(1, requested))
    if task_id:
        ctx.update(task_id, status="running", message=f"正在公开索引中查找“{keyword}”", progress=10)

    status_pattern = re.compile(
        r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I
    )
    candidates = []

    def add_candidates(value):
        value = html_lib.unescape(str(value or "")).replace("\\/", "/")
        for username, status_id in status_pattern.findall(value):
            url = f"https://x.com/{username}/status/{status_id}"
            if url not in candidates:
                candidates.append(url)

    add_candidates(keyword)
    direct_input = bool(candidates)
    account_match = re.search(r"(?:^|\s)(?:from:|@)([A-Za-z0-9_]{1,15})(?:\s|$)", keyword, re.I)
    bare_account = mode == "history" and not account_match and bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}", keyword.strip()))
    if bare_account:
        account_match = re.match(r"([A-Za-z0-9_]{1,15})", keyword.strip())
    if account_match:
        username = account_match.group(1)
        try:
            profile = ctx.http.get(
                f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}?lang=zh-cn",
                request.get("proxies", []), timeout=18,
                headers={"Referer": "https://platform.twitter.com/"},
            )
            profile_html = decode_response(profile)
            # 公开时间线内部同时使用绝对链接和 /账号/status/id 相对路径。
            add_candidates(profile_html)
            for status_id in re.findall(rf"/{re.escape(username)}/status/(\d+)", profile_html, re.I):
                add_candidates(f"https://x.com/{username}/status/{status_id}")
        except requests.RequestException:
            pass

    if not direct_input and len(candidates) < maximum:
        query = quote_plus(f"site:x.com status {keyword}")
        search_urls = [
            f"https://html.duckduckgo.com/html/?q={query}",
            f"https://www.google.com/search?q={query}&num=50&hl=zh-CN",
        ]
        for search_url in search_urls:
            try:
                response = ctx.http.get(
                    search_url, request.get("proxies", []), timeout=18,
                    headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
                )
            except requests.RequestException:
                continue
            page = BeautifulSoup(decode_response(response), "html.parser")
            for link in page.select("a[href]"):
                href = urljoin(search_url, str(link.get("href") or ""))
                parsed = urlparse(href)
                href = parse_qs(parsed.query).get("uddg", [href])[0]
                if href.startswith("/url?"):
                    href = parse_qs(urlparse(href).query).get("q", [href])[0]
                add_candidates(href)
            add_candidates(str(page))
            if len(candidates) >= maximum:
                break

    targets = []
    for post_url in candidates[:maximum]:
        ctx.checkpoint(task_id)
        payload = None
        embed_url = post_url.replace("https://x.com/", "https://twitter.com/")
        for endpoint in ("https://publish.twitter.com/oembed", "https://publish.x.com/oembed"):
            try:
                response = ctx.http.get(
                    endpoint + "?" + urlencode({
                        "url": embed_url, "omit_script": "true", "dnt": "true", "lang": "zh-cn",
                    }),
                    request.get("proxies", []), timeout=15,
                    headers={"Referer": "https://platform.twitter.com/"},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("html"):
                    break
            except (requests.RequestException, ValueError):
                continue
        if not payload:
            continue
        embed = BeautifulSoup(str(payload.get("html") or ""), "html.parser")
        text_node = embed.select_one("blockquote p")
        date_link = embed.select("blockquote a[href]")
        body = text(text_node)
        if not body:
            continue
        published_at = text(date_link[-1]) if date_link else ""
        author = str(payload.get("author_name") or "").strip()
        username_match = status_pattern.search(post_url)
        username = username_match.group(1) if username_match else ""
        # 账号时间线可附带关键词，按正文做本地筛选；from:/@账号部分不参与匹配。
        filter_text = "" if bare_account else re.sub(
            r"(?:^|\s)(?:from:|@)[A-Za-z0-9_]{1,15}(?:\s|$)", " ", keyword
        ).strip()
        if account_match and filter_text and filter_text.lower() not in body.lower():
            continue
        targets.append({"url": post_url, "detail_url": "", "prefill": {
            "text": body, "author": author or username,
            "published_at": published_at, "likes": "", "reposts": "", "replies": "",
            "media": "", "url": str(payload.get("url") or post_url).replace("twitter.com/", "x.com/"),
        }})
        if len(targets) >= maximum:
            break
    if not targets:
        raise RuntimeError(
            f"公开网页索引暂未找到与“{keyword}”相关且可嵌入的推文。"
            "可尝试输入 @账号、from:账号 关键词，或稍后重试。"
        )
    return targets
