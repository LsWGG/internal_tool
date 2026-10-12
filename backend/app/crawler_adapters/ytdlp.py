"""yt-dlp 通用视频源：任意 yt-dlp 支持的站点链接与播放列表/合集。

与 youtube.py 分工相同：这里只做发现与元数据抽取，真正的下载落在管理器的
`_download_youtube_video`（格式选择、代理轮换、断点重试都在那边）。字段名与
YouTube 一致，因此下载路径可以复用 youtube.fill_row 填表。
"""
from __future__ import annotations

import http.cookiejar
import re

from ..crawler_fields import BUILTIN_FIELDS
from . import youtube

URL_PATTERN = re.compile(r"https?://[^\s，。；、）)】\]]+", re.I)

# yt-dlp 的报错带着终端配色（`\x1b[0;31mERROR:\x1b[0m ...`）。管理器的下载路径
# 早就剥了一遍，这里漏了，于是用户看到的报错里全是 `[0;31m` 这种转义残渣。
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# 抖音分享出来的链接几乎都是「频道页 + modal_id=视频id」，而 yt-dlp 的 Douyin
# 解析器只认 /video/<id>。不还原的话，用户从 App 里复制出来的链接一律 Unsupported URL。
_DOUYIN_MODAL = re.compile(r"https?://(?:[\w-]+\.)*douyin\.com/[^?#]*\?[^#]*\bmodal_id=(\d+)", re.I)


def normalize_url(url):
    """把抖音的 modal_id 分享链接还原成 yt-dlp 认识的 /video/<id> 地址。"""
    match = _DOUYIN_MODAL.match(str(url or "").strip())
    return f"https://www.douyin.com/video/{match.group(1)}" if match else url


def clean_error(message):
    """yt-dlp 的异常文本 → 一行能直接给用户看的说明。"""
    text = _ANSI.sub("", str(message)).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return re.sub(r"^ERROR:\s*", "", lines[-1])[:240] if lines else ""


# yt-dlp 的解析器几乎都只认「单条内容」的地址，而账号主页是最容易被粘进来的一种链接。
# 落到 Generic 只有一句 Unsupported URL：既没点明是链接形态不对，也没说该去哪，
# 用户只能反复试。这里按链接形态补一句能直接照做的下一步。
_PROFILE_HINTS = (
    (re.compile(r"https?://(?:www\.|m\.)?(?:x|twitter)\.com/"
                r"(?!(?:i|home|explore|notifications|messages|search|settings|compose)(?:/|$))"
                r"[A-Za-z0-9_]{1,15}/?$", re.I),
     "这是 X / 推特的账号主页。yt-dlp 只能解析单条推文（…/status/数字）；"
     "要采集账号资料，请把「数据来源」改选为「推特 / X」并用「用户」模式；"
     "要下载视频，请粘贴那条推文的链接。"),
)


def link_hint(url):
    """链接形态不对时给一句说明；没有对应建议就返回空串。"""
    text = str(url or "").strip()
    for pattern, hint in _PROFILE_HINTS:
        if pattern.match(text):
            return hint
    return ""


def install_cookies(downloader, cookie_header):
    """把扫码登录存下的抖音 cookie 灌进 yt-dlp 的 cookie jar。

    必须写 jar，不能只给 `http_headers={'Cookie': ...}`：DouyinIE 判断登录态走的是
    `_get_cookies()`，读的就是 jar，头部里的 Cookie 它看不见。直接写 jar 也免去了
    把会话落成临时文件。
    """
    from ..douyin_profiles import _cookie_values  # 与抖音来源共用同一个头部解析
    for name, value in _cookie_values(cookie_header).items():
        downloader.cookiejar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, "douyin.com", False, False, "/", True,
            False, None, True, None, None, {}))

