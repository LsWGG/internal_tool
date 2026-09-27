"""外链工具（首页设置弹窗里的 CRUD、以及内嵌打开的外链工具页）的浏览器端到端。

前提和 `test_ui_catalog.py` 一样：dev server 在 5174。**后端不需要** —— 本套件自己起一个
私有实例（127.0.0.1:8002，数据根在临时目录里现场生成），因为这里要验证的正是
「在弹窗里写进去之后首页怎么变」这一整条链路。

第一条断言（后端读不到时首页仍是 6 组 18 卡）与 `test_ui_catalog.py` 是同一个场景：
那边把全部 `/api/**` abort 掉，所以那条护栏在那边也一直站着，这边只是再明确验一次。

用 307 跳转而不是 `route.fetch` 转发：与 `test_clean_browser.py` 同一个理由（那条路上
POST / DELETE + CORS 预检都已经跑通过，少一份意外）。只转 `/api/links`，其余接口断掉 ——
App.vue 里那个 2 秒全局轮询与本套件无关，既不打扰本机真实后端，也不往日志里灌 404。

外链工具的地址指向本机 **8003** 上的一个小站（`ProbeSite`）：保存时后端会真去读它的响应头
来判断「允不允许被嵌入」，所以这里必须有一个真能连上、并且真的会发那几个头的站点 ——
拿 `wiki.example.com` 这种域名当靶子的话，检测永远是「没问到」，那三个分支就一条都验不到。

用法：python test_links_browser.py
"""
import http.server
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright

DEV = "http://127.0.0.1:5174"
API = 8002
SITE = 8003
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

NAME = "内部 Wiki"
RENAMED = "内部 Wiki 2"
OK_URL = f"http://127.0.0.1:{SITE}/embed-ok"
TYPED = OK_URL
BLOCKED_NAME = "拒绝嵌入站"
BLOCKED_URL = f"http://127.0.0.1:{SITE}/blocked"
DEAD_NAME = "连不上的站"
DEAD_URL = "http://127.0.0.1:1/"        # 没人监听 → 立刻连接被拒 → 检测「没问到」


class ProbeSite:
    """嵌入检测的靶子：两个只有响应头不一样的极小页面，跑在 8003 上。

    `/embed-ok` 什么都不声明（能嵌）；`/blocked` 声明 `frame-ancestors 'self'`（不让嵌）。
    检测是**后端**在保存时发起的真请求，所以这些头必须真的从 HTTP 上过来。
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b"<!doctype html><title>probe site</title><p>probe site</p>"
            self.send_response(200 if self.path in ("/embed-ok", "/blocked") else 404)
            if self.path == "/blocked":
                self.send_header("Content-Security-Policy",
                                 "default-src *; frame-ancestors 'self'")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def __init__(self, port=SITE):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), self.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def links_of() -> list:
    """直接问私有实例：弹窗写进去的东西必须真的落库，不能只看首页渲染。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{API}/api/links") as response:
        return json.load(response)["links"]


def cards(page):
    return page.locator(".catalog-card")


def groups(page):
    return page.locator(".catalog-group")


def open_settings(page):
    page.get_by_role("button", name="设置").click()
    page.get_by_role("dialog").wait_for(timeout=10000)


def close_settings(page):
    # 删除的二次确认是另一层遮罩，它还在 DOM 里（那 180ms 的淡出）时 Escape 该让给它，
    # 所以先等它真的走掉再按。不在 DOM 里时这一行立刻返回。
    page.locator(".confirm-card").wait_for(state="detached", timeout=5000)
    page.keyboard.press("Escape")
    page.get_by_role("dialog").wait_for(state="detached", timeout=10000)


def fill_form(page, name=None, description=None, url=None):
    page.get_by_role("button", name="添加外链工具").click()
    form = page.locator(".settings-form")
    form.wait_for(timeout=10000)
    if name is not None:
        form.get_by_label("名称", exact=True).fill(name)
    if description is not None:
        form.get_by_label("说明", exact=True).fill(description)
    if url is not None:
        form.get_by_label("地址", exact=True).fill(url)
    # 分类留默认：新建时默认就是「外链工具」（下拉的默认项），这条路不碰原生下拉


def save(page):
    page.locator(".settings-form-actions button.primary").click()
    page.locator(".settings-form").wait_for(state="detached", timeout=30000)


