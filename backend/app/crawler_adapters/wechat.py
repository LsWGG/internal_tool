"""微信公众号数据源：搜狗桌面/移动端公开索引，以及通用公开索引兜底。

原先散在 CrawlerTaskManager 上的同名方法整体搬到这里，逻辑未变。
三级索引依次降级、近期成功结果 30 分钟内复用，都是为了不触发出口 IP 验证。
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..crawler_http import UA_WECHAT_MOBILE
from .util import clean_url, decode_response, format_beijing_datetime, text as _text


def decode_script_url(value):
    value = str(value or "").replace("\\/", "/")
    # 不能对整条 URL 使用 html.unescape：`&timestamp` 会被按无分号的
    # `&times` 实体解码成 `×tamp`，从而破坏微信签名参数。
    value = re.sub(r"(?i)&amp;", "&", value)
    value = re.sub(
        r"&#(?:x[0-9a-fA-F]+|\d+);?",
        lambda item: html_lib.unescape(item.group(0)), value,
    )
    value = value.replace("&quot;", '"').replace("&apos;", "'")
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda item: chr(int(item.group(1), 16)), value)
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda item: chr(int(item.group(1), 16)), value)
    if value.startswith("//"):
        value = "https:" + value
    return value.strip().strip("'\" ;)")


def resolve_sogou_url(href, request, session=None, referer="", http=None):
    """在同一搜索会话内执行搜狗中转，并只返回真实微信文章地址。"""
    public_url = urljoin("https://weixin.sogou.com", href)
    response = http.get(
        public_url, request.get("proxies", []), timeout=20, session=session,
        headers={"Referer": referer or "https://weixin.sogou.com/"},
    )

    # HTTP 重定向链中的 Location 或最终地址有时已经是微信原文。
    candidates = [response.url]
    for item in [*response.history, response]:
        location = item.headers.get("Location")
        if location:
            candidates.append(urljoin(item.url, location))

    content = decode_response(response)
    # 常见中转页把 URL 拆成 `url = '...'`、`url += '...'` 多段后再跳转。
    chunks = [match[1] for match in re.findall(
        r"(?:var\s+)?url\s*(?:\+)?=\s*(['\"])(.*?)\1", content, re.I | re.S
    )]
    if chunks:
        candidates.append("".join(chunks))

    # 同时兼容直接写入脚本、location 跳转、Meta Refresh 和普通链接的页面。
    candidates.extend(re.findall(
        r"https?(?::|%3A)(?:\\?/|%2F){2}mp\.weixin\.qq\.com[^\s'\"<>]+", content, re.I
    ))
    candidates.extend(match[1] for match in re.findall(
        r"(?:location\.(?:replace|assign)|location\.href\s*=)\s*\(?\s*(['\"])(.*?)\1", content, re.I | re.S
    ))
    soup = BeautifulSoup(content, "html.parser")
    refresh = soup.select_one("meta[http-equiv]")
    if refresh and str(refresh.get("http-equiv", "")).lower() == "refresh":
        match = re.search(r"url\s*=\s*(.+)$", str(refresh.get("content") or ""), re.I)
        if match:
            candidates.append(match.group(1))
    candidates.extend(node.get("href", "") for node in soup.select("a[href*='mp.weixin.qq.com']"))

    for candidate in candidates:
        candidate = decode_script_url(candidate)
        candidate = re.sub(r"(?i)%3a", ":", candidate)
        candidate = re.sub(r"(?i)%2f", "/", candidate)
        parsed = urlparse(candidate)
        if parsed.hostname and parsed.hostname.lower() == "mp.weixin.qq.com":
            return clean_url(candidate)
    return ""


def sogou_urls(account_name, ctx):
    """按公众号名称检索搜狗微信公开索引，不访问登录态或绕过验证码。"""
    request, task_id = ctx.request, ctx.task_id
    maximum = int(request.get("max_items") or 50)
    if maximum < 0: maximum = 10**9
    articles, seen = [], set()
    other_accounts = {}          # 索引里实际出现的发布方 → 条数；名字对不上时这是唯一能核对的线索
    normalized_name = re.sub(r"\s+", "", account_name).lower()
    session = requests.Session()
    page_number = 0
    # 搜狗公开索引有短时频控；最多退避重试两次，不尝试绕过验证码。
    while len(articles) < maximum:
        page_number += 1
        ctx.checkpoint(task_id)
        ctx.update(task_id, status="running", message=f"正在查找“{account_name}”的公开文章 · 第 {page_number} 页", progress=10)
        search_url = f"https://weixin.sogou.com/weixin?type=2&query={quote_plus(account_name)}&page={page_number}"
        response = None
        soup = None
        items = []
        attempts = 3 if page_number == 1 else 1
        for attempt in range(attempts):
            try:
                if attempt:
                    session.close()
                    session = requests.Session()
                    ctx.update(task_id, message=f"公开索引暂时无结果，正在重试（{attempt + 1}/{attempts}）")
                    time.sleep(2 + attempt * 3)
                # 搜狗的 3 个索引调用点都关掉 HTTP 层的重试与限流：这里上面的循环
                # 自带「换 session + 退避」的重试语义，叠加会放大成 3×3 次尝试；
                # 限流也会扰动现在这套刻意留出间隔、刚好不触发验证码的访问节奏。
                response = ctx.http.get(
                    search_url, request.get("proxies", []), timeout=25, session=session,
                    retries=0, rate=False,
                    headers={"Referer": "https://weixin.sogou.com/", "Accept-Language": "zh-CN,zh;q=0.9"},
                )
                soup = BeautifulSoup(decode_response(response), "html.parser")
                page_text = soup.get_text(" ", strip=True)
                captcha = soup.select_one("#seccodeImage, .verify-wrap, .vcode-box, input[name='cpt']")
                captcha = captcha or "/antispider/" in response.url
                captcha = captcha or "此验证码用于确认这些请求是您的正常行为" in page_text
                captcha = captcha or ("请先验证" in page_text and "验证码" in page_text)
                if captcha:
                    break
                items = soup.select(".news-box li, ul.news-list li, .news-list li")
                if not captcha and (items or page_number > 1):
                    break
            except requests.RequestException:
                if attempt == attempts - 1:
                    raise
        page_text = soup.get_text(" ", strip=True) if soup else ""
        captcha = soup.select_one("#seccodeImage, .verify-wrap, .vcode-box, input[name='cpt']") if soup else None
        captcha = captcha or (response is not None and "/antispider/" in response.url)
        captcha = captcha or "此验证码用于确认这些请求是您的正常行为" in page_text
        captcha = captcha or ("请先验证" in page_text and "验证码" in page_text)
        if captcha:
            raise RuntimeError("公众号公开搜索暂时需要人工验证。请稍后重试，或直接粘贴微信文章链接；系统不会绕过验证码。")
        if not items:
            break
        matched_on_page = 0
        for item in items:
            account = _text(item.select_one(".account, .s-p, .account-name, [uigs*='account']"))
            if account and normalized_name not in re.sub(r"\s+", "", account).lower():
                other_accounts[account] = other_accounts.get(account, 0) + 1
                continue
            link = item.select_one("h3 a[href], .txt-box a[href], a[href*='/link?url=']")
            if not link:
                continue
            public_url = clean_url(urljoin(search_url, link.get("href")))
            try:
                detail_url = resolve_sogou_url(
                    link.get("href"), request, session=session, referer=search_url, http=ctx.http
                )
            except Exception:
                detail_url = ""
            unique_url = detail_url or public_url
            if unique_url and unique_url not in seen:
                timestamp_node = item.select_one(".s-p .s2 script")
                timestamp_match = re.search(r"timeConvert\(['\"]?(\d+)", timestamp_node.get_text() if timestamp_node else "")
                published_at = ""
                if timestamp_match:
                    try:
                        published_at = datetime.fromtimestamp(int(timestamp_match.group(1)), timezone.utc).isoformat()
                    except (ValueError, OSError):
                        pass
                prefill = {
                    "title": _text(item.select_one("h3")) or _text(link), "description": _text(item.select_one(".txt-info")),
                    "author": account or account_name, "account": account or account_name,
                    "published_at": published_at, "content": "", "image": "",
                    # 导出字段只保存真实微信原文；中转页仅供内部去重，避免交付失效链接。
                    "url": detail_url,
                }
                if not detail_url:
                    prefill["collection_note"] = "仅公开索引；详情访问受平台限制"
                seen.add(unique_url)
                articles.append({"url": detail_url or public_url, "detail_url": detail_url, "prefill": prefill})
                matched_on_page += 1
                if len(articles) >= maximum:
                    return articles
        if matched_on_page == 0 and page_number > 1:
            break
        time.sleep(request.get("delay_seconds", 1.0))
    if not articles:
        # 结果全被名字挡掉时，把索引里真实出现的号列出来。名字少一个字就长这样，
        # 只回一句「没找到」的话，用户分不清是名字写错了还是这个号压根没被收录。
        top = sorted(other_accounts.items(), key=lambda item: -item[1])[:8]
        hint = (f"搜狗索引里有 {sum(other_accounts.values())} 条提及该名称的文章，发布方是："
                f"{'、'.join(name for name, _ in top)}，都不是这个公众号。" if top else "")
        raise RuntimeError(
            f"没有查找到“{account_name}”发布的公开历史文章。{hint}"
            "请核对公众号全称；微信没有开放按名称读取完整历史文章的公共接口。"
        )
    return articles


def mobile_urls(account_name, ctx):
    """使用搜狗微信移动端索引；其访问策略与桌面入口相互独立。"""
    request, task_id = ctx.request, ctx.task_id
    maximum = int(request.get("max_items") or 50)
    if maximum < 0:
        maximum = 500
    normalized_name = re.sub(r"\s+", "", account_name).lower()
    session = requests.Session()
    articles, seen = [], set()
    ctx.update(task_id, status="running", progress=10,
                message="桌面索引需要验证，正在切换移动端公开索引")
    for page_number in range(1, 6):
        ctx.checkpoint(task_id)
        search_url = (
            "https://weixin.sogou.com/weixinwap?type=2&query="
            f"{quote_plus(account_name)}&page={page_number}"
        )
        response = ctx.http.get(
            search_url, request.get("proxies", []), timeout=25, session=session,
            retries=0, rate=False,          # 同桌面索引：自带重试，见上
            headers={
                "User-Agent": UA_WECHAT_MOBILE,
                "Referer": "https://weixin.sogou.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        soup = BeautifulSoup(decode_response(response), "html.parser")
        page_text = soup.get_text(" ", strip=True)
        if "/antispider/" in response.url or "此验证码用于确认这些请求是您的正常行为" in page_text:
            raise RuntimeError("移动端公开索引也触发了访问验证")
        items = soup.select("li")
        if not items:
            break
        added = 0
        for item in items:
            account_node = item.select_one(".s2[data-sourcename], .s2")
            account = str(account_node.get("data-sourcename") or _text(account_node)) if account_node else ""
            if re.sub(r"\s+", "", account).lower() != normalized_name:
                continue
            link = item.select_one("h4 a[href^='/link?'], a[data-uigs^='article_title_'][href]")
            if not link:
                continue
            try:
                detail_url = resolve_sogou_url(
                    link.get("href"), request, session=session, referer=search_url, http=ctx.http
                )
            except Exception:
                detail_url = ""
            if not detail_url or detail_url in seen:
                continue
            date_node = item.select_one(".s3[data-lastmodified], .s3")
            published_at = str(date_node.get("data-lastmodified") or _text(date_node)) if date_node else ""
            articles.append({
                "url": detail_url,
                "detail_url": detail_url,
                "prefill": {
                    "title": _text(link),
                    "description": _text(item.select_one("[data-type='article_summary']")),
                    "author": account,
                    "account": account,
                    "published_at": format_beijing_datetime(published_at),
                    "content": "",
                    "image": "",
                    "url": detail_url,
                },
            })
            seen.add(detail_url)
            added += 1
            if len(articles) >= maximum:
                return articles
        if added == 0 and page_number > 1:
            break
        time.sleep(max(0.5, float(request.get("delay_seconds") or 1.0)))
    if not articles:
        raise RuntimeError(f"移动端公开索引没有找到“{account_name}”发布的文章")
    return articles


def public_search_urls(account_name, ctx):
    """通过通用公开索引发现微信原文并逐篇校验公众号。"""
    request, task_id = ctx.request, ctx.task_id
    maximum = int(request.get("max_items") or 50)
    if maximum < 0:
        maximum = 500
    normalized_name = re.sub(r"\s+", "", account_name).lower()
    query = quote_plus(f'site:mp.weixin.qq.com/s "{account_name}"')
    search_url = f"https://html.duckduckgo.com/html/?q={query}"
    ctx.update(task_id, status="running", progress=10,
                message="主索引需要验证，正在切换备用公开索引")
    last_error = None
    response = None
    for attempt in range(2):
        try:
            if attempt:
                time.sleep(3)
            response = ctx.http.get(
                search_url, request.get("proxies", []), timeout=25,
                retries=0, rate=False,      # 同桌面索引：自带重试，见上
                headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
            break
        except requests.RequestException as exc:
            last_error = exc
    if response is None:
        raise RuntimeError(f"备用公开索引连接失败：{last_error}")

    soup = BeautifulSoup(decode_response(response), "html.parser")
    candidates = []
    for link in soup.select(".result__a[href], a[href]"):
        href = str(link.get("href") or "").strip()
        parsed = urlparse(urljoin(search_url, href))
        candidate = parse_qs(parsed.query).get("uddg", [""])[0]
        if not candidate and parsed.hostname == "mp.weixin.qq.com":
            candidate = parsed.geturl()
        candidate = clean_url(candidate)
        if urlparse(candidate).hostname == "mp.weixin.qq.com" and candidate not in candidates:
            candidates.append(candidate)

    articles = []
    for candidate in candidates[:30]:
        ctx.checkpoint(task_id)
        try:
            html = ctx.fetch_html(candidate, {**request, "dynamic": False}, browser=ctx.browser)
        except Exception:
            continue
        article = BeautifulSoup(html, "html.parser")
        account = _text(article.select_one("#js_name, .profile_nickname, .account"))
        if re.sub(r"\s+", "", account).lower() != normalized_name:
            continue
        title = _text(article.select_one("#activity-name, meta[property='og:title'], h1"))
        published_at = _text(article.select_one("#publish_time, time"))
        if not published_at:
            timestamp = re.search(r"(?:var\s+)?ct\s*=\s*['\"](\d{9,13})", html)
            if timestamp:
                published_at = timestamp.group(1)
        articles.append({
            "url": candidate,
            "detail_url": candidate,
            "prefill": {
                "title": title,
                "author": account,
                "account": account,
                "published_at": format_beijing_datetime(published_at),
                "url": candidate,
            },
        })
        if len(articles) >= maximum:
            break
    if not articles:
        raise RuntimeError(f"备用公开索引也没有找到“{account_name}”的可验证原文")
    return articles


def discover(account_name, ctx):
    """使用主、备用公开索引发现文章，避免单一搜索入口波动中断采集。"""
    request, task_id = ctx.request, ctx.task_id
    cache_file = (ctx.data_dir / task_id / "wechat_discovery_cache.json"
                  if task_id and ctx.data_dir else None)
    # 公众号定时任务若每几分钟直接重查公开索引，很容易触发出口 IP 验证。
    # 近期成功结果可安全复用；真正的索引刷新保持至少 30 分钟间隔。
    if cache_file and int(request.get("max_items") or 50) == -1:
        try:
            cached = json.loads(cache_file.read_text("utf-8"))
            if time.time() - float(cached.get("fetched_at") or 0) < 1800:
                articles = cached.get("articles") or []
                if articles:
                    ctx.update(task_id, status="running", progress=10,
                               message="正在复用近期索引结果，避免频繁访问触发验证")
                    return articles
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            pass
    try:
        articles = sogou_urls(account_name, ctx)
    except (RuntimeError, requests.RequestException) as primary_error:
        try:
            articles = mobile_urls(account_name, ctx)
        except (RuntimeError, requests.RequestException) as mobile_error:
            try:
                articles = public_search_urls(account_name, ctx)
            except (RuntimeError, requests.RequestException) as fallback_error:
                raise RuntimeError(
                    "公众号公开索引均不可用。"
                    f"桌面入口：{primary_error}；移动入口：{mobile_error}；"
                    f"通用备用入口：{fallback_error}"
                ) from fallback_error
    if cache_file and articles:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"fetched_at": time.time(), "articles": articles},
                                        ensure_ascii=False), "utf-8")
        temporary.replace(cache_file)
    return articles
