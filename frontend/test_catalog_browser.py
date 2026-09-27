"""工具分类（设置弹窗第二个 TAB：分类 CRUD、排序、工具/分类拖拽）的浏览器端到端。

前提和 `test_ui_catalog.py` 一样：dev server 在 5174。**后端不需要** —— 本套件自己起一个
私有实例（127.0.0.1:8004，数据根在临时目录里现场生成），因为要验的正是「在弹窗里改完，
首页跟着怎么变」这一整条链路。同一实例上还挂着 `/api/links`：外链的归属存在那张表里，
拖外链 chip 走的是它那条写路径，两个路由都得在。

用 307 跳转而不是 `route.fetch` 转发：与 `test_links_browser.py` / `test_clean_browser.py`
同一个理由。只转 `/api/links` 和 `/api/catalog`，其余接口一律断掉（App.vue 那个 2 秒轮询
与本套件无关）。**不引 8003 那个探测站**：本套件里的外链地址指向 127.0.0.1:1（没人监听
→ 立刻连接被拒 → 「未检测」），嵌入结论不是这里要验的东西。

两处刻意的写法：

  * 落库断言一律直接问私有实例（`catalog_of()`），不看首页渲染 —— 乐观更新会让界面先动，
    只看界面等于没验那条 POST。
  * 状态是**逐段累积**的（新增 → 拖 → 改名 → 删除），所以每段的断言都写成「相对上一段」，
    而且拖动一律带 `source_position` / `target_position`：`drag_to` 默认落在元素正中，
    分类行的落点判定是按中线分上下半的，正中会踩在边界上。

用法：python test_catalog_browser.py
"""
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
API = 8004
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# 代码里那 7 个分类的顺序（`toolCatalog.js` 的分组 + 末尾的「外链工具」）。
CODE_ORDER = ["geo", "database", "documents", "images", "collection", "delivery", "external"]
# 首页卡片数的两个基准：代码里 18 个内置工具；「外链 chip 换组」那一段会种下一条外链，
# 从那之后它就是第 19 张卡 —— 外链也是卡片，落在它归属的那个分组里（这里被挪进了「图片处理」），
# 所以分组数不变、卡片数 +1。
BUILTIN_CARDS = 18
CARDS_AFTER_SEED = BUILTIN_CARDS + 1
NEW_NAME = "我的分类"
NEW_DESC = "临时放一些常用工具"
SEED_NAME = "外部站点"
SEED_CATEGORY = "geo"          # 先挂在「地理空间」，再挪到「图片处理」
SEED_URL = "http://127.0.0.1:1/"   # 连不上 → 未检测；本套件不验嵌入结论