def phase_empty_home(page):
    """后端还没写任何数据时，首页与改动前完全一样：6 组 18 卡。"""
    groups(page).first.wait_for(timeout=30000)
    assert groups(page).count() == 6, f"空表时分组数变了：{groups(page).count()}"
    assert cards(page).count() == 18, f"空表时卡片数变了：{cards(page).count()}"
    assert page.get_by_role("button", name="设置").is_visible()


def phase_dialog(page):
    """设置图标 → 弹窗；Esc 关掉，焦点回图标（弹窗是纯新增的一块，别把键盘用户困住）。"""
    open_settings(page)
    dialog = page.get_by_role("dialog")
    assert "系统设置" in dialog.inner_text()
    tab = dialog.locator(".settings-tabs button")
    # 两个 TAB：「外链工具」仍是第一个，也仍是默认打开的那个（这个套件的其余部分都建立在
    # 「打开弹窗看到的就是外链工具」上）。第二个是工具分类。
    assert tab.count() == 2, tab.count()
    assert [tab.nth(i).inner_text() for i in range(2)] == ["外链工具", "工具分类"]
    assert tab.first.get_attribute("aria-current") == "true"
    assert tab.nth(1).get_attribute("aria-current") is None
    assert "还没有外链工具" in dialog.inner_text()
    close_settings(page)
    focused = page.evaluate("document.activeElement && document.activeElement.getAttribute('aria-label')")
    assert focused == "设置", f"关掉弹窗后焦点没回到设置图标，而是在 {focused!r}"


def phase_bad_url(page):
    """`javascript:` 地址必须被挡在弹窗里 —— 这个值会被写进 iframe 的 src。"""
    open_settings(page)
    fill_form(page, name="坏地址", url="javascript:alert(1)")
    page.locator(".settings-form-actions button.primary").click()
    error = page.locator(".settings-error")
    error.wait_for(timeout=30000)
    assert "http" in error.inner_text(), error.inner_text()
    assert page.get_by_role("dialog").is_visible(), "报错后弹窗不该关掉"
    assert not links_of(), "被拒的地址居然写进了后端"
    close_settings(page)
    assert cards(page).count() == 18, "被拒的外链工具出现在首页了"


def phase_create(page):
    open_settings(page)
    fill_form(page, name=NAME, description="团队知识库", url=TYPED)
    # LOGO 预览跟着名称首字实时变（只读，没有自定义入口）
    assert page.locator(".settings-preview .settings-logo").inner_text() == "内"
    save(page)
    assert "已保存" in page.locator(".settings-note").inner_text()
    row = page.locator(".settings-list li").filter(has_text=NAME)
    row.wait_for(timeout=10000)
    assert "外链工具" in row.inner_text(), row.inner_text()   # 默认分类
    stored = links_of()
    assert [item["name"] for item in stored] == [NAME]
    assert stored[0]["embeddable"] is True, stored[0]      # 保存时探过：这个站没声明不让嵌
    close_settings(page)

    assert groups(page).count() == 7, f"新增之后分组数：{groups(page).count()}"
    assert cards(page).count() == 19, f"新增之后卡片数：{cards(page).count()}"
    nav = page.locator(".catalog-nav").inner_text()
    assert "外链工具" in nav, nav
    # 排在最后：内置 6 组的顺序不动（4K 首屏布局不变）
    assert page.locator(".catalog-group").last.locator("h2").inner_text() == "外链工具"
    card = cards(page).filter(has_text=NAME)
    icon = card.locator(".catalog-tool-icon")
    assert icon.inner_text() == "内", "LOGO 不是名称首字"
    assert "is-letter" in (icon.get_attribute("class") or ""), "首字瓦片没走外链工具那套样式"
    # 主题色是行内样式写上去的（色相由 id 哈希决定）。CSSOM 会把 hsl() 序列化成 rgb()，
    # 所以这里查「不是内置工具那片默认蓝」，而不是查某个颜色写法。
    assert "gradient" in (icon.get_attribute("style") or ""), icon.get_attribute("style")
    color = icon.evaluate("node => getComputedStyle(node).color")
    assert color != "rgb(74, 114, 196)", f"首字瓦片用的是默认蓝：{color}"
    assert f"127.0.0.1:{SITE}" in card.inner_text(), card.inner_text()
    return stored[0]["id"]


