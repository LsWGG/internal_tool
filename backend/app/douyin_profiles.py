"""Read Douyin profiles from its browser-rendered public user search."""
import base64
import logging
import os
import random
import re
import string
import time
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlencode, urlparse

import requests

from .crawler_browser import browser_scope
from .crawler_http import UA_DESKTOP_FULL, UA_DOUYIN
from .douyin_xbogus import XBogus

try:
    from .douyin_abogus import ABogus, BrowserFingerprintGenerator
except ImportError:  # 没装 gmssl 就退回旧签名，采集降级但整个模块不能因此 import 不了
    ABogus = BrowserFingerprintGenerator = None


_DOUYIN_UA = UA_DOUYIN

logger = logging.getLogger(__name__)


def _cookie_values(cookie_header):
    """Parse a browser Cookie header without logging or persisting its values."""
    values = {}
    for item in str(cookie_header or '').split(';'):
        key, sep, value = item.strip().partition('=')
        if sep and re.fullmatch(r'[!#$%&\'*+.^_`|~0-9A-Za-z-]+', key):
            values[key] = value.strip()
    return values


def _fallback_ms_token():
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(182)) + '=='


def _profile_query(sec_uid, cookies):
    """The browser query shape used by douyin-downloader's API client."""
    return {
        'device_platform': 'webapp', 'aid': '6383', 'channel': 'channel_pc_web',
        'update_version_code': '170400', 'pc_client_type': '1', 'pc_libra_divert': 'Windows',
        'version_code': '290100', 'version_name': '29.1.0', 'cookie_enabled': 'true',
        'screen_width': '1536', 'screen_height': '864', 'browser_language': 'zh-CN',
        'browser_platform': 'Win32', 'browser_name': 'Chrome', 'browser_version': '139.0.0.0',
        'browser_online': 'true', 'engine_name': 'Blink', 'engine_version': '139.0.0.0',
        'os_name': 'Windows', 'os_version': '10', 'cpu_core_num': '16', 'device_memory': '8',
        'platform': 'PC', 'downlink': '10', 'effective_type': '4g', 'round_trip_time': '200',
        'support_h265': '1', 'support_dash': '1', 'uifid': '',
        'msToken': cookies.get('msToken') or _fallback_ms_token(), 'sec_user_id': sec_uid,
    }


def _douyin_host(value):
    hostname = (urlparse(str(value or '')).hostname or '').lower()
    return hostname == 'douyin.com' or hostname.endswith('.douyin.com')


def profile_link_rows(items, target):
    """Build an exact-match fallback from rendered user-search links."""
    target = str(target or '').strip().lstrip('@')
    found = {}
    for item in items or []:
        href = str(item.get('href') or '').strip()
        parsed = urlparse(href)
        match = re.match(r'^/user/([^/?#]+)', parsed.path)
        if not _douyin_host(href) or not match:
            continue
        uid = unquote(match.group(1))
        text = str(item.get('text') or '')
        lines = [line.strip().lstrip('@') for line in text.splitlines() if line.strip()]
        if target != uid and target not in lines:
            continue
        found[uid] = {
            'username': uid, 'display_name': target, 'bio': '',
            'followers': '', 'following': '', 'videos': '', 'likes': '',
            'verified': '', 'avatar': '',
            'url': f'https://www.douyin.com/user/{uid}',
        }
    return list(found.values())


# 抖音把人机验证（滑块拼图）弹在搜索结果的位置上。它的文案里**没有**「验证码」
# 三个字，也没提「登录」，只认这几句原话，否则会把「要求验证」误判成「搜不到账号」，
# 让用户去反复换昵称和主页链接 —— 那条路永远走不通。
_VERIFY_MARKS = ('请完成下列验证', '完成验证后继续', '拖动完成', '安全验证', '验证码')
# 滑块本身渲染在 rmc.bytedance.com 的 iframe 里。弹出验证时主文档正文几乎被清空
# （实测只剩 4 个字符），光看正文一定漏判，iframe 才是可靠信号。
_VERIFY_FRAME_MARKS = ('verifycenter', '/captcha')


def _needs_verify(page):
    """页面是不是抖音的风控挑战。先认 iframe，再退回正文文案。"""
    for frame in page.frames:
        url = str(getattr(frame, 'url', '') or '')
        if any(mark in url for mark in _VERIFY_FRAME_MARKS):
            return True
    try:
        body = page.locator('body').inner_text(timeout=5000)
    except Exception:
        return False
    return any(mark in str(body or '') for mark in _VERIFY_MARKS)