def get_json(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{API}{path}") as response:
        return json.load(response)


def post_json(path: str, payload: dict):
    request = urllib.request.Request(f"http://127.0.0.1:{API}{path}",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def catalog_of() -> dict:
    """私有实例上的分类表：**稀疏形态**（只有用户改过的字段），所以断言里到处是 in / get。"""
    return get_json("/api/catalog")["catalog"]


def links_of() -> list:
    return get_json("/api/links")["links"]


# main() 把 page 装进来。轮询的空档必须让 Playwright 的事件循环转起来，理由见 until。
PUMP = []


def until(predicate, timeout=10):
    """等一个落库条件成立。POST 是异步的，界面已经动了不等于盘上已经写了。

    **空档用 `page.wait_for_timeout` 而不是 `time.sleep`。** 这份套件用 `page.route` 把
    `/api/**` 307 到私有实例，而 route handler 是在 Playwright 的事件循环里跑的：`time.sleep`
    会把唯一的线程占住，循环停转 —— 恰好在这期间发出（或者还没轮到的）那个请求，handler
    永远不会被调用，于是它既不应答也不失败，就这么挂着，等多久都等不到盘上落库。这个坑很
    隐蔽：前面几段能过只是因为那个 POST 恰好在一次 Playwright 调用的间隙里处理完了。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if PUMP:
            PUMP[0].wait_for_timeout(100)
        else:
            time.sleep(.1)
    return predicate()


def cards(page):
    return page.locator(".catalog-card")


def groups(page):
    return page.locator(".catalog-group")


def nav(page):
    return page.locator(".catalog-nav button")


def open_settings(page):
    """打开设置。已经开着就直接用 —— 弹窗里的关闭按钮 aria-label 是「关闭设置」，
    而 `get_by_role` 的 name 默认按子串匹配，弹窗一开，那句 `name="设置"` 就会同时命中
    齿轮和关闭按钮（strict mode 直接报错），所以这里用 exact 并先看有没有开着。"""
    if page.get_by_role("dialog").count() == 0:
        page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("dialog").wait_for(timeout=10000)


def close_settings(page):
    page.locator(".confirm-card").wait_for(state="detached", timeout=5000)
    page.keyboard.press("Escape")
    page.get_by_role("dialog").wait_for(state="detached", timeout=10000)


def open_cats(page):
    """打开设置并切到「工具分类」。TAB 是 v-if/v-else 的，同一时刻只有一栏在 DOM 里。"""
    open_settings(page)
    page.get_by_role("button", name="工具分类").click()
    page.locator(".settings-cats").wait_for(timeout=10000)


def rows(page):
    return page.locator(".settings-cat-row")


def row_ids(page):
    return page.eval_on_selector_all(".settings-cat-row", "els => els.map(el => el.dataset.cat)")


def row_of(page, category_id):
    return page.locator(f'.settings-cat-row[data-cat="{category_id}"]')


def select_cat(page, category_id):
    """点左栏一行：右栏（表单 + chip 区）跟着换成它。"""
    row_of(page, category_id).click()
    page.locator(f'.settings-cat-row[data-cat="{category_id}"].is-active').wait_for(timeout=5000)


def chip_of(page, tool):
    return page.locator(f'.settings-chip[data-tool="{tool}"]')


def drag(page, source, target, *, source_at=None, target_at=None):
    """拖一次：手动 mouse 步骤，**不要用 locator.drag_to()**。

    `drag_to` 走的是 Playwright 的 CDP 拖拽拦截（`Input.setInterceptDrags`）。这台机器上
    拖**分类行**时它会在 dragstart 之后整个卡死：页面里只剩 dragstart/dragend，drop 永远
    不来，`drag_to` 也永不返回（实测卡过 10 分钟以上，只能杀进程）。手动 dispatchMouseEvent
    不触发那套拦截，走的是浏览器自己的拖拽循环 —— 和真人按住鼠标拖是同一条路，两种拖拽都稳。

    默认落在源元素的左上角附近（chip 上那里是图标，不是「移动到…」按钮，按在按钮上起不来
    拖拽）。
    """
    source.scroll_into_view_if_needed()
    target.scroll_into_view_if_needed()
    page.wait_for_timeout(80)
    source_at = source_at or {"x": 12, "y": 12}
    sbox, tbox = source.bounding_box(), target.bounding_box()
    # 默认落在目标下缘：分类行按中线分上下半，落点决定插到这一行之前还是之后
    target_at = target_at or {"x": tbox["width"] / 2, "y": tbox["height"] - 6}
    sx, sy = sbox["x"] + source_at["x"], sbox["y"] + source_at["y"]
    tx, ty = tbox["x"] + target_at["x"], tbox["y"] + target_at["y"]
    page.mouse.move(sx, sy)
    page.mouse.down()
    for index in range(1, 7):
        page.mouse.move(sx + (tx - sx) * index / 6, sy + (ty - sy) * index / 6)
        page.wait_for_timeout(30)
    page.mouse.up()


def drag_chip(page, tool, target_id):
    """拖一个工具 chip 到某个分类行上。"""
    drag(page, chip_of(page, tool), row_of(page, target_id))


def form_input(page, label):
    return page.locator(".settings-cat-form").get_by_label(label, exact=True)


def save_category(page):
    page.locator(".settings-cat-form button.primary").click()


def reopen_home(page):
    """**真的重新加载**首页，等目录出来。

    两处刻意的写法：
      * 不用 `page.reload()`：它默认 wait_until='load'，在这个环境永远不触发（这份套件不过
        /tmp/runtest.py 那层包装，卡住就是真卡住）。
      * 也不能直接 `goto("…/#home")`：已经在 `#home` 上再 goto 同一个地址是一次**同文档导航**
        （只有 hash 没变），浏览器不会重新加载 —— 页面不会重新拉 `/api/links`，于是「刚种下去
        的那条外链」在界面上永远不出现。所以每次带一个变化的查询串，强制真的走一次导航。
    """
    page.goto(f"{DEV}/?r={reopen_home.count}#home", wait_until="domcontentloaded")
    reopen_home.count += 1
    page.wait_for_timeout(1200)
    groups(page).first.wait_for(timeout=30000)


reopen_home.count = 0


def phase_empty(page):
    """什么都没改过时：首页与改动前逐字节相同，分类 TAB 是代码里那 7 个内置分类。"""
    groups(page).first.wait_for(timeout=30000)
    assert groups(page).count() == 6, f"空表时分组数变了：{groups(page).count()}"
    assert cards(page).count() == 18, f"空表时卡片数变了：{cards(page).count()}"

    open_cats(page)
    assert row_ids(page) == CODE_ORDER, row_ids(page)
    # 内置分类没有删除按钮，理由（为什么不能删）写在右栏它本来该在的位置上。
    assert rows(page).locator("button.danger").count() == 0
    select_cat(page, "geo")
    assert page.locator(".settings-cat-delete").count() == 0
    assert "不能删除" in page.locator(".settings-hint").inner_text()
    assert page.get_by_role("button", name="新建分类").is_visible()
    close_settings(page)


def phase_create(page):
    """新建分类：立刻落库（order 末尾），首页**不变**（空分类不上首页）。"""
    open_cats(page)
    page.get_by_role("button", name="新建分类").click()
    page.wait_for_function("document.querySelectorAll('.settings-cat-row').length === 8")
    form_input(page, "名称").fill(NEW_NAME)
    form_input(page, "说明").fill(NEW_DESC)
    page.locator(".settings-icon-choice").nth(5).click()
    icon = page.locator(".settings-icon-choice").nth(5).get_attribute("title")
    save_category(page)

    saved = until(lambda: len(catalog_of()["order"]) == 8)
    assert saved, f"新建后 order 没变成 8 项：{catalog_of()['order']}"
    new_id = catalog_of()["order"][-1]
    assert new_id not in CODE_ORDER, f"自建分类的 id 撞上了内置的：{new_id}"
    # 改名的 POST 是后发的，等它自己落到盘上再读记录（新建那条到了不等于改名那条也到了）
    assert until(lambda: catalog_of()["categories"].get(new_id, {}).get("name") == NEW_NAME), \
        catalog_of()["categories"]
    record = catalog_of()["categories"][new_id]
    assert record.get("description") == NEW_DESC, record
    assert record.get("icon") == icon, record
    # 自建分类有删除按钮，内置的没有 —— 两边的差别就在这一个按钮上。
    assert page.locator(".settings-cat-delete").is_visible()

    close_settings(page)
    assert groups(page).count() == 6, "空分类不该出现在首页"
    assert cards(page).count() == 18
    return new_id


def phase_drag_tool(page, new_id):
    """拖拽 1（工具 → 分类）：docker 从「开发交付」拖进「图片处理」。

    内置工具的落点存在 catalog.json 的 tools 里，稀疏：只有偏离代码默认才写。
    """
    open_cats(page)
    select_cat(page, "delivery")
    chip_of(page, "docker").wait_for(timeout=5000)
    drag_chip(page, "docker", "images")
    assert until(lambda: catalog_of()["tools"].get("docker") == "images"), catalog_of()["tools"]

    close_settings(page)
    assert groups(page).count() == 5, "搬空的「开发交付」该从首页消失"
    assert cards(page).count() == 18, "工具只是换了组，不该多也不该少"
    assert page.locator('[data-group="delivery"]').count() == 0
    assert page.locator('[data-group="images"] .catalog-card').count() == 3


def phase_drag_category(page):
    """拖拽 2（分类重排）：「采集与资讯」拖到「地理空间」上面 → 它成了第一组。

    落点按行的中线分上下半，所以 `y=4` 明确落在上半边（插到这一行**之前**）。
    """
    open_cats(page)
    drag(page, row_of(page, "collection"), row_of(page, "geo"), target_at={"x": 20, "y": 4})
    assert until(lambda: catalog_of()["order"][0] == "collection"), catalog_of()["order"]

    close_settings(page)
    assert nav(page).first.inner_text().startswith("采集与资讯"), nav(page).first.inner_text()
    assert groups(page).first.locator("h2").inner_text() == "采集与资讯"


def phase_hover(page):
    """悬停高亮：合成事件，不赌鼠标停在半空。

    Chromium 在 dragover 期间读不到 dataTransfer.getData()，所以高亮认的是组件里的 drag
    ref —— 这里顺便证明「只是高亮，没有落库」。
    """
    open_cats(page)
    select_cat(page, "images")
    page.evaluate("""() => {
      const chip = document.querySelector('.settings-chip[data-tool="image-convert"]');
      const row = document.querySelector('.settings-cat-row[data-cat="documents"]');
      const dt = new DataTransfer();
      window.__dragTransfer = dt;
      chip.dispatchEvent(new DragEvent('dragstart', {bubbles:true, cancelable:true, dataTransfer:dt}));
      row.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, dataTransfer:dt}));
    }""")
    classes = row_of(page, "documents").get_attribute("class")
    assert "is-drop-into" in classes, classes
    assert "image-convert" not in catalog_of()["tools"], "悬停不该落库"

    page.evaluate("""() => {
      const chip = document.querySelector('.settings-chip[data-tool="image-convert"]');
      chip.dispatchEvent(new DragEvent('dragend', {bubbles:true, cancelable:true,
                                                   dataTransfer:window.__dragTransfer}));
    }""")
    classes = row_of(page, "documents").get_attribute("class")
    assert "is-drop-into" not in classes, "dragend 之后高亮该清掉"
    close_settings(page)


def phase_rollback(page):
    """存不进去就还原：POST 被掐断时，chip 回原位、盘上不动、错说在弹窗里。"""
    page.route(f"{DEV}/api/catalog",
               lambda route: route.abort() if route.request.method == "POST" else route.fallback())
    try:
        open_cats(page)
        select_cat(page, "images")
        drag_chip(page, "image-convert", "documents")
        error = page.locator(".settings-error")
        error.wait_for(timeout=10000)
        assert "已还原" in error.inner_text(), error.inner_text()
        assert "image-convert" not in catalog_of()["tools"], "失败的移动不该落库"
        select_cat(page, "images")
        assert chip_of(page, "image-convert").count() == 1, "失败后 chip 该回到原来的分类里"
    finally:
        page.unroute(f"{DEV}/api/catalog")


def phase_link_chip(page):
    """外链 chip：归属存在 links.json 里，拖它走的是 `/api/links` 那条写路径。"""
    post_json("/api/links", {"links": [{"id": "", "name": SEED_NAME, "description": "",
                                        "url": SEED_URL, "category": SEED_CATEGORY}]})
    link = until(lambda: (links_of() or [None])[0])
    assert link and link["category"] == SEED_CATEGORY, link

    reopen_home(page)
    open_cats(page)
    select_cat(page, SEED_CATEGORY)
    chip = page.locator(f'.settings-chip[data-link="{link["id"]}"]')
    chip.wait_for(timeout=5000)
    assert SEED_NAME in chip.inner_text()

    # 走「移动到…」（非拖拽路径），这是 390 宽下唯一可用的那条
    chip.locator(".settings-chip-move").click()
    chip.locator("select.settings-chip-select").select_option("images")
    saved = until(lambda: links_of()[0]["category"] == "images")
    assert saved, f"外链的归属没写回去：{links_of()}"
    assert "已把" in page.locator(".settings-note").inner_text()
    return link["id"]


def phase_reload(page, new_id, link_id):
    """端到端：重新打开页面，改过的东西都还在（分类顺序、自建分类、工具落点、外链归属）。"""
    reopen_home(page)
    assert nav(page).first.inner_text().startswith("采集与资讯")
    # 图片处理里现在有四张卡：它自己的两个内置工具 + 从「开发交付」搬过来的 docker +
    # 从「地理空间」挪过来的那条外链。这里同时看数量与名字 —— 数量对而名字不对，
    # 说明是「卡片渲染出来了但内容串了」，那是另一回事。
    images = page.locator('[data-group="images"]')
    assert images.locator(".catalog-card").count() == 4, images.inner_text()
    assert "Docker 离线包" in images.inner_text() and SEED_NAME in images.inner_text()

    open_cats(page)
    assert row_ids(page)[0] == "collection" and row_ids(page)[-1] == new_id, row_ids(page)
    select_cat(page, "images")
    assert chip_of(page, "docker").count() == 1
    assert chip_of(page, f"link:{link_id}").count() == 1


def phase_move_and_rename(page, new_id):
    """两条不依赖拖拽的路径（↑/↓ 与「移动到…」）+ 改内置分类的名字。"""
    open_cats(page)
    # 移动到…：把 docker 送进自建分类
    select_cat(page, "images")
    chip = chip_of(page, "docker")
    chip.locator(".settings-chip-move").click()
    chip.locator("select.settings-chip-select").select_option(new_id)
    assert until(lambda: catalog_of()["tools"].get("docker") == new_id), catalog_of()["tools"]

    close_settings(page)
    assert cards(page).count() == CARDS_AFTER_SEED
    custom = page.locator(f'[data-group="{new_id}"]')
    assert custom.locator(".catalog-card").count() == 1
    assert "Docker 离线包" in custom.inner_text()
    assert custom.locator("h2").inner_text() == NEW_NAME

    # ↑ / ↓：先下移再上移，位置回到原样（顺带证明它是真的换了序，不是只动界面）
    open_cats(page)
    before = row_ids(page)
    rows(page).first.get_by_role("button", name="下移").click()
    assert until(lambda: catalog_of()["order"][0] != before[0]), catalog_of()["order"]
    after = row_ids(page)
    assert after[0] == before[1] and after[1] == before[0], (before, after)
    rows(page).nth(1).get_by_role("button", name="上移").click()
    assert until(lambda: catalog_of()["order"][0] == before[0]), catalog_of()["order"]

    # 改名内置分类：首页的导航与分组标题都跟着变（名字存在稀疏覆盖里）
    select_cat(page, "geo")
    form_input(page, "名称").fill("地理")
    save_category(page)
    assert until(lambda: catalog_of()["categories"].get("geo", {}).get("name") == "地理")
    close_settings(page)
    assert page.locator('[data-group="geo"] h2').inner_text() == "地理"
    assert nav(page).filter(has_text="地理").count() >= 1

    # 名字改回去：覆盖该消失（和代码默认一样就不该留在文件里）
    open_cats(page)
    select_cat(page, "geo")
    form_input(page, "名称").fill("地理空间")
    save_category(page)
    assert until(lambda: "geo" not in catalog_of()["categories"]), catalog_of()["categories"]
    close_settings(page)


def phase_delete(page, new_id):
    """删除自建分类：二次确认 → 里面的工具回原组 → 首页回到 6 组（卡片数见 CARDS_AFTER_SEED）。"""
    open_cats(page)
    select_cat(page, new_id)
    page.locator(".settings-cat-delete").click()
    confirm = page.locator(".confirm-card")
    confirm.wait_for(timeout=5000)
    text = confirm.inner_text()
    assert NEW_NAME in text and "1 个内置工具" in text, text
    confirm.locator(".confirm-btn").click()

    assert until(lambda: new_id not in catalog_of()["order"]), catalog_of()["order"]
    assert new_id not in catalog_of()["categories"]
    assert "docker" not in catalog_of()["tools"], "指向被删分类的落点该一起清掉"

    close_settings(page)
    assert groups(page).count() == 6, groups(page).count()
    assert cards(page).count() == CARDS_AFTER_SEED
    assert page.locator('[data-group="delivery"]').count() == 1, "「开发交付」该回来"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="catalog-browser-") as raw:
        data = Path(raw) / "data"

        from app.catalog_api import make_router as make_catalog
        from app.links_api import make_router as make_links

        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=[DEV], allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])
        app.include_router(make_catalog(data / "catalog"))
        app.include_router(make_links(data / "links"))
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
                PUMP.append(page)          # 让 until() 的等待期间事件循环继续转（见 until 注释）

                def redirect(route):
                    url = route.request.url
                    if "/api/catalog" in url or "/api/links" in url:
                        route.fulfill(status=307, headers={
                            "Location": url.replace("127.0.0.1:5174", f"127.0.0.1:{API}")})
                    else:
                        route.abort()

                # 只拦开发服务器那个源。拦宽了会把 307 的目标再拦一遍 → 死循环
                page.route(f"{DEV}/api/**", redirect)
                reopen_home(page)

                seen = {}
                steps = [
                    ("空表首页与分类表", lambda: phase_empty(page)),
                    ("新建分类", lambda: seen.update(new_id=phase_create(page))),
                    ("拖拽：工具换组", lambda: phase_drag_tool(page, seen["new_id"])),
                    ("拖拽：分类排序", lambda: phase_drag_category(page)),
                    ("悬停高亮", lambda: phase_hover(page)),
                    ("存不进去就还原", lambda: phase_rollback(page)),
                    ("外链 chip 换组", lambda: seen.update(link_id=phase_link_chip(page))),
                    ("刷新后还在", lambda: phase_reload(page, seen["new_id"], seen["link_id"])),
                    ("非拖拽路径与改名", lambda: phase_move_and_rename(page, seen["new_id"])),
                    ("删除自建分类", lambda: phase_delete(page, seen["new_id"])),
                ]
                for name, call in steps:
                    print(f"--- {name}", flush=True)
                    try:
                        call()
                    except Exception as exc:
                        raise AssertionError(f"【{name}】{exc}") from exc

                # 窄屏：两栏堆叠，chip 的「移动到…」是主要交互，横向一律不许溢出
                open_cats(page)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(400)
                overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
                assert overflow <= 0, f"390px 打开工具分类时横向溢出 {overflow}px"
                close_settings(page)
                overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
                assert overflow <= 0, f"390px 首页横向溢出 {overflow}px"

                assert (data / "catalog" / "catalog.json").exists(), "改了一整轮却没落盘"
                assert not errors, errors
                print("PASS: 空表 6 组 18 卡 · 分类 TAB 7 行内置 / 新建分类落库且不上首页"
                      " / 拖拽工具换组（稀疏 tools·空组消失·卡片数不变）"
                      " / 拖拽分类排序（中线判定·首页首组跟着变）"
                      " / 悬停高亮（合成事件·不落库）"
                      " / POST 失败还原（chip 回原位·盘上不动）"
                      f" / 外链 chip 走 /api/links 换组（此后首页 {CARDS_AFTER_SEED} 张卡）"
                      " / 刷新后全部还在 / ↑↓ 与「移动到…」两条非拖拽路径"
                      " / 改内置名（首页跟着变·改回默认后覆盖消失）"
                      f" / 删自建分类（二次确认·工具回原组·首页回 6 组 {CARDS_AFTER_SEED} 卡）"
                      " / 390 宽不溢出 / 零 pageerror")
                page.unroute_all(behavior="wait")
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
