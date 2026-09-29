"""Compose 工作台（`#compose-manager`）的浏览器端到端：编辑区弹窗化、YAML 高亮与语法检测、
拖入导入、两栏布局。

前提和别的套件一样：dev server 在 5174。后端不需要 —— 本套件自己起一个私有实例
（127.0.0.1:8005），只把 `/api/compose/**` 307 过去，其余 `/api/**` 一律断掉（App.vue 的
轮询与本套件无关）。用 307 而不是 `route.fetch` 的理由同 `test_catalog_browser.py`。

私有实例直接跑 `backend/app/main.py` 那个 app，但把模块级的 `compose_manager` 换成
「数据根在临时目录」的实例：compose 的路由就写在 main.py 里、没有独立 router 可挂，
这样既跑到真正的处理函数与存储逻辑，又完全不碰用户本机的 `backend/compose_data`。
两个必须的细节：

  * `lifespan="off"`：那个 lifespan 会拉起 DBX 子进程，测试没必要动它。
  * 自己再加一层 CORS：main.py 只允许 5173，本套件从 5174 发请求。

**不点「启动 / 保存并启动」**：那会真的在本机 docker 上建容器。这里验的是文件与服务的
管理流程（新建 → 编辑 → 拖入导入 → 删除）与布局，运行态留给用户自己点。

用法：python test_compose_browser.py
"""
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright

DEV = "http://127.0.0.1:5174"
API = 8005
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

NAME = "联调环境"
EDITED_NAME = "联调环境 v2"
DRAG_FILE = "cache.yml"
# 拖入导入的项目名是**文件名去掉扩展名**（后端 import 接口的既有行为），不是文件内容里的东西
DRAGGED = "cache"
CONTENT = "services:\n  app:\n    image: nginx:alpine\n    ports:\n      - '8080:80'\n"
EDITED_CONTENT = "services:\n  app:\n    image: nginx:1.27-alpine\n"
DROPPED_CONTENT = "services:\n  cache:\n    image: redis:7-alpine\n"
# 语法检测的两档：第 4 行的 `[` 没闭合（错误级），没写 image（警告级，语法是对的）。
BROKEN_CONTENT = "services:\n  app:\n    image: [\n"
WARN_CONTENT = "services:\n  app:\n    command: echo hi\n"