def profile_index_rows(items, target):
    """Use a public search-index result only when its title names the exact account."""
    target = str(target or '').strip().lstrip('@')
    found = {}
    for item in items or []:
        href = str(item.get('href') or '').strip()
        parsed = urlparse(href)
        # DuckDuckGo/Bing often wrap the real result in a redirect query parameter.
        for key in ('uddg', 'url', 'u', 'q'):
            values = parse_qs(parsed.query).get(key) or []
            if values and _douyin_host(unquote(values[0])):
                href = unquote(values[0])
                parsed = urlparse(href)
                break
        # Bing's result redirect stores the real URL as URL-safe base64 in
        # ``u=a1...``. Decode it locally; no redirect needs to be followed.
        encoded = (parse_qs(parsed.query).get('u') or [''])[0]
        if encoded.startswith('a1'):
            try:
                decoded = base64.urlsafe_b64decode(encoded[2:] + '=' * (-len(encoded[2:]) % 4)).decode()
                if _douyin_host(decoded):
                    href, parsed = decoded, urlparse(decoded)
            except (ValueError, UnicodeDecodeError):
                pass
        match = re.match(r'^/user/([^/?#]+)', parsed.path)
        title = re.sub(r'\s+', ' ', str(item.get('text') or '')).strip().lstrip('@')
        # A result such as "人民日报 - 抖音" is safe; a longer name such as
        # "人民日报健康客户端" must remain ambiguous and not be substituted.
        exact_title = re.match(rf'^{re.escape(target)}(?:的抖音\s*[-|｜·•—–]|\s*[-|｜·•—–]|\s*$)', title)
        if not _douyin_host(href) or not match or not exact_title:
            continue
        uid = unquote(match.group(1))
        found[uid] = {
            'username': uid, 'display_name': target, 'bio': '',
            'followers': '', 'following': '', 'videos': '', 'likes': '',
            'verified': '', 'avatar': '', 'url': f'https://www.douyin.com/user/{uid}',
        }
    return list(found.values())


def _search_index_links(context, target):
    """Find an exact public Douyin profile result when on-site search is blocked."""
    links = []
    page = context.new_page()
    try:
        # A plain account query is indexed much more reliably than a strict
        # ``site:`` query, especially for Douyin's client-rendered profile URLs.
        query = quote_plus(f'{target} 抖音号')
        for url in (f'https://www.bing.com/search?q={query}&count=20',):
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(1000)
                links.extend(page.locator('a[href]').evaluate_all(
                    "nodes => nodes.map(node => ({href: node.href, text: node.innerText || node.textContent || ''}))"
                ))
            except Exception:
                continue
            if profile_index_rows(links, target):
                break
    finally:
        page.close()
    return links


def profile_rows(payload, target):
    """Only accept exact names/IDs; never substitute a similarly named account."""
    # 用户输入通常来自 ``@人民日报``，而搜索接口有时会返回带 @ 或空白的值。
    target = str(target or '').strip().lstrip('@')
    found = {}
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            uid = str(value.get('sec_uid') or value.get('secUid') or value.get('sec_user_id') or '')
            names = [str(value.get(key) or '').strip().lstrip('@') for key in (
                'nickname', 'unique_id', 'uniqueId', 'short_id', 'shortId',
                'sec_uid', 'secUid', 'sec_user_id',
            )]
            if uid and target in names:
                avatar = (value.get('avatar_larger') or value.get('avatarLarger') or
                          value.get('avatar_medium') or value.get('avatarMedium') or
                          value.get('avatar_thumb') or value.get('avatarThumb') or {})
                urls = avatar.get('url_list') or [] if isinstance(avatar, dict) else []
                row = {'username': value.get('unique_id') or value.get('uniqueId') or value.get('short_id') or value.get('shortId') or uid,
                       'display_name': value.get('nickname') or '', 'bio': value.get('signature') or '',
                       'followers': value.get('follower_count', value.get('followerCount', '')),
                       'following': value.get('following_count', value.get('followingCount', '')),
                       'videos': value.get('aweme_count', value.get('awemeCount', '')),
                       'likes': value.get('total_favorited', value.get('totalFavorited', '')),
                       'verified': value.get('enterprise_verify_reason') or value.get('custom_verify') or '',
                       'avatar': urls[0] if urls else '', 'url': 'https://www.douyin.com/user/' + uid}
                previous = found.get(uid, {})
                found[uid] = {key: val if val != '' else previous.get(key, '') for key, val in row.items()}
            for item in value.values():
                if isinstance(item, (list, dict)):
                    walk(item)
    walk(payload)
    return list(found.values())


