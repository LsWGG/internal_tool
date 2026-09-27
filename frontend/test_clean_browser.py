"""文件清洗工具的浏览器端到端：上传 / 服务器路径 → 映射 → 规则 → 自定义函数 → 试跑 →
提交 → 取消 → 续跑 → 预览 / 日志 / 报告 / 统计。

前提和 `test_ui_catalog.py` 一样：dev server 在 5174。后端**不需要** —— 本套件自己起一个
私有实例（127.0.0.1:8001，数据根与输入都在临时目录里现场生成）。目录套件把全部 `/api/**`
abort 掉，所以上面的每条数据分支在它那里都到不了，只有这里会真的跑数据。

为什么用 307 跳转而不是 `route.fetch` 转发（`test_word_batch_browser.py` 的写法）：
**SSE 会被 `route.fetch` 吃掉**。它的语义是「拿到完整响应体再 fulfill」，而事件流永不结束 ——
实测 30 秒超时，页面一条事件都收不到，任务徽章永远停在「等待执行」。307 让浏览器自己去请求
私有实例，流是真的流。代价有两条，缺一不可：私有实例要带 CORS（跨源了），而且路由只能拦
开发服务器那个源 —— 拦宽了会把转过去的请求再拦一遍，转成死循环。

用法：python test_clean_browser.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEV = "http://127.0.0.1:5174"
API = 8001
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# 逐行睡 0.03 秒的脱敏函数，两个作用：
#   1. 任务跑得够慢，取消才点得中（行进中才有得取消）；
#   2. 清洗后的预览里手机号是 `138****0000`，证明子进程真的执行了用户代码，而不是原样通过。
MASK = "\n".join([
    "import time",
    "def transform(row):",
    "    time.sleep(0.03)",
    "    value = str(row.get('手机', ''))",
    "    return {'手机': value[:3] + '****' + value[-4:]}",
])
# 上传那一轮用同一个变换但不睡：那一轮要的是「跑完」，不是「跑得久」
MASK_FAST = "\n".join([
    "def transform(row):",
    "    value = str(row.get('手机', ''))",
    "    return {'手机': value[:3] + '****' + value[-4:]}",
])
# 编辑器自检用：少一个冒号 —— 静态检查必须指着第 1 行说话（后端给的就是语法错误的行号）
TYPO = "def transform(row)\n    return row['手机']\n"
NOOP = "def transform(row):\n    return None\n"
# 故意乱排（两个空格缩进、等号两边没空格）：`ruff format` 必须把它改成四个空格 + 空格等号
MESSY = "def transform(row):\n  value=row['手机']\n  return {'手机':  value}\n"
# 试跑用：既要能算，又要能 print —— 逐行结果表和 print 输出块是两块独立的显示
TALKS = "\n".join([
    "def transform(row):",
    "    value = str(row.get('手机', ''))",
    "    print('看到', value)",
    "    return {'手机': value[:3] + '****' + value[-4:]}",
])
# `@codemirror/lang-python` 自带的那份**完整**内置表里的名字，真白名单（clean_worker 的
# `_ALLOWED_BUILTINS`）里一个都没有 —— 出现在补全弹窗里就说明 `autocompletion({override})`
# 那根线断了，界面开始教用户写必然被闸门拒掉的函数。挑的都是 `o` 前缀能命中的：
# 补全只渲染前 100 条，得让护栏落在**过滤后条目很少**的弹窗里，否则断言会「因为没渲染到」而假通过。
LEAKED = ("open", "compile", "globals", "locals", "eval", "exec", "breakpoint")
# 与 PyCodeEditor.vue 的 KEYWORDS 同一份。**故意在测试里再写一遍**：共享的话组件少了一个
# 关键字，断言也跟着少一个，等于没测。
PY_KEYWORDS = {"and", "as", "assert", "async", "await", "break", "class", "continue", "def",
               "del", "elif", "else", "except", "finally", "for", "from", "global", "if",
               "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
               "try", "while", "with", "yield", "True", "False", "None"}


def _csv(head, body):
    return "\n".join([",".join(head)] + [",".join(row) for row in body]) + "\n"


def fixtures(root: Path) -> Path:
    """现场造输入。测试不能依赖本机某个目录里恰好存在的文件。"""
    source = root / "in"
    (source / "nested").mkdir(parents=True)
    cities = ["北京", "上海", "广州"]  # 广州 不在枚举里，于是有真违规可统计
    head = ["姓名", "城市", "金额", "备注", "手机"]
    body = []
    for index in range(400):
        # 每 5 行留一个空备注：fill_null 才有东西可填
        note = "" if index % 5 == 0 else f"备注 {index}"
        # 金额刻意跨过 1000：千分位有没有生效，一眼（一个逗号）就能看出来
        body.append([f"张{index % 7}", cities[index % 3], f"{1000 + index * 3}.5", note,
                     f"138{index:08d}"])
    (source / "a.csv").write_text(_csv(head, body), encoding="utf-8")
    # 试跑那一轮用它：比 a.csv 多一列「内部编号」，而配置里刻意**不映射**它 —— 左栏
    # 末尾就多出一段标着「删除」的列。用户报的正是「清洗前的表头是清洗后的表头、
    # 删除字段一个也看不到」。
    #
    # 刻意**不放在 `in/` 里**：那个目录是上传整目录用的（`webkitdirectory` 把目录下
    # 所有文件都算进去），多一个文件就会把「2 个文件 / 读入 550 行」那一整套断言改掉。
    solo = root / "solo"
    solo.mkdir(exist_ok=True)
    (solo / "c.csv").write_text(
        _csv(head + ["内部编号"],
             [row + [f"N{index:04d}"] for index, row in enumerate(body)]),
        encoding="utf-8")
    # 列顺序刻意不同：映射必须按**名字**对，按位置就会串列
    inner = ["备注,手机,姓名,金额,城市"]
    inner += [f"内层 {i},139{i:08d},李{i % 5},{2000 + i * 5}.0,{cities[i % 3]}" for i in range(150)]
    (source / "nested" / "b.csv").write_text("\n".join(inner) + "\n", encoding="utf-8")
    return source


# ------------------------------------------------------------------ 界面动作

def open_step(page, index):
    """展开第 N 步（1..7）。七个步骤是一次只展开一步的手风琴，第 2 步之后的所有控件
    默认都躺在收起的卡片里（`v-show` 的 `display:none`）—— Playwright 点不可见的元素会
    一直等到超时，所以每个要碰第 N 步控件的 helper 都得先调这个。

    读的是模板里真实写着的 `aria-expanded`，不是在猜状态。已经展开就什么都不做：
    手风琴的切换是「翻转」，多点一次反而会把它收起来。
    """
    head = page.locator(".clean-step-head").nth(index - 1)
    if head.get_attribute("aria-expanded") != "true":
        head.click()


def name_input(page):
    open_step(page, 7)  # 任务名称在最后一步「执行」
    return page.locator(".clean-config input[placeholder*='9 月']")


# 「左侧只做配置，右侧做数据展示」的位置契约。用户原话：「左侧配置表单所有数据预览相关放到右侧，
# 左侧仅做配置，右侧做数据展示」—— 下面两组断言就是这句话的可执行版本。
#
# 按**数量**断言，不猜可见性：七步是一次只展开一步的手风琴，没展开的那几步其实都在 DOM 里
# （`v-show`），「藏起来」和「搬走了」不是一回事 —— 用户展开那一步就会撞见。
DATA_BLOCKS = (".clean-inspect", ".clean-map-preview", ".clean-pane", ".clean-compare", ".clean-legend",
               ".clean-col-tag", ".clean-dry", ".clean-estimate", ".clean-fn-test", ".clean-fn-table",
               ".clean-fn-print", ".clean-stats", ".clean-table-wrap", ".clean-logs", ".clean-report")


def assert_no_data_in_config(page):
    counts = page.evaluate(
        "(selectors) => Object.fromEntries(selectors.map(s => "
        "[s, document.querySelectorAll('.clean-config ' + s).length]))", list(DATA_BLOCKS))
    assert not any(counts.values()), f"配置列里还留着数据块：{counts}"


def assert_in_stage(page, selector):
    """反过来也要断言：块确实落在结果区里。只查「不在左边」的话，把块整个删掉也能过。"""
    assert page.locator(selector).first.evaluate("el => !!el.closest('.clean-stage')"), \
        f"{selector} 不在结果区（.clean-stage）里"


def active_source(page):
    """结果区当前在看哪一份数据（五个来源开关里的活动项）。

    开关只在有两份以上数据时才渲染（只有一份时它只是在重复 `.clean-current` 那句话），
    所以还没有第二次跑动的阶段要改用 `current_label`。
    """
    return page.locator(".clean-source button.active").inner_text()


def current_label(page):
    """结果区标题行那句话（`.clean-current`）—— 它在有没有来源开关时都在。"""
    return page.locator(".clean-stage .clean-current").inner_text()


def inspect(page):
    open_step(page, 2)
    page.get_by_role("button", name="探测表头").click()
    try:
        page.wait_for_selector(".clean-inspect", timeout=60000)
    except Exception as exc:
        # 探测失败时现场只剩一句超时，看不出是被拒了还是界面没切过去 —— 把提示和当前来源
        # 一起带进异常（提示气泡几秒就消失，所以必须在这里读）。
        toasts = " | ".join(page.locator(".toast-card").all_inner_texts())
        warns = " | ".join(page.locator(".clean-warn").all_inner_texts())
        meta = page.locator(".clean-stage-meta").inner_text().replace("\n", " ")
        raise AssertionError(
            f"探测结果没出现：toasts={toasts} / warns={warns} / meta={meta}") from exc
    page.get_by_role("button", name="按表头一键映射").click()
    # 探测的结果（文件/编码/分隔符/表头）也在结果区的「源数据」里，点完就切过去。
    # 这里断言 `.clean-current` 而不是来源开关：只有一份数据时开关根本不渲染（见 active_source）。
    assert_in_stage(page, ".clean-inspect")
    assert current_label(page).startswith("源数据"), current_label(page)


def mapping_preview(page):
    """第 2 步的实时数据预览。结束时字段表与原 `inspect(page)` 一致（探测本身就会按表头
    映射，那个 helper 里的「一键映射」其实是第二步保险）。

    「未映射」这个标记只能靠**解绑**一个来源列看到：探测完每一列都已经有主了。
    """
    open_step(page, 2)
    page.get_by_role("button", name="探测表头").click()
    page.wait_for_selector(".clean-inspect", timeout=60000)
    pane = page.locator(".clean-map-preview .clean-pane")
    pane.wait_for(timeout=60000)

    # 表头是文件里的**真实**列名（映射要按名字对，所以名字得先看得见）
    heads = pane.locator("thead th").all_inner_texts()
    assert heads[0] == "源数据", heads
    for column, name in enumerate(["姓名", "城市", "金额", "备注", "手机"]):
        assert name in heads[column + 1], heads
    # 值也是**真实**的：不是表头名、不是占位符。第一行备注是空的（每 5 行一个空）
    cells = pane.locator("tbody tr").first.locator("td").all_inner_texts()
    assert cells == ["1", "张0", "北京", "1000.5", "（空）", "13800000000"], cells
    assert pane.locator("tbody tr").count() == 20, "预览行数默认 20"

    # 探测顺带就按表头映射好了（既有的行为），所以这里每一列都已经有主
    assert pane.locator(".clean-col-tag.mapped").count() == 5, "表头映射没标出目的字段"
    assert "→ 姓名" in pane.locator(".clean-col-tag.mapped").first.inner_text()

    # 映射状态是**实时**的：把一个来源列改回「（新列）」，它那一列立刻改口说「未映射」
    source = page.locator(".clean-field").first.locator("select")
    source.select_option("")
    page.wait_for_timeout(150)
    assert pane.locator(".clean-col-tag.mapped").count() == 4, "解绑了还有 5 列有主"
    assert pane.locator(".clean-col-tag").filter(has_text="未映射").count() == 1
    source.select_option("姓名")
    page.wait_for_timeout(150)
    assert pane.locator(".clean-col-tag.mapped").count() == 5, "绑回去没恢复"

    # 预览行数：改了要真的重新探（服务端按 rows 再读一次）
    page.locator(".clean-map-preview label:has-text('预览行数')") \
        .locator("select").select_option("50")
    page.wait_for_function(
        "() => document.querySelectorAll('.clean-map-preview tbody tr').length === 50",
        timeout=60000)

    # 换一个文件看：表头必须换成**那个文件**的列序（b.csv 的列是乱序的，正好认得出）。
    # 取的是选项的值而不是硬写 key —— 目录上传的 key 带目录前缀（`in/a.csv`），
    # 硬写会把「上传路径长什么样」这个无关细节焊进断言。
    files = page.locator(".clean-map-preview label:has-text('预览文件')").locator("select")
    values = files.locator("option").evaluate_all("els => els.map(el => el.value)")
    other = next(value for value in values if value.endswith("b.csv"))
    home = next(value for value in values if value.endswith("a.csv"))
    files.select_option(other)
    page.wait_for_function("""() => {
      const th = [...document.querySelectorAll('.clean-map-preview thead th')]
        .map(item => item.textContent.trim());
      return th.length === 6 && th[1].startsWith('备注');
    }""", timeout=60000)
    # 切回 a.csv：后面的用例（`.clean-field` 的条数与位置）依赖的还是这一份
    files.select_option(home)
    page.wait_for_function("""() => {
      const th = [...document.querySelectorAll('.clean-map-preview thead th')]
        .map(item => item.textContent.trim());
      return th.length === 6 && th[1].startsWith('姓名');
    }""", timeout=60000)

    # 「实时」的另一半：改**解析选项**也要自动重探，不是只有点按钮才刷新。故意填一个错的
    # 编码 —— 值必须自己变成乱码，清掉又自己变回来。这是唯一能证明那条 watch 接上了的断言。
    encoding = page.locator(".clean-step").nth(1) \
        .locator(".clean-two label:has-text('编码')").locator("input")
    encoding.fill("gbk")
    page.wait_for_function("""() => {
      const cell = document.querySelector('.clean-map-preview tbody tr td:nth-child(2)');
      return cell && !cell.textContent.includes('张0');
    }""", timeout=60000)
    encoding.fill("")
    page.wait_for_function("""() => {
      const cell = document.querySelector('.clean-map-preview tbody tr td:nth-child(2)');
      return cell && cell.textContent.includes('张0');
    }""", timeout=60000)
    assert encoding.input_value() == "utf-8", "清空编码后没有回填探测到的编码"
    # 这块预览连同它的三个控件都搬到了结果区：左侧只剩分隔符/编码这两个输入框。
    assert_in_stage(page, ".clean-map-preview .clean-pane")
    assert_no_data_in_config(page)


def add_new_column(page, dest):
    """只填目标字段、来源留空 = 新建一列（第 2 步的约定）。"""
    open_step(page, 2)
    page.get_by_role("button", name="添加字段").click()
    page.locator(".clean-field").last.locator("input").fill(dest)


def field_panel(page, dest):
    """第 4 步的字段面板。`.clean-fn` 与 `.clean-limits` 也带 `clean-gen`，必须排掉。"""
    open_step(page, 4)
    return page.locator("details.clean-gen:not(.clean-fn):not(.clean-limits)") \
        .filter(has_text=dest).first


def add_op(page, op, field, value_kind=None, **values):
    """加一条声明式规则。`value_kind` 单独给：它是下拉框，其余参数是输入框。

    `number_format` 之类的算子**必须**有目标类型，漏了它服务端会拒绝整份配置
    （「ops.0：Value error, number_format 必须提供 value_kind」）—— 试跑就不会出结果。
    """
    open_step(page, 3)
    page.get_by_role("button", name="添加规则").click()
    card = page.locator(".clean-op").last
    card.locator("select").first.select_option(op)
    card.locator("select").nth(1).select_option(field)
    if value_kind:
        card.locator("label:has-text('目标类型')").locator("select").first.select_option(value_kind)
    for label, value in values.items():
        target = card.locator(f"label:has-text('{label}')").locator("input").first
        if value is True:
            target.check()
        else:
            target.fill(str(value))


def fn_source(card):
    """读回编辑器里的源码。

    CodeMirror 把文档渲染成一堆 `<div class="cm-line">`，没有 `textarea`，也就没有
    `input_value()`；`.cm-content` 的 `innerText` 是唯一带换行的读法（`textContent` 会把
    所有行连成一串）。**它只渲染视口内的行**：源码长到要滚动时读不全 —— 本套件里的函数
    都 ≤10 行，够用。
    """
    return card.locator(".cm-content").inner_text()


def _read_source_until(page, card, want, timeout):
    """等编辑器里的源码等于 `want`（首尾空白不算），等到就返回，超时就返回读到的最后一版。

    Vue 的 watch 到 CM 的 dispatch 之间隔着微任务，程序化写入（「用这段」/格式化）之后
    立刻读会读到上一版。
    """
    deadline = time.monotonic() + timeout
    got = fn_source(card)
    while got.strip() != want.strip() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        got = fn_source(card)
    return got


def wait_source(page, card, want, timeout=20.0):
    """等源码里出现 `want` 再读回来 —— 断言源码内容时用它，别直接 `fn_source`。"""
    deadline = time.monotonic() + timeout
    got = fn_source(card)
    while want not in got and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        got = fn_source(card)
    assert want in got, f"等了 {timeout} 秒，编辑器里还是没有 {want!r}：{got!r}"
    return got


def set_fn_source(page, card, source):
    """把源码写进 CodeMirror，并**读回比对**。

    contenteditable 不是 input：写没写进去不会自己暴露。不比对的话，写失败会伪装成
    「函数没生效」，一路查到子进程才发现源码框是空的。

    先试 `fill`（Playwright 对 contenteditable 走 `execCommand('insertText')`，CM 把它当
    一次真实输入），没落上再退回「全选 + insert_text」。**两条路都不许用 `keyboard.type`**：
    它逐字符发 keydown，CM 的 `Enter` → `insertNewlineAndIndent` 叠上 `indentOnInput`，
    每行的缩进会多一层 —— 症状要到几条断言之后才炸出来。

    全程**不 click**：点击落在内容中央，光标会跑到某一行中间，后面的补全上下文就全错了。
    聚焦用 `focus()`，选区归 CM 的 `Mod-a`（selectAll）。
    """
    box = card.locator(".cm-content")
    box.focus()
    box.fill(source)
    if _read_source_until(page, card, source, 3.0).strip() != source.strip():
        box.focus()
        page.keyboard.press("Control+a")
        page.keyboard.insert_text(source)
    got = _read_source_until(page, card, source, 10.0)
    assert got.strip() == source.strip(), \
        f"编辑器里的源码与写入的不一致（fill 与 insert_text 都没落上）：\n{got!r}\n期望：\n{source!r}"
    return got


def completion_labels(page):
    """当前补全弹窗里的候选文本。

    CM 把候选渲染在 `.cm-editor` 里一个 `position:fixed` 的容器里（`@codemirror/view` 的
    tooltip 默认 `parent` 就是编辑器自身），固定定位不受 `.clean-step{overflow:hidden}` 裁切。
    只渲染**前 100 条**，所以断言要挑过滤后条目很少的前缀来打。
    """
    return page.locator(".cm-tooltip-autocomplete .cm-completionLabel").all_inner_texts()


def popup_hit_test(page):
    """补全弹窗有没有真的显示出来：中心点必须落回弹窗自己身上。

    Playwright 的 `is_visible()` 不看祖先裁切，元素被 `overflow:hidden` 剪掉一半时它照样说
    「可见」；`elementFromPoint` 走的是命中测试，被剪掉的地方点不到 —— 这是唯一能在套件里
    验「弹窗没被裁」的写法（`@codemirror/view` 在祖先有 transform 时会把 fixed 降级成
    absolute 并改用编辑器做容器，那时就真会被裁）。
    """
    return page.evaluate("""() => {
      const tip = document.querySelector('.cm-tooltip-autocomplete');
      if (!tip) return {found: false};
      const box = tip.getBoundingClientRect();
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      return {found: true, width: Math.round(box.width), height: Math.round(box.height),
              inside: !!hit && tip.contains(hit),
              inViewport: box.top >= 0 && box.left >= 0 &&
                          box.bottom <= window.innerHeight && box.right <= window.innerWidth,
              hit: hit ? String(hit.className || hit.tagName) : '(null)'};
    }""")


def add_function(page, source, timeout_s=60, field="手机"):
    open_step(page, 3)
    page.get_by_role("button", name="添加函数").click()
    card = page.locator(".clean-fn").last
    card.locator("summary").click()
    card.locator("input").first.fill("手机脱敏")
    set_fn_source(page, card, source)
    # 输入字段 / 写回字段是勾选块（原来是 `<select multiple>`，原生列表框没法断言也点不准）。
    # 两个块各自的提示文字里有「字段」二字，所以用 `.clean-in-fields` / `.clean-out-fields`
    # 这两个专用包裹类限定范围，别用 `card.get_by_role(...)` 一把抓。
    card.locator(".clean-in-fields").get_by_role("checkbox", name=field, exact=True).check()
    card.locator(".clean-out-fields").get_by_role("checkbox", name=field, exact=True).check()
    # 超时是**每批**的：默认 5 秒会把睡满整批的子进程杀掉，值原样通过，看起来像函数没生效
    card.locator("input[type=number]").first.fill(str(timeout_s))
    return card


def constrain_city(page):
    """枚举 + 回退（保留原值）：违规要被计数，回退要打一条告警日志。"""
    open_step(page, 5)
    panel = page.locator(".clean-limits").filter(has_text="城市").first
    panel.locator("summary").click()
    panel.get_by_role("button", name="添加约束").click()
    panel.locator(".clean-rule select").first.select_option("enum")
    panel.locator(".clean-rule textarea").fill("北京\n上海")
    panel.locator(".clean-check input[type=checkbox]").check()
    panel.locator(".clean-two select").first.select_option("keep")
    return panel


def wait_dry(page):
    """等试跑出结果。失败时把提示带进异常 —— 否则现场只剩一句超时，看不出是配置被拒了。"""
    try:
        page.wait_for_selector(".clean-dry", timeout=180000)
    except Exception as exc:
        hints = " | ".join(page.locator(".clean-warn").all_inner_texts())
        toasts = " | ".join(page.locator(".toast-card").all_inner_texts())
        raise AssertionError(f"试跑没有结果：{hints} / {toasts}") from exc
    # 试跑的数字（读入/违规/回退/大模型调用）与前后对比都在结果区的「试跑预览」里，
    # 跑完自动切过去 —— 数字和它描述的那份数据得在同一屏。这两句放在 try 外面：
    # 位置不对是位置不对，不该被上面的 except 说成「试跑没有结果」。
    assert_in_stage(page, ".clean-dry")
    assert active_source(page) == "试跑预览", active_source(page)


def dry_run(page):
    """默认行数的试跑（按钮文案跟着行数走，所以这里点的是「试跑前 200 行」）。"""
    open_step(page, 6)
    page.get_by_role("button", name="试跑前 200 行").click()
    wait_dry(page)


def submit(page, name, dry=True):
    if dry:
        dry_run(page)
    open_step(page, 7)
    name_input(page).fill(name)
    page.get_by_role("button", name="获取处理预估").click()
    page.wait_for_selector(".clean-estimate", timeout=120000)
    # 预估的数字也在结果区（点完自动切过去）。这句必须在点「创建清洗任务」**之前**：
    # 提交会把结果区切到「任务结果」，之后再断言就是另一码事了。
    assert_in_stage(page, ".clean-estimate")
    assert active_source(page) == "处理预估", active_source(page)
    page.get_by_role("button", name="创建清洗任务").click()
    row = page.locator(".clean-task").filter(has_text=name).first
    row.wait_for(timeout=60000)
    return row


def wait_badge(page, name, badge, timeout=300):
    page.wait_for_function("""([name, badge]) => {
      const el = [...document.querySelectorAll('.clean-task')]
        .find(node => node.textContent.includes(name));
      return !!el && el.querySelector('.badge').textContent.trim() === badge;
    }""", arg=[name, badge], timeout=timeout * 1000)


def open_task(page, name):
    page.locator(".clean-task").filter(has_text=name).first \
        .get_by_role("button", name="查看").click()
    # 限定在结果区（`.clean-stage`）：`.clean-pane` 不止一处 —— 源数据那块预览也用它，
    # 而结果区同一时刻只挂一个来源的面板（改之前它躺在收起的手风琴里、`display:none`，
    # 不限定就会等到那块**看不见**的然后超时）。选定任务后 `selectTask` 先清空 `preview`
    # 再去取，所以这里等的确实是**这个任务**的前后对比。
    page.wait_for_selector(".clean-stage .clean-pane", timeout=60000)


def tab(page, label):
    page.get_by_role("button", name=label).click()


# ------------------------------------------------------------------ 三个阶段

def phase_upload(page, source):
    """上传目录 → 声明式规则 + 随机生成的新列 + 自定义函数 → 跑完 → 四个 Tab 都看一眼。"""
    name = f"浏览器上传 {int(time.time())}"
    # 先单独验一遍「选择单个文件」：这正是用户报的那个缺陷 —— 以前只有一个
    # `webkitdirectory` 输入框，就算只想传一个文件，弹出的也是**目录**选择器，传上来的是
    # 整个所在目录。现在两个输入框并存，这里真挑一个文件，摘要必须只说 1 个文件。
    page.locator('.clean-file input[data-source="files"]').set_input_files(str(source / "a.csv"))
    page.wait_for_selector(".clean-summary", timeout=60000)
    single = page.locator(".clean-summary").inner_text()
    assert "1 个文件" in single, single
    # 验完立刻移除：下面的目录上传要的是干净状态（否则摘要是 3 个文件）
    page.get_by_role("button", name="移除").click()
    page.wait_for_selector(".clean-summary", state="detached", timeout=30000)

    # `webkitdirectory` 的输入框只吃**目录**，所以传目录、由浏览器展开出相对路径 ——
    # 这正是用户点「选择目录」时发生的事，`relative_paths` 那条分支也就一起进了测试
    page.locator('.clean-file input[data-source="directory"]').set_input_files(str(source))
    page.wait_for_selector(".clean-summary", timeout=60000)
    summary = page.locator(".clean-summary").inner_text()
    assert "2 个文件" in summary, summary

    # 上传不再有大小上限（用户可能传超大目录），界面上就是一句实话：还剩多少盘
    hint = page.locator(".clean-file-hint").inner_text()
    assert "4.0 GB" not in hint and "8.0 GB" not in hint, hint
    assert "大小不限" in hint, hint

    mapping_preview(page)
    assert page.locator(".clean-field").count() == 5, "一键映射没有把 5 个表头都映射出来"
    add_new_column(page, "邮箱")
    assert page.locator(".clean-field").count() == 6
    # 只填目标字段 = 新建一列（源文件里没有这一列）。右侧预览必须照样把它列出来：表头映射时
    # 「我加了哪几列」只能在数据侧看得见，漏掉它就等于这一列凭空消失。
    added = page.locator('.clean-pane[data-side="source"] thead th.added')
    assert added.count() == 1, added.all_inner_texts()
    assert added.inner_text().replace("新增", "").strip() == "邮箱", added.inner_text()
    source_pane = page.locator('.clean-pane[data-side="source"]')
    assert source_pane.locator("thead th").count() == \
        source_pane.locator("tbody tr").first.locator("td").count(), "新增列没有逐行占位"

    # 生成方式要在试跑**之前**配好：只有目标字段、又没有生成规则的新列是配置错误，
    # 试跑会被服务端拒掉，界面只会弹一条提示，等 `.clean-dry` 就是白等。
    panel = field_panel(page, "邮箱")
    panel.locator("summary").click()
    panel.locator("select").first.select_option("random")
    panel.locator("select").nth(1).select_option("email")

    add_op(page, "number_format", "金额", value_kind="float", 小数位=1, 千分位=True)
    add_op(page, "fill_null", "备注", 填充值="（空）")
    add_function(page, MASK_FAST, timeout_s=30)
    constrain_city(page)

    dry_run(page)
    dry = page.locator(".clean-dry").inner_text()
    assert "违规" in dry, dry

    row = submit(page, name, dry=False)
    print("已提交 =", row.inner_text().replace("\n", " ")[:120])
    wait_badge(page, name, "已完成")

    open_task(page, name)
    before = page.locator('.clean-pane[data-side="before"]').inner_text()
    after = page.locator('.clean-pane[data-side="after"]').inner_text()
    # 生成列在清洗前是空的，清洗后有值 —— 这正是「只填目标字段 = 新建一列」
    assert "@" in after, "随机生成的邮箱没写进产物"
    assert "@" not in before, "清洗前不该有生成列的值"
    # 声明式规则确实改了值：千分位格式化了金额；留空的备注被填上；手机被函数脱敏。
    # 预览默认只有 50 行，所以只能断言「有没有」，不能断言条数。
    assert "," in after and "," not in before, "number_format 的千分位没生效"
    assert "（空）" in after, "fill_null 没把空备注填上"
    assert "****" in after, "自定义 Python 函数没生效"
    print("预览：清洗前 %d 字 / 清洗后 %d 字" % (len(before), len(after)))

    # 结果区有五个来源（源数据 / 函数试跑 / 试跑预览 / 处理预估 / 任务结果），所以「看一眼数据」
    # 和「配置里改一下」是两件事。停在任务结果页顺手改解析选项时，自动重探照跑（数据是新的），
    # 但面板**不能自己跳走** —— 用户以为自己只是在打字，不是说要换一份数据看。
    # 换编码是这条规则最容易被误触发的入口（600ms 防抖的那条 watch）。
    open_step(page, 2)   # 手风琴一次只开一步：编码输入框在第 2 步里，收起时点不到
    encoding = page.locator(".clean-step").nth(1) \
        .locator(".clean-two label:has-text('编码')").locator("input")
    encoding.fill("gbk")
    page.wait_for_timeout(1500)          # 600ms 防抖 + 一次真实探测
    assert active_source(page) == "任务结果", f"改编码把面板拽走了：{active_source(page)}"
    encoding.fill("utf-8")               # 还原：后面的步骤还指着这份样本
    page.wait_for_timeout(1500)
    assert active_source(page) == "任务结果", f"还原编码又把面板拽走了：{active_source(page)}"

    # 用户原话：「数据预览的刷新按钮与前边的下拉框没有持平」。这一行原来是居中：
    # 下拉框包在带标题的 label 块里（约 75px 高），按钮按整块居中就比下拉框低几像素。
    # 现在整行按底对齐 —— 底边必须落在同一条线上，靠的是 `align-items:flex-end`。
    gap = page.evaluate("""() => {
      const bar = document.querySelector('.clean-stage .clean-panel .clean-toolbar');
      const select = bar.querySelector('select').getBoundingClientRect();
      const button = bar.querySelector('button').getBoundingClientRect();
      return Math.round(Math.abs(select.bottom - button.bottom));
    }""")
    assert gap <= 1, f"数据预览的刷新按钮与下拉框底边差 {gap}px"

    # 另外三个工具栏住的是同一个类（报告页还多一个「下载 ZIP」的 `<a>`）：只量数据
    # 预览一处，别处不齐就漏了。四个都量：控件上下边缘的极差都要 ≤1px。
    preview_sources = []
    for label_text in ("数据预览", "运行日志", "回归报告", "字段统计"):
        tab(page, label_text)
        page.wait_for_timeout(250)
        # 来源开关（源数据/函数试跑/试跑预览/处理预估/任务结果）只在「数据预览」下露面：
        # 看日志、看报告、看统计时那五份数据都不在屏幕上，按钮留着只会让人以为页签是它们的下一层。
        source_switch = page.locator(".clean-source")
        if label_text == "数据预览":
            assert source_switch.count() == 1, "数据预览下没有来源开关"
            preview_sources = source_switch.locator("button").all_inner_texts()
            assert "源数据" in preview_sources and "任务结果" in preview_sources, preview_sources
        else:
            assert source_switch.count() == 0, f"{label_text} 下还留着来源开关"
        spread = page.evaluate("""() => {
          const bar = document.querySelector('.clean-stage .clean-panel .clean-toolbar');
          if (!bar) return null;
          // 复选框本身只有十几像素高（外面那圈 36px 是它爹 `.clean-check`），量它
          // 只会得到假报警：把它排除，改量 `.clean-check` 这一层。
          const boxes = [...bar.querySelectorAll('select,button,a,input,.clean-check')]
            .filter(el => !el.matches('input[type=checkbox],input[type=radio]'))
            .map(el => el.getBoundingClientRect()).filter(b => b.height > 0);
          if (boxes.length < 2) return {count: boxes.length};
          const span = list => Math.round(Math.max(...list) - Math.min(...list));
          return {count: boxes.length,
                  bottom: span(boxes.map(b => b.bottom)),
                  top: span(boxes.map(b => b.top))};
        }""")
        assert spread, f"{label_text} 没有工具栏"
        if spread["count"] < 2:
            print(f"{label_text} 工具栏只有 {spread['count']} 个控件，无可比项")
            continue
        assert spread["bottom"] <= 1, f"{label_text} 工具栏控件底边不齐：{spread}"
        assert spread["top"] <= 1, f"{label_text} 工具栏控件顶边不齐：{spread}"
        print(f"{label_text} 工具栏 {spread['count']} 个控件，顶/底边极差 "
              f"{spread['top']}/{spread['bottom']}px")

    # 收起来的只是按钮，不是出口：回到数据预览，来源按钮要原样回来（内容也得一样，
    # 别是「回来了但少了几颗」）。
    tab(page, "数据预览")
    back = page.locator(".clean-source button").all_inner_texts()
    assert back == preview_sources, f"回到数据预览，来源开关对不上：{back} vs {preview_sources}"

    tab(page, "运行日志")
    page.wait_for_selector(".clean-logs p", timeout=60000)
    logs = page.locator(".clean-logs").inner_text()
    # 回退**必须**留下告警：计数精确、日志有界（同组只报首次与数量级）
    assert "回退" in logs, logs[:400]
    print("日志行 =", page.locator(".clean-logs p").count())

    tab(page, "回归报告")
    page.wait_for_selector(".clean-report h1", timeout=60000)
    report = page.locator(".clean-report").inner_text()
    # 报告是 markdown-it 渲染的：核心内容就在表格里，表格渲染不出来这份报告就没用了
    assert page.locator(".clean-report table").count() >= 5, "报告的 Markdown 表格没渲染出来"
    assert "读入 550 行，输出 550 行" in report.replace(",", ""), report[:300]
    assert "回退" in report, "报告里没有回退统计"
    print("报告表格 =", page.locator(".clean-report table").count())

    tab(page, "字段统计")
    page.wait_for_selector(".clean-stats tbody tr", timeout=60000)
    rows = page.locator(".clean-stats tbody tr")
    assert rows.count() == 6, "统计表的字段数与配置不符"
    print("统计行 =", rows.count())

    # 用户原话：「字段统计的表太拥挤了，支持横向拖动」。六列原来被压着换行（表格
    # `width:100%`、单元格不换行限制都没有），永远出不来滚动条。现在表格按内容宽度
    # 排、外面套一层滚动容器：窄屏下必须真拖得动，宽屏放得下就不强求有滚动条。
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(300)
    scroll = page.evaluate("""() => {
      const wrap = document.querySelector('.clean-table-wrap');
      return {overflow: getComputedStyle(wrap).overflowX,
              over: wrap.scrollWidth - wrap.clientWidth};
    }""")
    assert scroll["overflow"] == "auto", scroll
    assert scroll["over"] > 0, f"390px 下统计表没有可横向拖的余量：{scroll}"
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.wait_for_timeout(200)

    # 用户原话：「获取处理预估和创建清洗任务 持平」—— 两个按钮都整行等宽。
    # 量的是第 7 步 body 的内容宽度：`width:100%` 的块级按钮就该等于它。
    open_step(page, 7)
    blocks = page.evaluate("""() => {
      const body = [...document.querySelectorAll('.clean-step-body')].at(-1);
      const style = getComputedStyle(body);
      const inner = body.getBoundingClientRect().width
        - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
        - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
      return {inner: Math.round(inner),
              buttons: [...body.querySelectorAll('button.clean-block')]
                .map(b => [b.innerText.trim().slice(0, 6), Math.round(b.getBoundingClientRect().width)])};
    }""")
    assert len(blocks["buttons"]) == 2, blocks
    for label, width in blocks["buttons"]:
        assert abs(width - blocks["inner"]) <= 2, f"{label} 没有整行等宽：{blocks}"
    print("预估 / 创建 按钮宽度 =", [w for _, w in blocks["buttons"]], "配置列内容宽 =", blocks["inner"])
    # 这一轮走完了全部七步（含生成列、函数、试跑、任务结果），是最全的一次「左侧只剩配置」抽查。
    assert_no_data_in_config(page)
    return name


def phase_resume(page, source):
    """服务器路径 → 慢函数 → 取消 → 续跑。这条路径上「取消」与「真的续跑」都要验到。"""
    name = f"浏览器续跑 {int(time.time())}"
    open_step(page, 1)
    page.get_by_role("button", name="服务器路径").click()
    page.locator("textarea[placeholder]").first.fill(str(source))
    inspect(page)
    add_function(page, MASK, timeout_s=60)
    row = submit(page, name)

    # 队列里有历次任务，所以只认这一行；等的是「处理中 + 有取消按钮」而不是中间百分比 ——
    # 550 行是一个块，进度只在块边界报，界面上就是 0% → 100%。
    page.wait_for_function("""(name) => {
      const el = [...document.querySelectorAll('.clean-task')]
        .find(node => node.textContent.includes(name));
      return !!el && el.textContent.includes('处理中')
        && !!el.querySelector('.task-actions button');
    }""", arg=name, timeout=120000)
    row.get_by_role("button", name="取消").click()
    page.get_by_role("button", name="确认取消").click()
    wait_badge(page, name, "已取消", timeout=180)

    # 取消之后要等一次列表刷新落地：`resume` 决策是服务端按输入指纹与提交点算的，
    # 刷新回来才有「续跑」按钮（刷新前那里还写着取消前那句「不能续跑：任务正在运行」）
    page.wait_for_function("""(name) => {
      const el = [...document.querySelectorAll('.clean-task')]
        .find(node => node.textContent.includes(name));
      return !!el && [...el.querySelectorAll('button')]
        .some(b => b.textContent.trim() === '续跑');
    }""", arg=name, timeout=120000)
    print("取消后 =", row.inner_text().replace("\n", " ")[:150])

    row.get_by_role("button", name="续跑").click()
    wait_badge(page, name, "已完成")

    open_task(page, name)
    tab(page, "回归报告")
    page.wait_for_selector(".clean-report h1", timeout=60000)
    report = page.locator(".clean-report").inner_text()
    # 行数完整不足以说明「接着跑」：重跑一遍也一样是 550 行。这一句来自后端的 resume_count。
    assert "本次是续跑" in report, "报告没有说明这是一次断点续跑（可能只是重跑了一遍）"
    assert "读入 550 行，输出 550 行" in report.replace(",", ""), report[:300]
    print("续跑报告 =", [line for line in report.split("\n") if "续跑" in line][:2])
    assert_no_data_in_config(page)
    return name


def phase_whitelist(page, temp_root):
    """服务器路径的白名单是**纵深防御**：拒绝时要说清被拒的是哪条路径。"""
    open_step(page, 1)
    page.get_by_role("button", name="服务器路径").click()
    page.locator("textarea[placeholder]").first.fill(str(temp_root / "data"))
    # 这里刻意不走 `inspect()`：那一轮等的是「探测出结果」，这一轮等的是被拒的提示。
    # 但「探测表头」在第 2 步里，手风琴的收起态下点不到，得先把那一步展开。
    open_step(page, 2)
    page.get_by_role("button", name="探测表头").click()
    page.wait_for_selector(".toast-card", timeout=60000)
    toast = page.locator(".toast-card").last.inner_text()
    assert "不允许读取这个路径" in toast, toast
    assert str(temp_root / "data") in toast, f"错误消息里没有被拒的路径：{toast}"
    print("白名单拒绝 =", toast.replace("\n", " ")[:120])
    # 被拒的那次探测没有数据可显示：结果区还停在上一份数据上，左侧也只多了一条 toast。
    assert_no_data_in_config(page)


def phase_dry_preview(page, source):
    """试跑本身：行数可设、右侧直接看真数据、报告明细条数能关、规则卡片能区分。

    这一轮**不提交任务**。试跑的用处就是「提交前的最后一眼」，它得能自己成立 ——
    没有任务时右侧四个 Tab 里有两个是灰的，正好一起验了。
    """
    # 从干净状态开始：上一轮的文件、映射、规则还挂在同一个组件实例上（页面里没有
    # localStorage，刷新就是干净的）。不清掉的话，「试跑 5 行」量到的还是上一轮的配置。
    page.reload(wait_until="domcontentloaded")
    page.locator(".clean-workspace").wait_for(timeout=30000)
    page.locator('.clean-file input[data-source="files"]') \
        .set_input_files(str(source.parent / "solo" / "c.csv"))
    page.wait_for_selector(".clean-summary", timeout=60000)
    inspect(page)
    # 用户报的就是这两个动作（左栏表头 + 删除字段）：
    # ①「备注」重命名成「备注说明」—— 给已有目的字段配了来源就是重命名，左栏必须仍旧
    #   显示**文件里**的列名；
    # ② 把「内部编号」这一行的映射删掉 —— 源列没配来源就是会被丢弃，左栏末尾必须带着
    #   原值把它列出来。
    open_step(page, 2)
    page.locator(".clean-field").nth(3).locator("input").fill("备注说明")
    page.locator(".clean-field").nth(5).locator(".clean-danger").click()
    assert page.locator(".clean-field").count() == 5, "内部编号那一行没删掉"
    # 两条声明式规则 + 一个自定义函数 + 一条约束：右侧两栏才有肉眼可见的差异可断言。
    # 单文件只有一个，5000 行的上限也够不着 —— 这里要的是「5 行就是 5 行」。
    add_op(page, "number_format", "金额", value_kind="float", 小数位=1, 千分位=True)
    add_op(page, "fill_null", "备注说明", 填充值="（空）")
    add_function(page, MASK_FAST, timeout_s=30)
    constrain_city(page)

    # 用户原话：「清洗规则 多个规则时 区分不明显」。序号徽章 + 一句跟着取值走的摘要，
    # 三者（序号 / 规则下拉 / 删除）必须排在同一行 —— 换行的话卡片又变成一坨。
    # 手风琴一次只开一步：量几何之前得先把第 3 步展开，收起时（display:none）
    # 每个 `getBoundingClientRect()` 都是 0，比出来的「同一行」是假的。
    open_step(page, 3)
    assert page.locator(".clean-op").count() == 2
    assert page.locator(".clean-op-no").all_inner_texts() == ["1", "2"]
    summaries = page.locator(".clean-op-sum").all_inner_texts()
    assert "金额" in summaries[0] and "备注" in summaries[1], summaries
    heads = page.evaluate("""() => [...document.querySelectorAll('.clean-op')].map(card => {
      const mid = el => {const box = el.getBoundingClientRect(); return Math.round(box.top + box.height / 2);};
      return [mid(card.querySelector('.clean-op-no')), mid(card.querySelector('.clean-op-head select')),
              mid(card.querySelector('.clean-op-head button'))];
    })""")
    for row in heads:
        assert max(row) - min(row) <= 2, f"规则卡片头部没排在同一行：{heads}"

    # —— 试跑行数：默认 200；改小了按钮文案要跟着改，发出去的也得是这个数
    open_step(page, 6)
    # 限定在第 6 步里找：函数卡片（第 3 步）里也有一颗「试跑行数」，文档级的
    # `.clean-step-body .clean-toolbar` 会同时命中两颗 —— 严格模式会直接报「2 elements」。
    rows_input = page.locator(".clean-step").nth(5) \
        .locator(".clean-toolbar").filter(has_text="试跑行数").locator("input[type=number]")
    assert rows_input.input_value() == "200", rows_input.input_value()
    rows_input.fill("5")
    page.get_by_role("button", name="试跑前 5 行").click()
    wait_dry(page)

    # 试跑完右侧自动切到「数据预览」，并且标明这一份是试跑（还没提交）
    page.wait_for_selector(".clean-stage .clean-compare", timeout=60000)
    meta = page.locator(".clean-stage-meta").inner_text()
    assert "试跑预览（未提交）" in meta, meta
    for label in ("运行日志", "字段统计"):
        tab_button = page.locator(".clean-tabs button").filter(has_text=label).first
        assert tab_button.is_disabled(), f"试跑没有{label}，这个 Tab 该是灰的"

    before = page.locator('.clean-compare .clean-pane[data-side="before"] tbody tr')
    after = page.locator('.clean-compare .clean-pane[data-side="after"] tbody tr')
    assert (before.count(), after.count()) == (5, 5), (before.count(), after.count())
    # 序号列 + 5 个字段逐格对上：金额被千分位、备注被填上、手机被自定义函数脱敏。
    # 这三样分别在声明式规则、空值填充、隔离子进程里发生 —— 预览拿到的必须是流水线的真产物。
    first_before = before.first.locator("td").all_inner_texts()
    first_after = after.first.locator("td").all_inner_texts()
    assert first_before == ["1", "张0", "北京", "1000.5", "", "13800000000", "N0000"], \
        first_before
    assert first_after == ["1", "张0", "北京", "1,000.5", "（空）", "138****0000"], first_after

    # 用户原话：「数据预览 清洗前的表头 显示的是清洗后的表头，而且没有展示删除字段」。
    # 表头只取第一个子节点（`<th>` 里还挂着一个「删除」/「新增」小标记）。
    names = lambda side: page.evaluate("""(side) => [...document.querySelectorAll(
      `.clean-pane[data-side="${side}"] thead th`)]
      .map(th => (th.firstChild?.textContent || '').trim())""", side)
    assert names("before") == ["清洗前", "姓名", "城市", "金额", "备注", "手机", "内部编号"], \
        names("before")
    # 重命名：右栏是目的字段名，左栏仍旧是文件里的列名
    assert names("after") == ["清洗后", "姓名", "城市", "金额", "备注说明", "手机"], names("after")
    # 被丢弃的源列：逐行带着原值出现在左栏（灰掉 + 划掉），右栏（产物）里没有它的位置
    dropped = page.locator('.clean-pane[data-side="before"] tbody td.dropped')
    assert dropped.count() == 5, "被丢弃的那一列应该在左栏逐行显示"
    assert dropped.first.inner_text() == "N0000", dropped.first.inner_text()
    assert page.locator('.clean-pane[data-side="before"] thead th.dropped').count() == 1
    assert page.locator('.clean-pane[data-side="after"] thead th.dropped').count() == 0
    assert page.locator('.clean-pane[data-side="after"] tbody td.dropped').count() == 0
    legend = page.locator(".clean-legend").inner_text()
    assert "不会进产物" in legend and "内部编号" in legend, legend
    changed = page.locator('.clean-compare .clean-pane[data-side="after"] td.changed')
    assert changed.first.inner_text() == "1,000.5", "清洗后的改动没有被标出来"
    hint = page.locator(".clean-panel .clean-hint").first.inner_text()
    assert "5 行" in hint, hint

    # 报告是流水线里现成的：提交之前就能看到它长什么样
    tab(page, "回归报告")
    page.wait_for_selector(".clean-report h1", timeout=60000)
    assert page.locator(".clean-report h2").count() == 10, "报告章节数不对"
    report = page.locator(".clean-report").inner_text()
    assert "回退" in report, "试跑报告里没有回退统计"
    assert "回退样本" in report, "默认（报告明细条数 = 8）应该列出回退明细"

    # —— 报告明细条数 = 0：十节都在，只是两张明细表不列，并写清明细在哪
    open_step(page, 7)
    page.locator("label:has-text('报告明细条数') input").fill("0")
    # 这一轮**换个数**（5 → 4）：试跑完的 `.clean-dry` 与上一份长得一模一样，行数变了
    # 才有确定的完成信号。顺带验了「界面上的行数真的发得出去」。
    open_step(page, 6)
    rows_input.fill("4")
    page.get_by_role("button", name="试跑前 4 行").click()
    page.wait_for_function("""() => {
      const box = document.querySelector('.clean-dry');
      return !!box && box.innerText.includes('读入 4 行');
    }""", timeout=180000)
    # 试跑完会自动切回「数据预览」（左边点了试跑，要看的就是前后对比）—— 这是产品行为，
    # 不是测试的绊脚石：不等它切完就去读报告，读到的是上一份，`wait_for_function` 会白等。
    # 只取四个 Tab（`>` 子选择器）：来源开关（试跑预览 / 任务结果）也住在 `.clean-tabs` 里，
    # 它同样用 `.active` 标当前来源。
    assert page.locator(".clean-tabs > button.active").inner_text() == "数据预览"
    tab(page, "回归报告")
    page.wait_for_selector(".clean-report h1", timeout=60000)
    assert page.locator(".clean-report h2").count() == 10, "明细条数不该动报告的十个章节"
    report = page.locator(".clean-report").inner_text()
    assert "读入 4 行" in report, "报告还是上一份（这一次试跑了 4 行）"
    assert "回退样本" not in report, "设成 0 以后还列了回退明细"
    assert "按设置未列样本明细" in report, "不列明细时该说明这是设置生效的结果"
    assert "fallback.jsonl" in report, "不列明细时要说清逐条明细在下载包里"

    # —— 自定义函数的例子：不会写的人点开就能抄；「用这段」要连模式与字段一起换掉
    open_step(page, 3)
    card = page.locator(".clean-fn").last
    card.locator(".clean-guide-toggle").click()
    samples = card.locator(".clean-sample")
    assert samples.count() == 3, samples.count()
    for at in range(3):
        code = samples.nth(at).locator("pre").inner_text()
        assert code.startswith("def transform("), code[:60]
    # 第 2 个例子讲的是「返回 None = 这一行不改」—— 这条语义光看文字猜不出来
    samples.nth(1).get_by_role("button", name="用这段").click()
    assert "return None" in wait_source(page, card, "return None")
    inputs = card.locator(".clean-in-fields .clean-check.on span").all_inner_texts()
    assert inputs == ["城市"], inputs
    # 第 3 个例子是整列模式：模式与输入字段都得跟着换（只换源码的话贴进去必然跑不通）
    samples.nth(2).get_by_role("button", name="用这段").click()
    assert card.locator("select").nth(1).input_value() == "column"
    inputs = card.locator(".clean-in-fields .clean-check.on span").all_inner_texts()
    assert inputs == ["姓名"], inputs
    source = wait_source(page, card, "def transform(columns):")
    assert "[str(v).strip() for v in values]" in source, source[:200]
    # 走到这儿左侧已经出现过「函数卡片 + 试跑按钮 + 例子」这些最容易顺手塞一块预览的地方，
    # 正好确认一遍它们都是纯配置。
    assert_no_data_in_config(page)


def open_completions(page, expect=None):
    """按 Ctrl+Space 打开补全，等到候选真的渲染出来再返回全部候选文本。

    先 Escape 关掉上一轮的弹窗：弹窗容器在文档变化后不一定消失（CM 会拿新前缀重新过滤），
    `wait_for_selector` 会立刻命中那个**陈旧**的弹窗，读到的是上一轮的候选。
    """
    page.keyboard.press("Escape")
    page.locator(".cm-tooltip-autocomplete").wait_for(state="detached", timeout=10000)
    page.keyboard.press("Control+Space")
    if expect is None:
        page.wait_for_selector(".cm-tooltip-autocomplete", timeout=15000)
    else:
        page.locator(".cm-tooltip-autocomplete .cm-completionLabel") \
            .filter(has_text=expect).first.wait_for(timeout=15000)
    return completion_labels(page)


def phase_fn_editor(page):
    """编辑器本身：补全的词表、静态检查的行号、格式化、逐行试跑。

    这一轮盯的是「界面能点的」和「后端能跑的」是不是同一份。补全里出现 `open` / `eval` /
    裸 `import`，就等于在教用户写一个必然被闸门拒掉的函数 —— 这三个名字都来自
    `@codemirror/lang-python` 自带的那份完整内置表，`autocompletion({override})` 一断就会
    冒出来，所以反向断言比正向断言更要紧。
    """
    open_step(page, 3)
    # 一张**新加的**卡片：上一轮那张里还留着「用这段」填进去的 column 模式源码，
    # 在它上面做这些断言会被上一轮的状态污染。
    card = add_function(page, NOOP, timeout_s=20)

    # 工具条：控件底边对齐（label 是列式的，持平靠工具条的 align-items:flex-end），
    # 且「试跑行数」的输入框不能被压扁 —— 不定宽时它只有 26px，四个字比它还宽。
    boxes = [item.bounding_box() for item in card.locator(".clean-fn-actions > *").all()]
    bottoms = [round(box["y"] + box["height"]) for box in boxes]
    assert max(bottoms) - min(bottoms) <= 2, f"卡片工具条没有持平：{boxes}"
    rows_input = card.locator("label:has-text('试跑行数') input").bounding_box()
    assert rows_input["width"] >= 80, f"「试跑行数」的输入框被压扁了：{rows_input}"

    # ① 字符串里只该出现**勾选过的字段**：没勾的运行时读不到、也写不进去。
    #    顺便验弹窗没被 `.clean-step{overflow:hidden}` 裁掉（Playwright 的可见性判断
    #    不看祖先裁切，只有命中测试能验）。
    set_fn_source(page, card, "def transform(row):\n    return row['")
    labels = open_completions(page, expect="手机")
    assert "手机" in labels, labels
    details = page.locator(".cm-tooltip-autocomplete .cm-completionDetail").all_inner_texts()
    assert any("输入字段" in item for item in details), details
    hit = popup_hit_test(page)
    assert hit["found"] and hit["inside"] and hit["inViewport"], f"补全弹窗被裁掉了：{hit}"
    assert hit["width"] > 40 and hit["height"] > 10, f"补全弹窗没有尺寸：{hit}"

    # ② 代码上下文里给的必须**全部**来自后端白名单（+ Python 关键字 + 文档内的名字）。
    #    前缀挑 `o`：真白名单里有一批（bool / ord / sorted / round…），而漏进来的那份
    #    完整内置表会多出 open / compile / globals —— 补全只渲染前 100 条，用带前缀的
    #    小弹窗才保证这些名字**渲染得到**，否则断言会「因为没渲染到」而假通过。
    set_fn_source(page, card, "def transform(row):\n    return o")
    labels = open_completions(page, expect="ord")
    vocab = page.evaluate("() => fetch('/api/clean/meta').then(r => r.json())")["python"]
    allowed = (set(vocab["builtins"]) | {f"import {name}" for name in vocab["modules"]}
               | PY_KEYWORDS | {"row", "columns", "transform"})
    leaked = sorted(set(labels) & set(LEAKED))
    assert not leaked, f"补全里冒出了真白名单里没有的名字 {leaked}（autocompletion 的 override 断了吗）"
    assert set(labels) <= allowed, f"补全里有词表之外的东西：{sorted(set(labels) - allowed)}"
    code_labels = labels

    # ③ 「导入」只给白名单模块的成品片段，不给裸 `import`：它后面那个占位符用户填什么
    #    都会被闸门拒掉（`import os` 就是这么写出来的）。
    set_fn_source(page, card, "def transform(row):\n    return im")
    labels = open_completions(page, expect="import math")
    assert "import" not in labels, f"补了裸 import：{labels}"
    fragments = [item for item in labels if item.startswith("import ")]
    assert {item.split(" ", 1)[1] for item in fragments} <= set(vocab["modules"]), fragments
    assert "import math" in fragments, fragments

    # ④ 静态检查：少个冒号 → 卡片里列出**第几行**；改对之后必须自己消失
    #    （这条同时是「陈旧诊断」的护栏：问题列表是从编辑器当前诊断里读的，不是攒的）
    set_fn_source(page, card, TYPO)
    problem = card.locator(".clean-fn-problems li").first
    problem.wait_for(timeout=30000)
    text = problem.inner_text()
    assert "第 1 行" in text, text
    set_fn_source(page, card, NOOP)
    card.locator(".clean-fn-problems").wait_for(state="detached", timeout=30000)

    # ⑤ 格式化：环境里没有 ruff 就**不该有这个按钮**（一个必然报错的按钮比没有按钮更糟）
    format_button = card.get_by_role("button", name="格式化", exact=True)
    if vocab["formatter"]["available"]:
        set_fn_source(page, card, MESSY)
        format_button.click()
        formatted = wait_source(page, card, "value = row[")
        assert formatted.strip() != MESSY.strip(), "点了格式化，源码却没变"
        assert "\n    value = row[" in formatted, formatted
        print("格式化 =", formatted.strip().splitlines()[1])
    else:
        assert format_button.count() == 0, "没有 ruff，却渲染了必然报错的「格式化」按钮"
        print("格式化 = 跳过（环境里没有 ruff）")

    # ⑥ 试跑：逐行进出 + `print` 输出。样本是**文件原值**，所以进出都能对到具体行。
    #    触发它的两个控件（试跑行数 + 按钮）留在函数卡片里 —— 那是写代码的地方；
    #    结果面板整块搬到了结果区，所以下面的接收方是 `.clean-stage` 而不是 `card`。
    set_fn_source(page, card, TALKS)
    card.locator("label:has-text('试跑行数') input").fill("3")
    card.get_by_role("button", name="用真实数据试跑这个函数").click()
    panel = page.locator(".clean-stage .clean-panel")
    table = panel.locator(".clean-fn-table").first
    table.wait_for(timeout=120000)
    rows = table.locator("tbody tr")
    assert rows.count() == 3, f"试跑行数没生效：{rows.count()}"
    head = panel.locator(".clean-fn-test .clean-hint").first.inner_text()
    assert "c.csv" in head and "前 3 行" in head, head
    first = rows.first.locator("td").all_inner_texts()
    assert first == ["1", "手机=13800000000", "手机=138****0000", ""], first
    printed = panel.locator(".clean-fn-print").inner_text()
    assert "看到 13800000000" in printed, printed
    #    函数试跑是它自己的一个来源：切过去之后四个任务页签就该收起来（试跑没有日志/统计）。
    assert active_source(page) == "函数试跑", active_source(page)
    assert page.locator(".clean-tabs > button").count() == 0, "函数试跑面板下不该有任务页签"
    assert_no_data_in_config(page)
    print("补全 =", len(code_labels), "条代码候选 /", len(fragments), "个模块片段 / 静态检查 =",
          text.strip()[:40], "/ 试跑 =", first[1], "→", first[2])


def llm_estimate_payload() -> dict:
    """一份**由真估算器算出来**的、带大模型字段的 `/estimate` 响应。

    这组数是用户实测的那次：50 行 / 批量 10 / 并发 1（本地 ollama 一次只跑一个请求）/
    历史延迟 4856.5 毫秒（5 个样本）。实测 19.4–23.6 秒，估算区间 17–48 秒罩得住。

    用真估算器而不是手写 JSON：`as_dict()` 的字段名一改，这里跟着红，不会悄悄失效。
    """
    from app.clean_llm import LlmEndpoint, LlmPool, estimate_llm
    from app.clean_models import (CleanTaskConfig, FieldSpec, GenerateRule, LlmTaskOptions,
                                  SourceSpec)

    config = CleanTaskConfig(
        source=SourceSpec(mode="upload", upload_id="u1"),
        fields=[FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}",
                                                             batch_size=10))],
        llm=LlmTaskOptions(endpoint_ids=["a"]))
    pool = LlmPool([LlmEndpoint(id="a", url="https://a.test/v1", model="m", max_concurrency=1)])
    pool.report_ok(pool.endpoints[0], 4.856, rows=10)
    return {
        "rows": 50, "rows_exact": True, "files": [], "source": {},
        "estimate": estimate_llm(config, pool, rows=50).as_dict(),
        "notes": [],
        # 模板只读这两个列表（逐端点那行从 selected 来，缺失告警从 missing 来）
        "endpoints": {"selected": [], "usable": 1, "concurrency": 1, "missing": []},
    }


def phase_estimate_panel(page):
    """预估面板上「约 N 轮」与「按每次 X 的历史延迟估算」这两句。

    本套件的配置没有大模型字段（真调那几个字段要走真端点），真调用只会给出「按运行时实际
    速率」，这两句根本渲染不出来 —— 所以先拿上面那份响应替一下，验证文案；再放开真调用，
    验证没有大模型字段时这两句**不出现**（而不是显示成「约 0 轮」）。
    """
    payload = llm_estimate_payload()
    stub = "http://127.0.0.1:5174/api/clean/estimate"
    open_step(page, 7)
    # 注册得比 main() 里那条通配 307 晚：Playwright 后注册的先匹配，这条只吃掉这一个 URL
    page.route(stub, lambda route: route.fulfill(json=payload))
    try:
        page.get_by_role("button", name="获取处理预估").click()
        panel = page.locator(".clean-estimate")
        panel.wait_for(timeout=60000)
        text = panel.inner_text()
        # 5 次调用 1 并发 → 5 轮；每轮 4.856 秒 → 17 秒 – 48 秒
        assert "约 5 轮" in text, text
        assert "约 17 秒 – 48 秒" in text, text
        assert "按每次 5 秒的历史延迟估算" in text, text
        assert "并发 1" in text, text
    finally:
        page.unroute(stub)

    page.get_by_role("button", name="获取处理预估").click()
    panel = page.locator(".clean-estimate")
    panel.wait_for(timeout=120000)
    # 等真响应把替上去的那份换掉：没有大模型字段时后端给的是「按运行时实际速率」。
    # 超时就是这次调用失败了 —— 面板还停在替上去的那份上，别让它被读成「真响应也没有轮数」。
    try:
        page.wait_for_function("""() => {
          const node = document.querySelector('.clean-estimate');
          return node && node.textContent.includes('实际速率');
        }""", timeout=120000)
    except PlaywrightTimeoutError:
        raise AssertionError("真预估没回来，面板还停在替上去的那份："
                             + panel.inner_text().replace("\n", " | "))
    text = panel.inner_text()
    assert "轮" not in text, f"没有大模型字段却给出了轮数：{text}"
    assert "历史延迟" not in text, text
    print("预估面板 =", " / ".join(line for line in text.splitlines() if line)[:120])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clean-browser-") as raw:
        root = Path(raw)
        source = fixtures(root)
        data = root / "data"
        # 数据根与输入根都在临时目录里：私有实例不碰本机真实数据，也不会被别处的
        # 同名任务干扰（这正是 CLEAN_DATA_DIR 存在的理由）。
        os.environ["CLEAN_DATA_DIR"] = str(data)
        os.environ["CLEAN_ALLOWED_ROOTS"] = str(source)

        from app.clean_api import make_router

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
        assert server.started, "私有实例没起来（端口 8001 被占了？）"

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000},
                                        reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def redirect(route):
                    url = route.request.url
                    if "/api/clean/" in url:
                        route.fulfill(status=307, headers={
                            "Location": url.replace("127.0.0.1:5174", f"127.0.0.1:{API}")})
                    else:
                        # 别的工具的轮询（`App.vue` 那个 2 秒全局定时器）与本套件无关：
                        # 断掉，既不打扰本机真实后端，也不往日志里灌 404
                        route.abort()

                # 只拦开发服务器那个源。拦宽了会把自己的 307 目标再拦一遍 → 死循环
                page.route("http://127.0.0.1:5174/api/**", redirect)
                page.goto(f"{DEV}/#clean", wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                page.locator(".clean-workspace").wait_for(timeout=30000)

                uploaded = phase_upload(page, source)
                resumed = phase_resume(page, source)
                phase_whitelist(page, root)
                phase_dry_preview(page, source)
                phase_fn_editor(page)
                phase_estimate_panel(page)

                # 窄屏之前再抽一次：390 宽下五个来源开关要换行，左侧更不该再背着一块预览。
                assert_no_data_in_config(page)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(400)
                overflow = page.evaluate("document.documentElement.scrollWidth-innerWidth")
                assert overflow <= 0, f"390px 横向溢出 {overflow}px"

                for name in (uploaded, resumed):
                    row = page.locator(".clean-task").filter(has_text=name).first
                    row.get_by_role("button", name="删除").click()
                    page.get_by_role("button", name="确认删除").click()
                    page.wait_for_function("""(name) => ![...document.querySelectorAll('.clean-task')]
                      .some(node => node.textContent.includes(name));""", arg=name)
                assert not list((data / "tasks").iterdir()), "删了任务但产物目录还在"
                assert not errors, errors
                print("PASS: 上传 / 服务器路径 / 规则 / 自定义函数 / 试跑 / 预估 / 提交 / 取消 / 续跑"
                      " / 表头映射实时预览（含新列「新增」标记）/ 无大小上限 / 前后对比 / 实时日志 / 报告表格 / 统计"
                      " / 白名单 / 按钮持平 / 工具栏 ×4 持平"
                      " / 统计横拖 / 试跑预览 5 行 / 明细条数 = 0 / 例子填入 / 编辑器补全词表"
                      " / 补全弹窗不被裁 / 静态检查行号 / 格式化 / 函数试跑 / 390 宽 / 清理"
                      " / 左侧零数据块（源数据·函数试跑·试跑预览·处理预估都在结果区）"
                      " / 改解析选项不把面板拽走 / 来源开关只在数据预览下"
                      " / 预估面板的轮数与历史延迟 / 无大模型字段时不显示轮数")
                page.unroute_all(behavior="wait")
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
