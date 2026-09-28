"""抖音扫码登录：点一下按钮，弹出浏览器，扫完自动把 Cookie 存下来。

抖音对未登录请求一律返回空数据 —— 四个采集模式都一样，而让非技术用户从开发者工具里
手工复制 Cookie 不可行。所以这里开一个**可见**的浏览器窗口，用户自己扫码，登录成功后
把上下文里的 Cookie 抓出来落盘（0o600，走 crawler_secrets 那套原子写）。此后所有抖音
任务创建与数据预览都会自动带上它（见 CrawlerTaskManager 的回填）。

三条边界：
  * 不绕过验证码：只是把登录页原样渲染出来，扫码在抖音自己的页面里完成。
  * Cookie 明文不外流：`state()` 只回状态与保存时间，值不进任何返回值。
  * 协作式取消：取消只是置位，工作线程在自己的轮询点退出 —— 浏览器只能由创建它的
    线程关闭（CrawlerBrowser 的线程守卫），所以没有"立刻掐断"这回事。

状态机 `idle → running → done | failed | cancelled`，终态可以再 start 刷新一轮。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .crawler_browser import CrawlerBrowser
from .crawler_http import UA_DOUYIN
from .crawler_secrets import GLOBAL_COOKIE_TASK_ID, load_secrets, secrets_file, write_secret

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.douyin.com/"
# 登录成功的判据：页面把 sessionid 种进 cookie 才算数。其它键可能是访客态就有的。
SESSION_COOKIE_NAME = "sessionid"
MS_TOKEN_NAME = "msToken"
POLL_INTERVAL_S = 1.0
LOGIN_TIMEOUT_S = 300
# 导航超时压到 30 秒：取消失效的窗口期最多就这么长，用户体验上还能接受。
GOTO_TIMEOUT_S = 30

# 拼 Cookie 头用的名字白名单，和 douyin_profiles 的解析口径一致。
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# msToken 常常不落在 cookie jar 里（页面把它种在 storage），缺了就从页面里捞一次。
_MS_TOKEN_SCRIPT = """
() => {
  const hit = document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith('msToken='));
  if (hit) return hit.slice('msToken='.length);
  for (const store of [window.localStorage, window.sessionStorage]) {
    try {
      const value = store.getItem('msToken');
      if (value) return value;
    } catch (error) { /* 隐私模式下碰一下就抛，换下一个 */ }
  }
  return '';
}
"""

_lock = threading.Lock()
# 模块态。thread 那一栏是身份标记：只有它记着的那个线程才有权写终态。
_state = {"state": "idle", "message": "", "thread": None}
_cancelled = threading.Event()
_confirmed = threading.Event()


def _finish(state, message):
    """终态的唯一收口。上一轮线程的迟到收尾在这里被丢弃，不会覆盖新一轮的 running。"""
    with _lock:
        if _state["thread"] is not threading.current_thread():
            return
        _state.update({"state": state, "message": message})


def _is_closed(exc):
    """用户直接关掉窗口时 Playwright 抛的异常。按字符串认，不为它 import playwright。"""
    return "TargetClosed" in type(exc).__name__ or "has been closed" in str(exc)


def _cookie_header(cookies):
    """拼 Cookie 请求头：按名排序、跳过空值与带分隔符的值（否则拼出来是个坏 header）。"""
    pairs = {}
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        name, value = str(item.get("name") or ""), str(item.get("value") or "")
        if not _COOKIE_NAME.fullmatch(name):
            continue
        if not value or any(char in value for char in ";\r\n"):
            logger.warning("跳过无法安全拼进 Cookie 头的键：%s", name)
            continue
        pairs.setdefault(name, value)
    return "; ".join(f"{name}={pairs[name]}" for name in sorted(pairs))


def _douyin_cookies(cookies):
    """只留抖音域下的。全量保留（内容本来就是我们自己灌进去的），不另立键白名单。"""
    kept = []
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("domain") or "").lstrip(".").lower()
        if host == "douyin.com" or host.endswith(".douyin.com"):
            kept.append(item)
    return kept


def _page_ms_token(page):
    """从页面里捞 msToken。捞不到不算失败 —— 消费端本来就有兜底。"""
    try:
        return str(page.evaluate(_MS_TOKEN_SCRIPT) or "").strip()
    except Exception as exc:
        logger.warning("读取 msToken 失败（已忽略）：%s", exc)
        return ""


def _wait_for_login(context):
    """等扫码完成。返回登录后的 storage_state；返回 None 表示状态已经收口（取消/超时/关窗）。"""
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while True:
        if _cancelled.is_set():
            _finish("cancelled", "已取消，浏览器窗口正在关闭")
            return None
        try:
            names = {str(item.get("name")) for item in context.cookies()}
            if _confirmed.is_set() or SESSION_COOKIE_NAME in names:
                return context.storage_state()
        except Exception as exc:
            if _is_closed(exc):
                _finish("failed", "浏览器窗口已关闭，登录未完成")
                return None
            raise
        if time.monotonic() >= deadline:
            _finish("failed", f"登录等待超时（{LOGIN_TIMEOUT_S // 60} 分钟），请重试")
            return None
        time.sleep(POLL_INTERVAL_S)


def _login_worker(data_dir):
    """专用线程里跑完整轮登录。浏览器只在这个线程里创建和使用。"""
    engine = CrawlerBrowser(headless=False)
    try:
        context = engine.new_context(user_agent=UA_DOUYIN, locale="zh-CN",
                                     viewport={"width": 1280, "height": 900})
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_S * 1000)
        except Exception as exc:
            if not _is_closed(exc):
                raise
            # 窗口在首页渲染完之前就被关了，用户就是不想登录。
            _finish("failed", "浏览器窗口已关闭，登录未完成")
            return

        state = _wait_for_login(context)
        if state is None:
            return

        cookies = _douyin_cookies(state.get("cookies"))
        names = {str(item.get("name")) for item in cookies}
        if SESSION_COOKIE_NAME not in names:
            # confirm 是用户手动点的，"点了但没登录上"是常见误操作，这里得说清楚。
            _finish("failed", "未检测到登录成功：请在窗口里完成扫码并确认登录后重试")
            return
        if MS_TOKEN_NAME not in names:
            token = _page_ms_token(page)
            if token:
                cookies.append({"name": MS_TOKEN_NAME, "value": token,
                                "domain": ".douyin.com", "path": "/"})

        write_secret(data_dir, GLOBAL_COOKIE_TASK_ID, "douyin_cookie", _cookie_header(cookies))
        _finish("done", "登录成功，Cookie 已保存")
    except ImportError:
        _finish("failed", "未安装浏览器组件（playwright），无法打开登录窗口")
    except Exception as exc:
        # 固定文案：异常消息里可能夹着 cookie 值，不能往外抛。
        logger.warning("抖音扫码登录失败：%s", exc)
        _finish("failed", "登录过程出错，请重试；若反复失败可手动填写 Cookie")
    finally:
        engine.close()


def start_login(data_dir):
    """开一个可见浏览器等扫码。已经在跑就直接报错，不做排队。"""
    with _lock:
        if _state["state"] == "running":
            raise ValueError("登录窗口已经打开，请在弹出的浏览器窗口里完成扫码")
        _cancelled.clear()
        _confirmed.clear()
        thread = threading.Thread(target=_login_worker, args=(Path(data_dir),),
                                  daemon=True, name="douyin-login")
        _state.update({"state": "running", "message": "等待扫码登录…", "thread": thread})
    thread.start()
    return state(data_dir)


def _saved_at(data_dir):
    try:
        stamp = datetime.fromtimestamp(secrets_file(data_dir, GLOBAL_COOKIE_TASK_ID).stat().st_mtime)
    except OSError:
        return ""
    return stamp.strftime("%Y-%m-%d %H:%M")


def state(data_dir):
    """给前端的只读快照。saved/saved_at 每次从磁盘重算，所以重启后标记仍然为真。"""
    with _lock:
        snapshot = {"state": _state["state"], "message": _state["message"]}
    saved = bool(load_secrets(data_dir, GLOBAL_COOKIE_TASK_ID).get("douyin_cookie"))
    snapshot.update({"saved": saved, "saved_at": _saved_at(data_dir) if saved else ""})
    return snapshot


def confirm_login():
    """用户主动说「我登好了」—— 扫码之外的兜底通道（比如他走的是密码登录）。"""
    with _lock:
        if _state["state"] != "running":
            raise ValueError("当前没有正在进行的扫码登录")
    _confirmed.set()


def cancel_login():
    with _lock:
        if _state["state"] != "running":
            raise ValueError("当前没有正在进行的扫码登录")
    _cancelled.set()