def get_json(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{API}{path}") as response:
        return json.load(response)


def projects() -> list:
    return get_json("/api/compose/projects")


def stored(name: str):
    return next((item for item in projects() if item["name"] == name), None)


# main() 把 page 装进来：轮询空档必须让 Playwright 的事件循环转起来，
# 不能用 time.sleep（理由见 test_catalog_browser.py 的 until 注释）。
PUMP = []


def until(predicate, timeout=10):
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


def open_page(page):
    page.goto(f"{DEV}/#compose-manager", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    page.locator(".compose-layout").wait_for(timeout=30000)


def dialog(page):
    return page.locator(".compose-editor-card")


def yaml_source(page):
    """读回编辑器里的 YAML。

    CodeMirror 把文档渲染成一堆 `<div class="cm-line">`，没有 `textarea`，也就没有
    `input_value()`；`.cm-content` 的 `innerText` 是唯一带换行的读法。**它只渲染视口内的
    行**：本套件的 compose 都只有几行，够用。
    """
    return page.locator(".compose-yaml .cm-content").inner_text()


def wait_yaml(page, want, timeout=8.0):
    """等编辑器里的 YAML 等于 `want`（首尾空白不算），返回读到的那一版。"""
    deadline = time.monotonic() + timeout
    got = yaml_source(page)
    while got.strip() != want.strip() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        got = yaml_source(page)
    return got


def set_yaml(page, source):
    """把 YAML 写进 CodeMirror，并**读回比对**。

    contenteditable 不是 input：写没写进去不会自己暴露，不比对的话会把「没写进去」伪装成
    「保存没生效」。用 `fill`（Playwright 对 contenteditable 走 execCommand insertText，
    CM 当成一次真实输入）；**不用 `keyboard.type`** —— 它逐字符发 keydown，CM 的 Enter 会
    叠上 indentOnInput，每行的缩进多一层。
    """
    page.locator(".compose-yaml .cm-content").fill(source)
    got = wait_yaml(page, source)
    assert got.strip() == source.strip(), \
        f"编辑器里的 YAML 与写入的不一致：\n{got!r}\n期望：\n{source!r}"


def lint_text(page):
    """编辑器下面那份问题清单（没有问题时为空串）。"""
    block = page.locator(".compose-yaml-problems")
    return block.first.inner_text() if block.count() else ""


def drag_with_file(page, name, content, commit):
    """把 dataTransfer 里带 File 的 dragover / drop 事件打到页面根上。

    `commit=False` 只派发 dragover（用来看悬停高亮），`commit=True` 再派发 drop。
    """
    page.evaluate("""([name, content, commit]) => {
        const dt = new DataTransfer();
        dt.items.add(new File([content], name, {type: 'text/yaml'}));
        const target = document.querySelector('.compose-page');
        const fire = type => target.dispatchEvent(
            new DragEvent(type, {dataTransfer: dt, bubbles: true, cancelable: true}));
        fire('dragover');
        if (commit) fire('drop');
    }""", [name, content, commit])


def phase_layout(page):
    """页面主体是「服务收藏 + 运行与日志」，编辑区不再常驻。"""
    assert page.locator(".compose-editor").count() == 0, "编辑区又变成常驻栏了"
    assert dialog(page).count() == 0, "没点新建就弹出了编辑弹窗"
    columns = page.locator(".compose-layout").evaluate(
        "(el)=>getComputedStyle(el).gridTemplateColumns.split(' ').length")
    assert columns == 2, f"主区应该是两栏（收藏 + 运行），实际 {columns} 栏"
    # 运行时那一栏要比收藏栏宽：腾出来的位置是给容器状态与日志的
    library = page.locator(".compose-library").bounding_box()["width"]
    runtime = page.locator(".compose-runtime").bounding_box()["width"]
    assert runtime > library * 2, f"运行区没有拿到腾出来的宽度：{library} vs {runtime}"
    assert page.locator(".compose-import").is_visible(), "导入区不见了"
    assert "拖" in page.locator(".compose-import").inner_text(), "导入区没有提示可拖入"
    # 拖入区与「导入」按钮是两件东西：按钮在收藏列表头，不许套在拖入区里
    assert page.locator(".compose-import button").count() == 0, "导入按钮又和拖入区叠在一起了"
    assert page.get_by_role("button", name="导入 compose 文件").is_visible(), "没有单独的导入按钮"
    # 左侧拖入区要够大（不是一条 40px 的细缝），但也不许把整栏吞掉：列表才是这一栏的内容
    dropzone = page.locator(".compose-import")
    library_height = page.locator(".compose-library").bounding_box()["height"]
    drop_height = dropzone.bounding_box()["height"]
    assert drop_height >= 96, f"左侧拖入区太小了：{drop_height}px"
    assert drop_height < library_height * .45, f"拖入区把整栏都占了：{drop_height} / {library_height}"
    assert page.locator(".compose-empty").first.is_visible(), "空表时没有引导文案"
    assert page.locator(".compose-empty.runtime").first.is_visible(), "运行区空态没有引导文案"
    # 上半部分（标题 + Docker 状态 + sudo）压成一条，工作区拿到视口的大部分高度
    head = page.locator(".compose-head").bounding_box()["height"]
    assert page.locator(".compose-layout").bounding_box()["height"] > head * 3, f"上半部分占得太多：{head}px"
    # 状态与 sudo 并排在同一条状态栏里（两段的纵向范围必须相交，否则就是又叠成两层）
    engine = page.locator(".compose-engine").bounding_box()
    access = page.locator(".compose-access").bounding_box()
    overlap = min(engine["y"] + engine["height"], access["y"] + access["height"]) - max(engine["y"], access["y"])
    assert overlap > 0, "「Docker 状态」与「sudo 权限」又上下叠成两层了"
    # 两栏各自滚动，整页不许有第二条滚动条（宿主 main 底部留白 80px，算高度时要减掉）
    pageScroll = page.evaluate("document.documentElement.scrollHeight-innerHeight")
    assert pageScroll <= 0, f"1440 下整页出现了滚动条：{pageScroll}px"
    # 没选项目时，运行按钮一律不可点
    for label in ["启动", "重启", "停止"]:
        assert page.get_by_role("button", name=label, exact=True).is_disabled(), f"{label} 不该可点"


def phase_create(page):
    """「＋ 新建」→ 弹窗 → 保存：弹窗关闭、项目落库、出现在左栏。"""
    page.get_by_role("button", name="＋ 新建").click()
    card = dialog(page)
    card.wait_for(timeout=5000)
    assert card.get_attribute("aria-modal") == "true", "弹窗缺 aria-modal"
    assert "新建" in card.locator("#compose-editor-title").inner_text(), "弹窗标题不对"
    # 快速模板下拉已删掉：选错模板会把已经写好的 yaml 整段覆盖，那一行留给编辑框
    assert page.locator(".compose-template").count() == 0, "快速模板选择器又回来了"
    # 弹窗的用处就是把空间给 yaml：编辑框的高度必须占卡片的大头
    card_height = card.bounding_box()["height"]
    editor_height = page.locator(".compose-yaml .cm-editor").bounding_box()["height"]
    assert editor_height > card_height * .6, f"yaml 编辑框没拿到弹窗的大部分高度：{editor_height} / {card_height}"
    # 高亮真的上了色：必须有一截文本用的是「键」那个令牌色（只断言「有 span」会被空壳样式骗过）
    keyed = page.evaluate("""() => {
        const hex = getComputedStyle(document.documentElement).getPropertyValue('--compose-yaml-key').trim();
        const value = parseInt(hex.slice(1), 16);
        const want = `rgb(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255})`;
        return [...document.querySelectorAll('.compose-yaml .cm-line span')]
            .some(node => getComputedStyle(node).color === want);
    }""")
    assert keyed, "compose.yaml 没有高亮：没有一个 token 用上 --compose-yaml-key"
    # 预填的那份是干净的：等检查跑完一轮（400ms 防抖 + 一个来回），它必须一声不吭
    page.wait_for_timeout(900)
    assert lint_text(page) == "", f"干净的 compose 被报出了问题：{lint_text(page)!r}"
    assert not page.get_by_role("button", name="保存项目").is_disabled(), "没毛病却不让保存"
    page.locator(".compose-name input").fill(NAME)
    set_yaml(page, CONTENT)
    page.get_by_role("button", name="保存项目").click()
    created = until(lambda: stored(NAME))
    assert created, "保存后私有实例里没有这个项目"
    assert created["content"].strip() == CONTENT.strip(), created["content"]
    page.wait_for_function("!document.querySelector('.compose-editor-card')", timeout=5000)
    until(lambda: page.locator(".compose-project", has_text=NAME).count() == 1)
    assert page.locator(".compose-project", has_text=NAME).count() == 1, "左栏没出现新项目"
    # 保存后自动选中它：右栏标题跟着变
    assert NAME in page.locator(".compose-runtime-head").inner_text(), "保存后没有选中新项目"
    # 「启动」只跟 Docker 可用性走。这个环境没有 sudo 密码，docker.sock 恰好不可读，
    # 所以这里顺手验了另一半：**文件管理与 Docker 状态无关** —— compose 写得进、
    # 改得动、删得掉，正是这个页面「先管文件再管服务」的意思。
    engine = get_json("/api/compose/status")
    assert page.get_by_role("button", name="启动", exact=True).is_disabled() == (not engine["available"]), \
        "工作区「启动」没有跟着 Docker 可用状态走"
    for label in ["编辑 compose", "删除记录"]:
        assert not page.get_by_role("button", name=label).is_disabled(), f"{label} 不该看 Docker 的脸色"


def phase_edit(page):
    """选中项目 → 「编辑 compose」→ 弹窗预填 → 保存修改写回。"""
    page.locator(".compose-project", has_text=NAME).click()
    page.wait_for_timeout(200)
    page.get_by_role("button", name="编辑 compose").click()
    card = dialog(page)
    card.wait_for(timeout=5000)
    assert page.locator(".compose-name input").input_value() == NAME, "编辑弹窗没预填项目名"
    assert yaml_source(page).strip() == CONTENT.strip(), f"编辑弹窗没预填 compose 内容：{yaml_source(page)!r}"
    page.locator(".compose-name input").fill(EDITED_NAME)
    set_yaml(page, EDITED_CONTENT)
    page.get_by_role("button", name="保存修改").click()
    updated = until(lambda: stored(EDITED_NAME))
    assert updated, "改名没写回私有实例"
    assert updated["content"].strip() == EDITED_CONTENT.strip(), updated["content"]
    page.wait_for_function("!document.querySelector('.compose-editor-card')", timeout=5000)
    # 关闭方式：先 Escape，再点遮罩 —— 两条都要能关掉
    page.get_by_role("button", name="编辑 compose").click()
    dialog(page).wait_for(timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('.compose-editor-card')", timeout=5000)
    page.get_by_role("button", name="编辑 compose").click()
    dialog(page).wait_for(timeout=5000)
    page.mouse.click(12, 12)
    page.wait_for_function("!document.querySelector('.compose-editor-card')", timeout=5000)


def phase_lint(page):
    """语法检测：错误级挡「保存」，警告级不挡，改好之后按钮立刻回来。

    检查是后端的 PyYAML 跑的（`POST /api/compose/validate`），所以这里同时验了那条路由真在
    私有实例里活着 —— 检查不到东西时组件是「放行」的，只看按钮点不点得动会漏掉这一点。
    """
    page.locator(".compose-project", has_text=EDITED_NAME).click()
    page.wait_for_timeout(200)
    page.get_by_role("button", name="编辑 compose").click()
    card = dialog(page)
    card.wait_for(timeout=5000)
    save = page.get_by_role("button", name="保存修改")
    assert not save.is_disabled(), "刚打开、一个字没改就拦着保存了"
    # 警告级：服务没写 image（compose 多半跑不起来），但这个文件语法是对的 —— 不该拦人
    set_yaml(page, WARN_CONTENT)
    until(lambda: "image" in lint_text(page))
    assert "image" in lint_text(page), f"服务没写 image 这件事没被提示：{lint_text(page)!r}"
    assert "第 2 行" in lint_text(page), f"警告没划在出问题的那个服务上：{lint_text(page)!r}"
    assert not save.is_disabled(), "警告级的问题不该拦着保存"
    # 错误级：`[` 没闭合（PyYAML 的 mark 落在文末那个空行上，要退回到有内容的第 3 行 ——
    # 空行划不出波浪线，用户也不知道该改哪儿）。清单里有行号，正文下有波浪线，两个保存按钮都不可点
    set_yaml(page, BROKEN_CONTENT)
    until(lambda: "第 3 行" in lint_text(page))
    assert "第 3 行" in lint_text(page), f"语法错误没报到第 3 行：{lint_text(page)!r}"
    assert page.locator(".compose-yaml .cm-lintRange-error").count() >= 1, "错误那一行没有波浪线"
    assert save.is_disabled(), "YAML 有语法错误时「保存修改」还能点"
    assert page.get_by_role("button", name="保存并启动").is_disabled(), "有语法错误时「保存并启动」还能点"
    assert save.get_attribute("title"), "按钮被禁用了却没说明为什么"
    # 改回合法 YAML：坏掉的项目不该一直拦着人
    set_yaml(page, EDITED_CONTENT)
    until(lambda: not save.is_disabled())
    assert not save.is_disabled(), "改回合法 YAML 之后保存仍然被拦着"
    assert lint_text(page) == "", f"改好之后问题清单没清掉：{lint_text(page)!r}"
    # 全程没点过保存：关掉弹窗，存储里还是原文（检测不落盘）
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('.compose-editor-card')", timeout=5000)
    assert stored(EDITED_NAME)["content"].strip() == EDITED_CONTENT.strip(), "语法检测把手改的草稿写进了库"


def phase_drop(page):
    """把 .yml 文件拖进页面即导入：悬停高亮 + 落库 + 出现在左栏。"""
    drag_with_file(page, DRAG_FILE, DROPPED_CONTENT, commit=False)
    page.wait_for_timeout(150)
    assert page.locator(".compose-import.is-over").count() == 1, "拖入时导入区没有高亮"
    drag_with_file(page, DRAG_FILE, DROPPED_CONTENT, commit=True)
    dropped = until(lambda: stored(DRAGGED))
    assert dropped, "拖入的文件没有进私有实例"
    assert dropped["content"].strip() == DROPPED_CONTENT.strip(), dropped["content"]
    assert page.locator(".compose-import.is-over").count() == 0, "落下后高亮没清掉"
    until(lambda: page.locator(".compose-project", has_text=DRAGGED).count() == 1)
    assert page.locator(".compose-project", has_text=DRAGGED).count() == 1, "拖入的项目没出现在左栏"
    # 非 yml 文件要被挡下来（不落库）
    drag_with_file(page, "notes.txt", "hello", commit=True)
    page.wait_for_timeout(700)
    assert not stored("notes"), "txt 文件不该被当成 compose 导入"


def phase_delete(page):
    """「删除记录」走二次确认，删完左栏清空、右栏回到未选中。"""
    page.locator(".compose-project", has_text=DRAGGED).click()
    page.wait_for_timeout(200)
    page.get_by_role("button", name="删除记录").click()
    page.locator(".confirm-card").wait_for(timeout=5000)
    page.locator(".confirm-btn").click()
    assert until(lambda: stored(DRAGGED) is None), "确认后项目还在私有实例里"
    until(lambda: page.locator(".compose-project", has_text=DRAGGED).count() == 0)
    assert page.locator(".compose-project", has_text=DRAGGED).count() == 0, "左栏还留着已删除的项目"
    # 取消确认时不能删
    page.locator(".compose-project", has_text=EDITED_NAME).click()
    page.wait_for_timeout(200)
    page.get_by_role("button", name="删除记录").click()
    page.locator(".confirm-card").wait_for(timeout=5000)
    page.locator(".cancel-btn").click()
    page.wait_for_timeout(700)
    assert stored(EDITED_NAME), "点了取消却把项目删了"


def phase_narrow(page):
    """390 宽：弹窗与两栏都不许横向溢出（窄屏是堆叠的）。"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(400)
    overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
    assert overflow <= 0, f"390px 页面横向溢出 {overflow}px"
    page.get_by_role("button", name="＋ 新建").click()
    dialog(page).wait_for(timeout=5000)
    page.wait_for_timeout(300)
    overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
    assert overflow <= 0, f"390px 弹窗横向溢出 {overflow}px"
    page.screenshot(path="/tmp/compose-narrow-dialog.png")
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('.compose-editor-card')", timeout=5000)
    page.screenshot(path="/tmp/compose-narrow.png", full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})


def phase_wide(page):
    """4K：两栏跟着变宽，整页不滚，弹窗仍在视口内。"""
    page.set_viewport_size({"width": 3840, "height": 2160})
    page.wait_for_timeout(400)
    wide = page.evaluate("""() => ({
        runtime: Math.round(document.querySelector('.compose-runtime').getBoundingClientRect().width),
        layout: Math.round(document.querySelector('.compose-layout').getBoundingClientRect().height),
        overflow: document.documentElement.scrollWidth - innerWidth,
        pageScroll: document.documentElement.scrollHeight - innerHeight,
    })""")
    assert wide["overflow"] <= 0, wide
    assert wide["runtime"] > 2400, f"4K 下运行区没有变宽：{wide}"
    assert wide["pageScroll"] <= 0, f"4K 下整页出现了滚动条：{wide}"
    page.screenshot(path="/tmp/compose-4k.png")
    page.set_viewport_size({"width": 1440, "height": 1000})


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="compose-browser-") as raw:
        from app import main as backend_main

        # 只换存储根：路由函数取的是模块全局，换掉即全部落到临时目录。
        backend_main.compose_manager = backend_main.ComposeManager(Path(raw) / "compose")
        backend_main.app.add_middleware(CORSMiddleware, allow_origins=[DEV], allow_credentials=True,
                                        allow_methods=["*"], allow_headers=["*"])
        server = uvicorn.Server(uvicorn.Config(backend_main.app, host="127.0.0.1", port=API,
                                               log_level="error", lifespan="off"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, f"私有实例没起来（端口 {API} 被占了？）"

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                PUMP.append(page)

                def redirect(route):
                    url = route.request.url
                    if "/api/compose/" in url:
                        route.fulfill(status=307, headers={
                            "Location": url.replace("127.0.0.1:5174", f"127.0.0.1:{API}")})
                    else:
                        route.abort()

                # 只拦开发服务器那个源，拦宽了会把 307 的目标再拦一遍 → 死循环
                page.route(f"{DEV}/api/**", redirect)
                open_page(page)

                steps = [
                    ("两栏布局与空态", lambda: phase_layout(page)),
                    ("新建走弹窗", lambda: phase_create(page)),
                    ("编辑预填与关闭方式", lambda: phase_edit(page)),
                    ("语法检测", lambda: phase_lint(page)),
                    ("拖入导入", lambda: phase_drop(page)),
                    ("删除要二次确认", lambda: phase_delete(page)),
                    ("390 宽不溢出", lambda: phase_narrow(page)),
                    ("4K 两栏", lambda: phase_wide(page)),
                ]
                for name, call in steps:
                    print(f"--- {name}", flush=True)
                    try:
                        call()
                    except Exception as exc:
                        raise AssertionError(f"【{name}】{exc}") from exc

                print("PAGE ERRORS", errors)
                assert not errors
                browser.close()
        finally:
            server.should_exit = True

    print("PASS compose 工作台：编辑区弹窗化 / YAML 高亮与语法检测 / 拖入导入 / 4K 两栏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