def _signed_url(endpoint, query):
    """给接口 URL 签名，返回 (完整 URL, 配套的 User-Agent)。

    a_bogus 才是现在抖音认的签名。旧的 X-Bogus 会被风控直接判定成机器请求：
    实测同一个搜索请求，X-Bogus 返回 search_nil_info 的 antispam_check /
    hit_shark，换成 a_bogus 才会走到正常的业务分支。所以只有在 abogus 模块
    整个不可用（没装 gmssl）时才退回 X-Bogus —— 那条路大概率拿不到数据，
    但至少不会让采集整体崩掉。
    """
    if ABogus is not None:
        try:
            # 指纹和 UA 必须跟请求头里发出去的那份一致，否则签名自相矛盾。
            fingerprint = BrowserFingerprintGenerator.generate_fingerprint('Chrome')
            signer = ABogus(fp=fingerprint, user_agent=_DOUYIN_UA)
            signed, _abogus, ua, _body = signer.generate_abogus(query, '')
            return f'{endpoint}?{signed}', ua
        except Exception as exc:
            logger.warning('生成 a_bogus 失败，本次退回 X-Bogus：%s', exc)
    signed_url, _, ua = XBogus(_DOUYIN_UA).build(f'{endpoint}?{query}')
    return signed_url, ua