def phase_search(page):
    page.locator("#tool-search").fill("Wiki")
    page.wait_for_function("() => document.querySelectorAll('.catalog-card').length === 1")
    assert NAME in cards(page).first.inner_text()
    page.locator("#tool-search").fill("")
    page.wait_for_function("() => document.querySelectorAll('.catalog-card').length === 19")


def phase_open(page, link_id):
    """外壳是系统 UI，内容是 iframe。**只看 iframe 的属性，不进 iframe**（同源策略之外，
    里面是别人家的页面）。"""
    cards(page).filter(has_text=NAME).click()
    page.wait_for_function("(id) => location.hash === '#link:' + id", arg=link_id)
    page.locator(".link-page").wait_for(timeout=30000)
    # 地图那块是 v-show，`main h1` 会匹到两个 —— 按外链页自己的容器取
    assert page.locator(".link-page h1").inner_text() == NAME
    assert page.locator(".back-home").is_visible()
    frame = page.locator("iframe.link-frame")
    assert frame.count() == 1
    assert frame.get_attribute("src") == TYPED, frame.get_attribute("src")
    sandbox = frame.get_attribute("sandbox") or ""
    assert "allow-scripts" in sandbox and "allow-same-origin" in sandbox, sandbox
    # 「进来就是这个页面」：地址和「在新窗口打开」那一条都删了，页面上只剩系统页头 + iframe
    assert not page.locator(".link-toolbar").count(), "外链页又多出来一条工具栏"
    assert not page.locator(".link-note").count(), "外链页又多了一段说明文字"
    # 框要正好占满一屏：高了会多一条滚动条，矮了底下留一截空白。
    # 高度 = 100dvh - 280px（窄屏 -244），这两个数是量出来的，页头一改就得重量。
    box = page.evaluate("""() => {
      const f = document.querySelector('.link-frame').getBoundingClientRect();
      return {below: innerHeight - f.bottom, extra: document.documentElement.scrollHeight - innerHeight};
    }""")
    assert box["extra"] <= 0, f"外链页多出一条滚动条：{box}"
    assert box["below"] < 80, f"框底下空了一截：{box}"
    page.get_by_role("button", name="返回工具门户").click()
    page.locator(".catalog-page").wait_for(timeout=30000)


def phase_edit(page):
    open_settings(page)
    page.locator(".settings-list li").filter(has_text=NAME).get_by_role("button", name="编辑").click()
    form = page.locator(".settings-form")
    form.wait_for(timeout=10000)
    assert form.get_by_label("名称", exact=True).input_value() == NAME, "编辑没带出原值"
    form.get_by_label("名称", exact=True).fill(RENAMED)
    save(page)
    close_settings(page)
    page.wait_for_function(
        "(name) => [...document.querySelectorAll('.catalog-card')].some(node => node.textContent.includes(name))",
        arg=RENAMED)
    assert cards(page).count() == 19, "改名不该多出一张卡"
    assert [item["name"] for item in links_of()] == [RENAMED], "改名没落库"


