"""新闻网站数据源：列表页文章识别、分页推导与整站发现。

原先散在 CrawlerTaskManager 上的同名方法整体搬到这里，逻辑未变；
`_article_links`、`_next_news_page` 本来就是纯函数，只是不再挂在实例上。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..crawler_browser import browser_scope
from .util import clean_url, decode_response, same_site, text as _text


def article_links(page_url, soup, selector=""):
    """从新闻列表识别详情链接，排除栏目、翻页、登录和静态资源链接。"""
    if selector:
        selectors = [selector]
    elif urlparse(page_url).netloc.lower().removeprefix("www.") == "cn.nytimes.com":
        # 该站的栏目正文位于 sectionWrapper；推荐榜虽然也使用标题标签，
        # 但在此容器之外，不能作为当前栏目文章采集。
        selectors = [
            ".sectionWrapper h1 a[href]", ".sectionWrapper h2 a[href]",
            ".sectionWrapper h3 a[href]", ".sectionWrapper h4 a[href]",
            # 服务端返回的精简 HTML 偶尔没有 sectionWrapper；通用选择器
            # 作为降级，热门榜仍由 URL 和祖先容器规则排除。
            "article a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]",
            ".story a[href]", ".story-body a[href]", "li a[href]",
        ]
    else:
        selectors = [
            "main article a[href]", "main h1 a[href]", "main h2 a[href]", "main h3 a[href]",
            "[role='main'] article a[href]", "[role='main'] h2 a[href]", "[role='main'] h3 a[href]",
            "article a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]",
            ".news-list a[href]", ".news_list a[href]", ".article-list a[href]",
            ".article_list a[href]", ".list a[href]", ".content-list a[href]",
            "li a[href]",
        ]
    anchors = []
    for item in selectors:
        try:
            anchors.extend(soup.select(item))
        except Exception:
            continue
    ignored_text = re.compile(r"^(首页|上一页|下一页|末页|更多|登录|注册|next|previous|prev|more|\d+)$", re.I)
    ignored_path = re.compile(r"\.(?:jpg|jpeg|png|gif|svg|webp|css|js|pdf|zip|mp4)(?:$|\?)", re.I)
    # 列表页的侧栏和页脚经常也使用 article/list 等类名。它们不是当前
    # 栏目的文章，若不排除会造成“配置 50 条，第一页只凑出几十条”的假象。
    ignored_sections = re.compile(
        r"/(?:mostviewed|most-viewed|popular|recommended|recommend|topic|topics|"
        r"slideshow|slideshows|interactive|video|videos|tag|tags)(?:/|$)", re.I,
    )
    found, seen = [], set()
    for anchor in anchors:
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = clean_url(urljoin(page_url, href))
        if url in seen or url == clean_url(page_url) or not same_site(page_url, url) or ignored_path.search(url):
            continue
        text = _text(anchor)
        if not text or ignored_text.match(text.strip()):
            continue
        parsed = urlparse(url)
        path = parsed.path.lower()
        if ignored_sections.search(path) or re.search(
                r"(?:^|[?&])utm_(?:source|campaign)=.*(?:most.?viewed|popular)",
                parsed.query, re.I):
            continue
        score = 0
        if len(text.strip()) >= 8:
            score += 1
        if anchor.find_parent(["article", "h1", "h2", "h3"]):
            score += 2
        parent_classes = " ".join(sum((node.get("class", []) for node in anchor.parents if getattr(node, "attrs", None)), []))[:800].lower()
        if re.search(r"sidebar|footer|nav(?:igation)?|most.?viewed|popular|hot.?story|recommend|related|pagination", parent_classes):
            continue
        if re.search(r"news|article|story|post|item|title|headline|content|list", parent_classes):
            score += 1
        if re.search(r"/(?:20\d{2}[/_-]\d{1,2}|20\d{6}|news|article|story|post|content)/", path):
            score += 2
        if re.search(r"\.(?:s?html?|aspx?)$", path):
            score += 1
        if parsed.query and re.search(r"(?:^|&)(?:page|p|start|offset)=\d+", parsed.query, re.I):
            score -= 2
        if score >= 2:
            seen.add(url)
            found.append(url)
    return found


def next_news_page(page_url, soup, selector=""):
    candidates = []
    if selector:
        try:
            candidates.extend(soup.select(selector))
        except Exception:
            pass
    candidates.extend(soup.select("a[rel='next'], link[rel='next']"))
    for anchor in soup.select("a[href]"):
        if re.fullmatch(r"\s*(?:下一页|下页|后页|next|next page)\s*(?:[>›»]+)?\s*|\s*[>›»]+\s*", _text(anchor), re.I):
            candidates.append(anchor)
    # 有些站点只显示页码，不提供 rel=next 或“下一页”文本。仅在明确的
    # 分页容器内寻找大于当前页的最小页码，避免误把正文数字当作分页。
    current_match = re.search(r"/page/(\d+)(?:/|$)", urlparse(page_url).path, re.I)
    current_number = int(current_match.group(1)) if current_match else 1
    numbered = []
    for anchor in soup.select(
            ".pagination a[href], .pager a[href], .paging a[href], nav[aria-label*='pag'] a[href]"):
        text = _text(anchor).strip()
        if text.isdigit() and int(text) > current_number:
            numbered.append((int(text), anchor))
    if numbered:
        candidates.append(min(numbered, key=lambda item: item[0])[1])
    # 兼容 /栏目/2/ 形式的纯数字分页。候选 URL 必须严格位于当前栏目
    # 路径的下一层，防止把文章日期或导航数字误判为页码。
    parsed_page = urlparse(page_url)
    page_path = parsed_page.path
    short_match = re.search(r"/(\d+)/?$", page_path)
    short_current = int(short_match.group(1)) if short_match else 1
    short_base = page_path[:short_match.start()] + "/" if short_match else page_path.rstrip("/") + "/"
    short_numbered = []
    for anchor in soup.select("a[href]"):
        label = _text(anchor).strip()
        if not label.isdigit() or int(label) <= short_current:
            continue
        candidate = clean_url(urljoin(page_url, anchor.get("href")))
        candidate_path = urlparse(candidate).path
        if re.fullmatch(re.escape(short_base) + r"\d+/", candidate_path):
            short_numbered.append((int(label), anchor))
    if short_numbered:
        candidates.append(min(short_numbered, key=lambda item: item[0])[1])
    for node in candidates:
        href = node.get("href")
        if href:
            url = clean_url(urljoin(page_url, href))
            if same_site(page_url, url) and url != clean_url(page_url):
                return url
    # 目录型新闻栏目常采用 /section/page/N/，但首页不渲染可识别的
    # 下一页控件。纽约时报中文网等站点即需要此回退。后续页无文章时，
    # 发现循环在没有新增文章或出现重复页面时停止。
    parsed = urlparse(page_url)
    path = parsed.path
    match = re.search(r"/page/(\d+)(/?)$", path, re.I)
    if match:
        next_path = path[:match.start(1)] + str(int(match.group(1)) + 1) + path[match.end(1):]
    elif (parsed.netloc.lower().removeprefix("www.") == "cn.nytimes.com"
          and (short_match := re.search(r"/(\d+)(/?)$", path))):
        next_path = (path[:short_match.start(1)] + str(int(short_match.group(1)) + 1)
                     + path[short_match.end(1):])
    elif path.endswith("/") and not re.search(r"\.(?:s?html?|aspx?)$", path, re.I):
        if parsed.netloc.lower().removeprefix("www.") == "cn.nytimes.com":
            next_path = f"{path}2/"
        else:
            next_path = f"{path}page/2/"
    else:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, next_path, parsed.params, parsed.query, ""))


def listing_html(url, request, browser=None, http=None):
    """新闻列表优先尝试浏览器滚动，以兼容无限滚动；不可用时退回普通请求。"""
    try:
        with browser_scope(browser, request.get("proxies")) as engine:
            # 这个调用点原本既不带桌面 UA 也不带语言，保持原样。
            context = engine.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                stable = 0
                previous_height = 0
                while True:
                    for label in ("加载更多", "查看更多", "更多新闻", "Load more"):
                        button = page.get_by_text(label, exact=False).last
                        try:
                            if button.is_visible(timeout=150):
                                button.click(timeout=1000)
                                break
                        except Exception:
                            pass
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)
                    height = page.evaluate("document.body.scrollHeight")
                    stable = stable + 1 if height == previous_height else 0
                    previous_height = height
                    if stable >= 2:
                        break
                    maximum = int(request.get("max_items") or 50)
                    if maximum > 0 and len(article_links(
                            url, BeautifulSoup(page.content(), "html.parser"),
                            request.get("article_link_selector", ""))) >= maximum:
                        break
                return page.content()
            finally:
                context.close()
    except Exception:
        return decode_response(http.get(url, request.get("proxies", [])))


def discover(seeds, ctx):
    request, task_id = ctx.request, ctx.task_id
    maximum = int(request.get("max_items") or 50)
    if maximum < 0: maximum = 10**9
    queue, visited, articles = list(seeds), set(), []
    while queue and len(articles) < maximum:
        page_url = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        ctx.checkpoint(task_id)
        ctx.update(task_id, status="running", message=f"正在查找新闻文章 · 第 {len(visited)} 页", progress=10)
        try:
            listing_html_text = ctx.fetch_listing_html(page_url, request, browser=ctx.browser)
        except Exception:
            # 第一页失败应正常报告错误；推导出的后续页不存在或临时失败时，
            # 保留此前已经发现的文章，避免整项任务丢失。
            if articles:
                break
            raise
        soup = BeautifulSoup(listing_html_text, "html.parser")
        previous_count = len(articles)
        for url in article_links(page_url, soup, request.get("article_link_selector", "")):
            if url not in articles:
                articles.append(url)
                if len(articles) >= maximum:
                    break
        # 推导出的分页若被站点重定向回栏目首页，会得到完全相同的链接；
        # 此时停止当前分页链，继续处理其他栏目。
        if len(articles) == previous_count:
            continue
        next_page = next_news_page(page_url, soup, request.get("next_page_selector", ""))
        if next_page and next_page not in visited:
            queue.append(next_page)
    if not articles:
        raise RuntimeError("没有在列表页中识别到新闻详情。请确认输入的是公开新闻列表页，或在高级设置中填写新闻链接规则。")
    return articles[:maximum]