def enrich_profile(row, request):
    """Fill restricted profile fields using a signed request and the user's logged-in session.

    This follows the request shape used by the local douyin-downloader project:
    full browser query parameters, msToken and X-Bogus.  The session is never
    retained beyond this request path and failures intentionally leave public
    profile data untouched.
    """
    cookie_header = str((request or {}).get('douyin_cookie') or os.getenv('DOUYIN_COOKIE') or '').strip()
    if not cookie_header or not row.get('url'):
        return row
    cookies = _cookie_values(cookie_header)
    if not cookies:
        return row
    sec_uid = unquote(urlparse(row['url']).path.rsplit('/', 1)[-1])
    query = _profile_query(sec_uid, cookies)
    endpoint = 'https://www.douyin.com/aweme/v1/web/user/profile/other/'
    session = requests.Session()
    session.cookies.update(cookies)
    headers = {
        'User-Agent': _DOUYIN_UA, 'Referer': row['url'], 'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    payload = {}
    try:
        # Fresh signatures are important: an empty 200 response is a known
        # Douyin risk-control response, so retry once with another signature.
        for attempt in range(2):
            signed_url, signed_ua = _signed_url(endpoint, urlencode(query))
            response = session.get(signed_url, headers={**headers, 'User-Agent': signed_ua}, timeout=20)
            if response.content:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
            if isinstance(payload, dict) and payload.get('user'):
                break
            if attempt == 0:
                time.sleep(.3)
    except requests.RequestException:
        return row
    finally:
        session.close()
    user = payload.get('user') or payload.get('user_info') or {}
    stats = payload.get('user_stats') or payload.get('stats') or {}
    if not isinstance(user, dict) or str(user.get('sec_uid') or user.get('secUid') or '') != sec_uid:
        return row
    avatar = user.get('avatar_larger') or user.get('avatarLarger') or user.get('avatar_medium') or user.get('avatarMedium') or {}
    avatar_urls = avatar.get('url_list') or [] if isinstance(avatar, dict) else []
    return {**row,
            'username': user.get('unique_id') or user.get('uniqueId') or user.get('short_id') or user.get('shortId') or row['username'],
            'display_name': user.get('nickname') or row['display_name'],
            'bio': user.get('signature') or '',
            'followers': stats.get('follower_count', stats.get('followerCount', user.get('follower_count', user.get('followerCount', '')))),
            'following': stats.get('following_count', stats.get('followingCount', user.get('following_count', user.get('followingCount', '')))),
            'videos': stats.get('aweme_count', stats.get('awemeCount', user.get('aweme_count', user.get('awemeCount', '')))),
            'likes': stats.get('total_favorited', stats.get('totalFavorited', user.get('total_favorited', user.get('totalFavorited', '')))),
            'verified': user.get('enterprise_verify_reason') or user.get('custom_verify') or user.get('verified') or '',
            'avatar': avatar_urls[0] if avatar_urls else row.get('avatar', '')}


def discover_profiles(value, request, browser=None):
    accounts = list(dict.fromkeys(x.strip().lstrip('@') for x in re.split(r'[\n,，;；]+', str(value)) if x.strip()))
    targets = []
    # 搜索页对未登录访客只渲染导航空壳，连搜索接口都不会请求，所以表单里
    # 填了 cookie 就必须带进浏览器上下文 —— 否则用户永远搜不到自己的账号。
    cookies = _cookie_values(request.get('douyin_cookie'))
    with browser_scope(browser, request.get('proxies')) as engine:
        context = engine.new_context(
            user_agent=UA_DESKTOP_FULL,
            locale='zh-CN', viewport={'width': 1440, 'height': 1000},
        )
        if cookies:
            # new_context 不收 cookies 参数，只能建好上下文再灌。
            context.add_cookies([{'name': name, 'value': value, 'domain': '.douyin.com', 'path': '/'}
                                 for name, value in cookies.items()])
        try:
            page = context.new_page()
            for account in accounts:
                parsed = urlparse(account)
                if parsed.scheme:
                    if not _douyin_host(account) or not parsed.path.startswith('/user/'):
                        raise ValueError('请输入抖音账号名称或完整的抖音用户主页链接')
                    name = parsed.path.split('/')[2]
                    url = 'https://www.douyin.com/user/' + quote(name, safe='')
                else:
                    name = account
                    url = 'https://www.douyin.com/search/' + quote(name, safe='') + '?type=user'
                rows = {}
                def receive(response):
                    if not _douyin_host(response.url):
                        return
                    try:
                        for row in profile_rows(response.json(), name):
                            rows[row['url']] = row
                    except Exception:
                        pass
                page.on('response', receive)
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=45000)
                    # 搜索页是个 SPA：domcontentloaded 只代表导航壳出来了，搜索应用的
                    # JS 包（合计十几 MB）下完才开始发请求。实测慢网络下要 40 秒开外，
                    # 等 10 秒只会看到一片空骨架屏，然后误报成「搜不到账号」。
                    for tick in range(120):
                        page.wait_for_timeout(500)
                        if rows:
                            break
                        # 每 5 秒瞟一眼有没有弹验证。弹了就别再耗满 60 秒。
                        if tick % 10 == 9 and _needs_verify(page):
                            break
                    if not rows:
                        links = page.locator('a[href*="/user/"]').evaluate_all(r'''(elements, target) => elements.map(link => {
                            let node = link;
                            let text = '';
                            for (let depth = 0; depth < 5 && node; depth++, node = node.parentElement) {
                                const candidate = (node.innerText || '').trim();
                                const lines = candidate.split(/\n+/).map(value => value.trim().replace(/^@/, ''));
                                if (lines.includes(target) && (!text || candidate.length < text.length)) text = candidate;
                            }
                            return {href: link.href, text};
                        })''', name)
                        for row in profile_link_rows(links, name):
                            rows[row['url']] = row
                    if not rows and not parsed.scheme:
                        for row in profile_index_rows(_search_index_links(context, name), name):
                            rows[row['url']] = row
                    # 完整主页链接已经由用户明确指定。即使抖音隐藏了动态
                    # 资料，也返回最小可预览记录，不把有效主页误报为搜索失败。
                    if not rows and parsed.scheme:
                        uid = unquote(parsed.path.split('/')[2])
                        row = profile_link_rows([{'href': url, 'text': uid}], uid)[0]
                        rows[row['url']] = row
                    if not rows:
                        body = page.locator('body').inner_text(timeout=5000)
                        # 人机验证要排在登录判断前面：验证弹窗盖住页面时正文里
                        # 也可能有「登录」字样，先撞上那句提示会给出完全错误的指引。
                        # 这是抖音对当前网络/设备的判定，换昵称、换主页链接都没用，
                        # 只能由人在真实浏览器里把滑块拖过去。
                        if _needs_verify(page):
                            raise RuntimeError(
                                f'抖音账号“{name}”：抖音要求人机验证（滑块拼图），'
                                '自动采集无法继续。请在浏览器里打开抖音、完成一次搜索并'
                                '拖过滑块，验证通过后稍等片刻再重试；'
                                'Cookie 无效或本机网络被判定为异常都会触发这个验证。')
                        # 未登录时页面只渲染导航栏那个「登录」入口，不会出现
                        # 「扫码登录」这类更强的字样，判定词必须包括它；这时该
                        # 做的是补 cookie，而不是让用户去猜昵称和用户 ID 的区别。
                        if any(x in body for x in ('扫码登录', '登录')):
                            hint = ('当前填写的抖音 Cookie 未能通过校验，请更新后重试'
                                    if cookies else '抖音搜索用户需要登录，请先填写抖音 Cookie 再重试')
                            raise RuntimeError(f'抖音账号“{name}”：{hint}。')
                        raise RuntimeError(f'抖音账号“{name}”：公开用户搜索没有返回可读取的匹配账号。'
                                           '可尝试填写完整用户主页链接；昵称不能直接当作用户 ID。')
                    if len(rows) > 1:
                        raise ValueError(f'抖音存在多个名为“{name}”的账号，请填写目标账号的完整主页链接以避免采错')
                    targets.extend({'url': row['url'], 'detail_url': '', 'prefill': enrich_profile(row, request)} for row in rows.values())
                finally:
                    page.remove_listener('response', receive)
        finally:
            context.close()
    return targets