def phase_embed_check(page):
    """保存时探测嵌入政策，三条路各验一遍：能嵌的照旧、不让嵌的直开新窗口、连不上的算未检测。

    这三个结论都不是前端猜的 —— 它们来自后端真去读的那几个响应头（8003 的裸 http.server），
    所以这一整段同时在验后端那条探测链路。
    """
    open_settings(page)

    # 1) 站点自己声明不让嵌：结论、依据、以及「点卡片会怎样」都要在弹窗里说清楚
    fill_form(page, name=BLOCKED_NAME, description="声明了 frame-ancestors", url=BLOCKED_URL)
    save(page)
    note = page.locator(".settings-note")
    note.wait_for(timeout=10000)
    assert "不允许被嵌入" in note.inner_text(), note.inner_text()
    assert "frame-ancestors 'self'" in note.inner_text(), note.inner_text()
    row = page.locator(".settings-list li").filter(has_text=BLOCKED_NAME)
    chip = row.locator(".settings-verdict")
    assert chip.inner_text() == "不可嵌入 · 新窗口", chip.inner_text()
    assert "frame-ancestors 'self'" in (chip.get_attribute("title") or ""), chip.get_attribute("title")
    assert links_of()[-1]["embeddable"] is False, links_of()[-1]

    # 「检测」按钮：单独把这一条再问一遍（结论不变，但这条路必须真的通）
    row.get_by_role("button", name="检测").click()
    note.wait_for(timeout=30000)
    page.wait_for_function("() => !document.querySelector('.settings-row-actions button:disabled')")
    assert "不允许被嵌入" in note.inner_text(), note.inner_text()

    # 2) 连不上的地址：探测问不到结论，就不能假装它是「可以嵌」
    fill_form(page, name=DEAD_NAME, url=DEAD_URL)
    save(page)
    note.wait_for(timeout=30000)
    assert "没能问到" in note.inner_text(), note.inner_text()
    assert page.locator(".settings-list li").filter(has_text=DEAD_NAME).locator(
        ".settings-verdict").inner_text() == "未检测"
    close_settings(page)

    # 不让嵌的那张卡：直接是「在新窗口打开」的链接，点它不该把门户本身带走
    assert cards(page).count() == 21, f"新增两条之后卡片数：{cards(page).count()}"
    blocked = cards(page).filter(has_text=BLOCKED_NAME)
    blocked.wait_for(timeout=10000)
    assert blocked.get_attribute("target") == "_blank", "不让嵌的卡片没标成新窗口"
    assert blocked.get_attribute("href") == BLOCKED_URL
    assert "新窗口" in blocked.inner_text(), blocked.inner_text()
    before = page.evaluate("location.hash")
    with page.expect_popup(timeout=20000) as popup_info:
        blocked.click()
    popup = popup_info.value
    popup.wait_for_load_state("domcontentloaded")
    assert popup.url.startswith(BLOCKED_URL), popup.url
    popup.close()
    assert page.evaluate("location.hash") == before, "点卡片把门户自己导航走了"
    assert page.locator(".catalog-page").is_visible()

    # 连不上那条按老样子走：进外壳页，照样挂 iframe（还有「检测」可以再试）
    dead = cards(page).filter(has_text=DEAD_NAME)
    assert dead.get_attribute("target") is None, "「未检测」不该被当成不让嵌"
    assert dead.get_attribute("href") == "#link:" + next(
        item["id"] for item in links_of() if item["name"] == DEAD_NAME)

    # 手输 / 直接点进不让嵌的那个 hash：给说明和按钮，不挂那个注定空白的 iframe
    blocked_id = next(item["id"] for item in links_of() if item["name"] == BLOCKED_NAME)
    page.evaluate("(id) => { location.hash = '#link:' + id }", blocked_id)
    panel = page.locator(".link-blocked")
    panel.wait_for(timeout=10000)
    assert f"127.0.0.1:{SITE}" in panel.inner_text(), panel.inner_text()
    assert "frame-ancestors 'self'" in panel.inner_text(), panel.inner_text()
    assert not page.locator("iframe.link-frame").count(), "明知不让嵌还挂了 iframe"
    open_link = page.locator(".link-open")
    assert open_link.get_attribute("href") == BLOCKED_URL
    assert open_link.get_attribute("target") == "_blank"
    page.get_by_role("button", name="返回工具门户").click()
    page.locator(".catalog-page").wait_for(timeout=30000)

    # 收尾：把这两条删掉，后面的段落接着用 19 张卡的前提
    open_settings(page)
    for name in (BLOCKED_NAME, DEAD_NAME):
        row = page.locator(".settings-list li").filter(has_text=name)
        row.get_by_role("button", name="删除").click()
        confirm = page.get_by_role("alertdialog")
        confirm.wait_for(timeout=10000)
        confirm.get_by_role("button", name="确认删除").click()
        # 删除的二次确认是另一层遮罩，等它淡出（180ms）再继续，否则下一次点击会撞上它
        page.locator(".confirm-card").wait_for(state="detached", timeout=5000)
        row.wait_for(state="detached", timeout=10000)
    close_settings(page)
    assert cards(page).count() == 19, f"删完之后卡片数：{cards(page).count()}"


