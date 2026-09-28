"""采集任务里的 Playwright 复用。

原先 5 处样板各自 `sync_playwright()` → `chromium.launch()` → 用完 `browser.close()`，
每取一个页面就冷启动一次 Chromium（0.5 秒起步，还要先拨代理）。一次任务动辄几十个
页面，大半时间花在重复启动上。

这里的粒度是**浏览器进程复用、上下文每次新建**：
  * 进程贵，复用；
  * 上下文便宜，且承载 cookie / UA / 视口，每次调用都新建 —— 与原先「一次调用一个
    浏览器」的隔离语义一致，上一个页面的登录态不会串到下一个页面。

线程归属：Playwright 的同步 API 跨线程使用会以各种难查的方式失败，所以实例记住创建
它的线程，别的线程一进来就报错。线程池有 3 个 worker，这个守卫是必要的。

不绕过验证码：这里只负责把页面渲染出来，等待站点自身的正常跳转，不做任何挑战解析。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class CrawlerBrowser:
    """一个采集浏览器进程。构造时不启动任何东西，首次使用才真的拉起 Chromium。"""

    def __init__(self, proxies=(), headless=True):
        self.proxies = [str(item).strip() for item in (proxies or []) if str(item).strip()]
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._owner = threading.get_ident()

    @property
    def running(self):
        return self._browser is not None

    def _ensure(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("采集浏览器只能在其创建线程内使用；请勿跨线程传递")
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        if self._browser is not None:
            # 进程已经掉了（崩溃或被系统回收）。丢掉重开，不让剩下的页面跟着一起废。
            logger.warning("采集浏览器进程已断开，重新启动一个")
            self.close()
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        try:
            options = {"headless": self.headless}
            if self.proxies:
                proxy = self.proxies[0]
                options["proxy"] = {"server": proxy if "://" in proxy else "http://" + proxy}
            self._browser = playwright.chromium.launch(**options)
        except BaseException:
            playwright.stop()
            raise
        self._playwright = playwright
        return self._browser

    def new_context(self, **options):
        """参数原样交给 Playwright，UA / locale / viewport 由调用方按站点自己定。"""
        return self._ensure().new_context(**options)

    def close(self):
        """幂等。清理路径不抛异常 —— 在 finally 里抛会盖掉真正的失败原因。"""
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = None
        for closer in (browser.close if browser is not None else None,
                       playwright.stop if playwright is not None else None):
            if closer is None:
                continue
            try:
                closer()
            except Exception as exc:
                logger.warning("关闭采集浏览器时出错（已忽略）：%s", exc)


@contextmanager
def browser_scope(browser, proxies=()):
    """借一个浏览器给这次调用：外面传了（一次任务共用一个）就只借用，没传就自建自关。"""
    if browser is not None:
        yield browser
        return
    owned = CrawlerBrowser(proxies)
    try:
        yield owned
    finally:
        owned.close()