def _request_payload(path, params, request, referer='https://www.douyin.com/'):
    """Request one Douyin web API page using the downloader's signed browser shape."""
    cookie_header = str((request or {}).get('douyin_cookie') or os.getenv('DOUYIN_COOKIE') or '').strip()
    cookies = _cookie_values(cookie_header)
    query = _profile_query('', cookies)
    query.pop('sec_user_id', None)
    query.update(params or {})
    endpoint = 'https://www.douyin.com' + path
    session = requests.Session()
    if cookies:
        session.cookies.update(cookies)
    headers = {'User-Agent': _DOUYIN_UA, 'Referer': referer, 'Accept': '*/*',
               'Accept-Language': 'zh-CN,zh;q=0.9'}
    try:
        for attempt in range(2):
            signed_url, signed_ua = _signed_url(endpoint, urlencode(query))
            response = session.get(signed_url, headers={**headers, 'User-Agent': signed_ua}, timeout=20)
            if response.content:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if isinstance(payload, dict):
                    return payload
            if attempt == 0:
                time.sleep(.3)
    except requests.RequestException:
        return {}
    finally:
        session.close()
    return {}


def aweme_row(item):
    """Normalize the aweme object returned by search/post endpoints."""
    item = item or {}
    author = item.get('author') if isinstance(item.get('author'), dict) else {}
    stats = item.get('statistics') if isinstance(item.get('statistics'), dict) else {}
    video = item.get('video') if isinstance(item.get('video'), dict) else {}
    cover = video.get('cover') if isinstance(video.get('cover'), dict) else {}
    covers = cover.get('url_list') or []
    aweme_id = str(item.get('aweme_id') or item.get('id') or '')
    return {
        'title': item.get('desc') or '', 'description': item.get('desc') or '',
        'author': author.get('nickname') or '',
        'username': author.get('unique_id') or author.get('short_id') or author.get('sec_uid') or '',
        'published_at': item.get('create_time') or '',
        'duration': (video.get('duration') or 0) / 1000 if video.get('duration') else '',
        'views': stats.get('play_count', ''), 'likes': stats.get('digg_count', ''),
        'comments': stats.get('comment_count', ''), 'shares': stats.get('share_count', ''),
        'thumbnail': covers[0] if covers else '',
        'url': f'https://www.douyin.com/video/{aweme_id}' if aweme_id else '',
    }


def _raise_if_risk_controlled(payload):
    """被风控拦下时抛出真实原因，别让调用方去猜「是不是关键词没结果」。

    抖音拦截时返回的是 HTTP 200 + 空结果，只在 search_nil_info 里留下判据。
    实测被拦时任何关键词都返回同一份判定 —— 连完全不存在的乱码词也一样 ——
    所以看到 verify_check / antispam_check 就不该再往 Cookie 上归因。
    """
    nil = payload.get('search_nil_info') if isinstance(payload, dict) else None
    nil_type = str((nil or {}).get('search_nil_type') or '')
    if nil_type == 'verify_check':
        raise RuntimeError('抖音要求人机验证（滑块拼图），自动采集无法继续。'
                           '请在浏览器里打开抖音、完成一次搜索并拖过滑块，验证通过后再重试。')
    if nil_type == 'antispam_check':
        raise RuntimeError('抖音把本机请求判定成了异常流量（风控拦截）。请稍后重试；'
                           '若持续失败，多半是当前网络出口被限制，换个网络通常能恢复。')