def phase_delete(page):
    open_settings(page)
    page.locator(".settings-list li").filter(has_text=RENAMED).get_by_role("button", name="删除").click()
    # 二次确认是**另一层**遮罩。设置弹窗如果压在它上面，这一下会点不中（Playwright 的
    # 可操作性检查会报 intercepted），也就是用户看到的「点了删除没反应」。
    confirm = page.get_by_role("alertdialog")
    confirm.wait_for(timeout=10000)
    confirm.get_by_role("button", name="确认删除").click()
    page.wait_for_function("() => !document.querySelector('.settings-list li')")
    assert links_of() == [], "删了之后后端还留着"
    close_settings(page)
    assert groups(page).count() == 6, f"删完之后分组数：{groups(page).count()}"
    assert cards(page).count() == 18, f"删完之后卡片数：{cards(page).count()}"


def phase_missing(page):
    """手改 hash 指到一个不存在的外链工具：给空状态，不白屏。"""
    page.evaluate("location.hash = '#link:zzz'")
    page.locator(".link-empty").wait_for(timeout=10000)
    assert page.locator(".back-home").is_visible()
    assert not page.locator("iframe.link-frame").count()
    page.evaluate("location.hash = ''")
    page.locator(".catalog-page").wait_for(timeout=30000)
    assert cards(page).count() == 18


def main() -> int:
    # 检测靶子（8003）先起来：保存时后端会去读它的响应头，所以它必须在链路里
    with tempfile.TemporaryDirectory(prefix="links-browser-") as raw, ProbeSite():
        data = Path(raw) / "data"

        from app.links_api import make_router

        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=[DEV], allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])
        app.include_router(make_router(data))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=API, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, f"私有实例没起来（端口 {API} 被占了？）"

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000},
                                        reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def redirect(route):
                    url = route.request.url
                    if "/api/links" in url:
                        route.fulfill(status=307, headers={
                            "Location": url.replace("127.0.0.1:5174", f"127.0.0.1:{API}")})
                    else:
                        route.abort()

                # 只拦开发服务器那个源。拦宽了会把 307 的目标再拦一遍 → 死循环
                page.route("http://127.0.0.1:5174/api/**", redirect)
                page.goto(f"{DEV}/#home", wait_until="domcontentloaded")
                page.wait_for_timeout(1200)

                # 每段单独包一层并报出段落名：Playwright 的报错是一大段调用栈，
                # 读不出「卡在哪一段」，而这里九段的失败原因完全不同。
                seen = {}
                steps = [
                    ("空表首页", lambda: phase_empty_home(page)),
                    ("设置弹窗", lambda: phase_dialog(page)),
                    ("坏地址", lambda: phase_bad_url(page)),
                    ("新增", lambda: seen.update(id=phase_create(page))),
                    ("搜索", lambda: phase_search(page)),
                    ("进入外链页", lambda: phase_open(page, seen["id"])),
                    ("改名", lambda: phase_edit(page)),
                    ("嵌入检测", lambda: phase_embed_check(page)),
                    ("删除", lambda: phase_delete(page)),
                    ("不存在的 id", lambda: phase_missing(page)),
                ]
                for name, call in steps:
                    print(f"--- {name}", flush=True)
                    try:
                        call()
                    except Exception as exc:
                        raise AssertionError(f"【{name}】{exc}") from exc

                # 窄屏：面板 + 长地址都在弹窗里，这里最容易横向溢出的正是它
                open_settings(page)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(400)
                overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
                assert overflow <= 0, f"390px 打开设置弹窗时横向溢出 {overflow}px"
                close_settings(page)
                overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
                assert overflow <= 0, f"390px 首页横向溢出 {overflow}px"

                assert not (data / "links.json").exists() or links_of() == []
                assert not errors, errors
                print("PASS: 空表 6 组 18 卡 / 设置图标与 Esc / 焦点回图标 / 坏地址被挡在弹窗里"
                      " / 新增（默认分类·第 7 组·19 卡·首字 LOGO·主题色） / 搜索命中 / 卡片进外链页"
                      " （hash·系统外壳·iframe src·无工具栏·框占满一屏） / 改名落库"
                      " / 嵌入检测（能嵌·不让嵌→卡片直开新窗口·连不上→未检测·「检测」按钮"
                      "·不让嵌的 hash 落地页无 iframe） / 删除（两层遮罩点得中）"
                      " / 不存在的 id 走空状态 / 390 宽不溢出 / 零 pageerror")
                page.unroute_all(behavior="wait")
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