# 站点清单在运行时从本机安装的 yt-dlp 里读（见 supported_sites），官方地址只作为补充入口：
# 官方列表随 yt-dlp 版本更新，这里列的是「当前装着的这版」能解析什么。
OFFICIAL_URL = "https://github.com/yt-dlp/yt-dlp"
OFFICIAL_SITES_URL = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"

# 常用站点：中文名 + 域名，在页面上点一下就把清单筛到这个站点。
# 这里只列名称不给示例链接：编出来的链接点了就报错，不如让用户贴自己的地址。
POPULAR_SITES = [
    ("YouTube", "youtube.com"),
    ("哔哩哔哩", "bilibili.com"),
    ("抖音", "douyin.com"),
    ("TikTok", "tiktok.com"),
    ("小红书", "xiaohongshu.com"),
    ("微博", "weibo.com"),
    ("快手", "kuaishou.com"),
    ("西瓜视频", "ixigua.com"),
    ("优酷", "youku.com"),
    ("爱奇艺", "iqiyi.com"),
    ("腾讯视频", "v.qq.com"),
    ("芒果 TV", "mgtv.com"),
    ("AcFun", "acfun.cn"),
    ("Vimeo", "vimeo.com"),
    ("Twitch", "twitch.tv"),
    ("X / 推特", "x.com"),
    ("Instagram", "instagram.com"),
    ("Facebook", "facebook.com"),
    ("SoundCloud", "soundcloud.com"),
    ("Bandcamp", "bandcamp.com"),
    ("Reddit", "reddit.com"),
    ("Dailymotion", "dailymotion.com"),
    ("TED", "ted.com"),
    ("Niconico", "nicovideo.jp"),
]

# 从解析器自带的 _VALID_URL 正则里抠域名。正则里的转义点和 (?x) 空白先还原成普通文本，
# 否则 `youtube\.com` 这种写法一个域名都匹配不到。
_DOMAIN = re.compile(
    r"([a-z0-9][a-z0-9.-]*\.(?:com|cn|tv|net|org|me|io|co|ru|jp|kr|de|fr|uk|us|info|xyz|app|cc|to|fm|"
    r"live|video|site|top|club|pro|se|nl|it|es|pl|in|id|tw|hk|sg|my|ph|vn|th|mx|br|ar|cl|tr|ir|sa|"
    r"ae|za|au|nz|edu|gov|moe|dev|sh|ws|gg|im|art|blog|page|news|press|media|studio|online|store|fun|"
    r"space|world|today|life|one|plus|run|red|be|eu|asia))(?![a-z0-9.])", re.I)


# 形如 com.au / co.uk 的公共后缀：单看后缀不是站点域名。
_PUBLIC_SUFFIX = re.compile(r"(?:com|co|net|org|gov|edu|ac)\.[a-z]{2}", re.I)


