"""Read Douyin profiles from its browser-rendered public user search."""
import re
from urllib.parse import quote, unquote, urlparse


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


def discover_profiles(value, request):
    from playwright.sync_api import sync_playwright
    accounts = list(dict.fromkeys(x.strip().lstrip('@') for x in re.split(r'[\n,，;；]+', str(value)) if x.strip()))
    targets = []
    with sync_playwright() as p:
        options = {'headless': True}
        proxies = request.get('proxies') or []
        if proxies:
            proxy = str(proxies[0])
            options['proxy'] = {'server': proxy if '://' in proxy else 'http://' + proxy}
        browser = p.chromium.launch(**options)
        try:
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/128.0.0.0 Safari/537.36'
                ),
                locale='zh-CN', viewport={'width': 1440, 'height': 1000},
            )
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
                    for _ in range(20):
                        page.wait_for_timeout(500)
                        if rows:
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
                    # 完整主页链接已经由用户明确指定。即使抖音隐藏了动态
                    # 资料，也返回最小可预览记录，不把有效主页误报为搜索失败。
                    if not rows and parsed.scheme:
                        uid = unquote(parsed.path.split('/')[2])
                        row = profile_link_rows([{'href': url, 'text': uid}], uid)[0]
                        rows[row['url']] = row
                    if not rows:
                        body = page.locator('body').inner_text(timeout=5000)
                        reason = '页面要求登录或访问验证' if any(x in body for x in ('扫码登录', '验证码', '安全验证')) else '公开用户搜索没有返回可读取的匹配账号'
                        raise RuntimeError(f'抖音账号“{name}”：{reason}。可尝试填写完整用户主页链接；昵称不能直接当作用户 ID。')
                    if len(rows) > 1:
                        raise ValueError(f'抖音存在多个名为“{name}”的账号，请填写目标账号的完整主页链接以避免采错')
                    targets.extend({'url': row['url'], 'detail_url': '', 'prefill': row} for row in rows.values())
                finally:
                    page.remove_listener('response', receive)
            context.close()
        finally:
            browser.close()
    return targets