def search_awemes(keyword, request, maximum):
    """Search Douyin videos using the same endpoint and params as douyin-downloader."""
    rows, seen, offset = [], set(), 0
    while len(rows) < maximum:
        payload = _request_payload('/aweme/v1/web/general/search/single/', {
            'keyword': keyword, 'search_channel': 'aweme_video_web', 'sort_type': 0,
            'publish_time': 0, 'search_source': 'normal_search', 'query_correct_type': '1',
            'is_filter_search': 0, 'offset': offset, 'count': min(20, maximum - len(rows)),
        }, request, 'https://www.douyin.com/search/' + quote(keyword, safe=''))
        if not rows:
            # 已经翻到过内容就别因为后面被拦而把前面的结果丢掉。
            _raise_if_risk_controlled(payload)
        entries = payload.get('data') if isinstance(payload.get('data'), list) else []
        for entry in entries:
            item = entry.get('aweme_info') if isinstance(entry, dict) else None
            aweme_id = str(item.get('aweme_id') or '') if isinstance(item, dict) else ''
            if item and aweme_id and aweme_id not in seen:
                seen.add(aweme_id); rows.append(aweme_row(item))
        has_more = str(payload.get('has_more') or '0') in ('1', 'true', 'True')
        next_offset = payload.get('cursor', payload.get('offset', 0))
        try: next_offset = int(next_offset)
        except (TypeError, ValueError): next_offset = 0
        if not has_more or not entries or next_offset <= offset:
            break
        offset = next_offset
    return rows


def user_awemes(sec_uid, request, maximum):
    """Read a public account's videos with cursor pagination."""
    rows, seen, cursor = [], set(), 0
    while len(rows) < maximum:
        payload = _request_payload('/aweme/v1/web/aweme/post/', {
            'sec_user_id': sec_uid, 'max_cursor': cursor, 'count': min(18, maximum - len(rows)),
            'locate_query': 'false', 'show_live_replay_strategy': '1', 'need_time_list': '1',
            'time_list_query': '0', 'whale_cut_token': '', 'cut_version': '1',
            'publish_video_strategy_type': '2',
        }, request, f'https://www.douyin.com/user/{sec_uid}')
        if not rows:
            _raise_if_risk_controlled(payload)
        items = payload.get('aweme_list') if isinstance(payload.get('aweme_list'), list) else []
        for item in items:
            aweme_id = str(item.get('aweme_id') or '') if isinstance(item, dict) else ''
            if item and aweme_id and aweme_id not in seen:
                seen.add(aweme_id); rows.append(aweme_row(item))
        has_more = str(payload.get('has_more') or '0') in ('1', 'true', 'True')
        try: next_cursor = int(payload.get('max_cursor') or 0)
        except (TypeError, ValueError): next_cursor = 0
        if not has_more or not items or next_cursor == cursor:
            break
        cursor = next_cursor
    return rows


def aweme_comments(aweme_id, video_url, request, maximum):
    """Read Douyin comments with the reference project's cursor request parameters."""
    rows, seen, cursor = [], set(), 0
    while len(rows) < maximum:
        payload = _request_payload('/aweme/v1/web/comment/list/', {
            'aweme_id': aweme_id, 'cursor': cursor, 'count': min(20, maximum - len(rows)),
            'item_type': '0', 'insert_ids': '', 'whale_cut_token': '', 'cut_version': '1', 'rcFT': '',
        }, request, video_url)
        if not rows:
            _raise_if_risk_controlled(payload)
        items = payload.get('comments') if isinstance(payload.get('comments'), list) else []
        for item in items:
            user = item.get('user') if isinstance(item.get('user'), dict) else {}
            cid = str(item.get('cid') or item.get('comment_id') or '')
            if not cid or cid in seen: continue
            seen.add(cid)
            rows.append({'author': user.get('nickname') or '',
                         'username': user.get('unique_id') or user.get('short_id') or '',
                         'text': item.get('text') or '', 'published_at': item.get('create_time') or '',
                         'likes': item.get('digg_count', ''), 'replies': item.get('reply_comment_total', ''),
                         'url': f'{video_url}#comment-{cid}'})
        has_more = str(payload.get('has_more') or '0') in ('1', 'true', 'True')
        try: next_cursor = int(payload.get('cursor') or 0)
        except (TypeError, ValueError): next_cursor = 0
        if not has_more or not items or next_cursor == cursor: break
        cursor = next_cursor
    return rows