def _strings(value):
    """_VALID_URL 有时是字符串，有时是字符串列表。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _domains(extractor):
    """解析器正则 → 域名集合；抠不出域名的（域名由分支拼出来的）就留空。"""
    found = set()
    for raw in _strings(getattr(extractor, "_VALID_URL", "") or ""):
        text = re.sub(r"\s+", "", raw).replace("\\", "")
        for host in _DOMAIN.findall(text):
            host = host.lower().lstrip(".").removeprefix("www.")
            # `10(?:play)?\.com\.au` 这种域名中间带分支的正则，从左往右能匹配到的只有
            # `com.au`。真域名识别不出来就算了，不能把公共后缀当站点域名列出去。
            if len(host.split(".")[0]) >= 2 and not _PUBLIC_SUFFIX.fullmatch(host):
                found.add(host)
    return found


def _primary_domains(name, domains):
    """镜像站会把一个站点的域名撑到几十个（YouTube 就带了一串 invidious 镜像）。

    能按站点名筛就先筛——「YouTube」只留含 youtube 的那几个；筛不出来的原样保留，
    宁可多列也不要把真正的域名丢掉。
    """
    letters = re.sub(r"[^a-z0-9]", "", name.lower())
    matched = sorted(item for item in domains if len(letters) >= 4 and letters in item.replace(".", ""))
    return matched or sorted(domains)


def supported_sites():
    """列出当前安装的 yt-dlp 能解析的站点。

    名称和域名都取自 yt-dlp 自己的解析器注册表，所以装了什么版本就列什么，
    不会和官方文档对不上，也不需要联网。清单在一次进程内不会变，算一次就缓存。
    """
    global _SITES_CACHE
    if _SITES_CACHE is None:
        payload = _build_supported_sites()
        # 只缓存成功的清单：yt-dlp 没装时不缓存，装好之后下一次请求就能拿到真正的列表。
        if payload["available"]:
            _SITES_CACHE = payload
        return payload
    return _SITES_CACHE


_SITES_CACHE = None


def _build_supported_sites():
    payload = {"available": True, "count": 0, "sites": [],
               "popular": [{"label": label, "domain": domain} for label, domain in POPULAR_SITES],
               "official_url": OFFICIAL_URL, "official_sites_url": OFFICIAL_SITES_URL}
    try:
        from yt_dlp.extractor import gen_extractors
    except ImportError:
        payload["available"] = False
        payload["message"] = "yt-dlp 未安装，暂时无法列出支持的站点。"
        return payload

    merged = {}
    for extractor in gen_extractors():
        name = str(getattr(extractor, "IE_NAME", "") or "")
        key = name.split(":")[0].strip()
        if not key:
            continue
        # 说明文字只认不带冒号的主解析器：变体（youtube:likes、vimeo:likes 之类）的说明
        # 只讲自己那一小块，拿来当整站说明会变成「Vimeo user likes」这种以偏概全的话。
        # 主解析器没写说明就原样用名字，不拿变体的顶上。
        entry = merged.setdefault(key, {"name": key, "label": key, "domains": set()})
        description = "" if ":" in name else str(getattr(extractor, "IE_DESC", "") or "").strip()
        if description and entry["label"] == key:
            entry["label"] = description
        entry["domains"] |= _domains(extractor)

    sites = [{"name": entry["name"], "label": entry["label"],
              "domains": _primary_domains(entry["name"], entry["domains"])} for entry in merged.values()]
    sites.sort(key=lambda item: (item["label"] or item["name"]).lower())
    payload["count"] = len(sites)
    payload["sites"] = sites
    return payload


def _maximum(request):
    """一次任务最多解析/下载多少条；-1 表示不限量（仍受 500 条上限保护）。"""
    requested = int(request.get("max_items") or 50)
    return 10**9 if requested < 0 else min(500, max(1, requested))


def target_urls(value, request):
    """从输入框和 urls 字段里取出链接，按出现顺序去重。"""
    raw = list(value) if isinstance(value, (list, tuple)) else [value]
    raw.extend(request.get("urls") or [])
    urls = []
    for item in raw:
        for found in URL_PATTERN.findall(str(item or "")):
            found = found.strip().rstrip(",;")
            if found and found not in urls:
                urls.append(found)
    return urls


def _entry_url(entry, playlist):
    """flat 条目还原成可下载地址。

    flat 模式下多数解析器会给 webpage_url；YouTube 只给 11 位视频 id，
    这时按 watch 地址还原。还原不出来的条目直接跳过，宁可少下也不猜地址。
    """
    for key in ("webpage_url", "url"):
        candidate = str(entry.get(key) or "").strip()
        if candidate.startswith("http"):
            return candidate
    identifier = str(entry.get("id") or entry.get("url") or "").strip()
    extractor = str(playlist.get("extractor_key") or playlist.get("extractor") or "").lower()
    if "youtube" in extractor and re.fullmatch(r"[\w-]{11}", identifier):
        return f"https://www.youtube.com/watch?v={identifier}"
    return ""


def extract(url, request, flat=False):
    """交给 yt-dlp 解析元数据（不下载）。"""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp 视频下载组件未安装，请先安装 yt-dlp") from exc
    options = {"quiet": True, "no_warnings": True, "skip_download": True,
               "color": "never",
               "socket_timeout": 30, "retries": 3, "playlistend": _maximum(request),
               "extract_flat": "in_playlist" if flat else False,
               # 合集链接要展开成里面的视频，否则只会拿到第一个。
               "noplaylist": False}
    proxies = request.get("proxies") or []
    if proxies:
        proxy = str(proxies[0]).strip()
        options["proxy"] = proxy if "://" in proxy else "http://" + proxy
    cookie = str(request.get("douyin_cookie") or "").strip()
    use_cookies = bool(cookie) and "douyin.com" in url
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            if use_cookies:
                install_cookies(downloader, cookie)
            return downloader.extract_info(url, download=False) or {}
    except Exception as exc:
        detail = clean_error(exc) or exc.__class__.__name__
        # 只有「解析器压根不认这个链接」时才补形态说明：其余报错（风控、超时、地区限制）
        # 的下一步各不相同，套用同一句建议只会把人带偏。
        hint = link_hint(url) if "Unsupported URL" in detail else ""
        message = f"yt-dlp 暂时无法解析该链接：{detail}"
        raise RuntimeError(f"{message}。{hint}" if hint else message) from exc


def expand(urls, request, maximum, update=None, task_id=None):
    """把播放列表/频道/合集链接展开成单条视频地址。"""
    if update and task_id:
        update(task_id, status="running", message="正在解析播放列表", progress=8)
    targets, seen = [], set()
    for url in urls:
        info = extract(url, request, flat=True)
        entries = info.get("entries") or [info]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video = _entry_url(entry, info)
            if not video or video in seen:
                continue
            seen.add(video)
            targets.append(video)
            if len(targets) >= maximum:
                return targets
    return targets


def row_from_info(info):
    """与 YouTube 同一套列，下载路径填表也走它。"""
    return youtube.fill_row({key: "" for key in BUILTIN_FIELDS["ytdlp"]}, info or {})


def resolve_targets(request, urls, maximum, update=None, task_id=None):
    """按模式把输入解析成单条视频地址：videos 原样用，playlist 先展开。"""
    # 规范化放在这个漏斗里：预览走 request["urls"]、任务走 target_urls()，两条路都经过它。
    urls = [normalize_url(url) for url in list(urls)][:maximum]
    if str(request.get("ytdlp_mode") or "videos").lower() != "playlist":
        return urls
    expanded = expand(urls, request, maximum, update=update, task_id=task_id)
    if not expanded:
        raise RuntimeError("未从该链接解析到可下载的视频，请确认链接可公开访问")
    return expanded


def preview_targets(request, urls, maximum=3):
    """把已解析好的地址取回元数据，做成带 prefill 的结果行。

    预览和「不下载视频文件」都走这里：两条路径都只要元数据，不落地文件。
    """
    rows = []
    for url in list(urls)[:maximum]:
        info = extract(url, request)
        row = row_from_info(info)
        row["url"] = info.get("webpage_url") or url
        rows.append({"url": url, "detail_url": "", "prefill": row})
    return rows


def discover(value, ctx):
    """按 ytdlp_mode 发现目标：逐条视频链接，或展开播放列表。"""
    request = ctx.request
    maximum = _maximum(request)
    urls = target_urls(value, request)
    if not urls:
        raise ValueError("请输入视频或播放列表链接，每行一个")
    urls = resolve_targets(request, urls, maximum, update=ctx.update, task_id=ctx.task_id)
    if not request.get("download_videos", True):
        # 取消勾选「下载视频文件」时只采元数据；行经 prefill 直接落表，不去爬页面 DOM。
        return preview_targets(request, urls, maximum)
    return urls
