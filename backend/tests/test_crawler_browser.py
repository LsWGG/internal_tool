import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.crawler_browser import CrawlerBrowser, browser_scope
from app.crawler_manager import CrawlerTaskManager


class FakeContext:
    def __init__(self, options):
        self.options = options
        self.closed = False
        self.pages = 0

    def new_page(self):
        self.pages += 1
        return self

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = False
        self.connected = True
        self.fail_close = False

    def is_connected(self):
        return self.connected and not self.closed

    def new_context(self, **options):
        context = FakeContext(options)
        self.contexts.append(context)
        return context

    def close(self):
        if self.fail_close:
            raise RuntimeError("驱动已经没了")
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launches = []

    def launch(self, **options):
        self.launches.append(options)
        return self.browser


class FakePlaywright:
    def __init__(self):
        self.browser = FakeBrowser()
        self.chromium = FakeChromium(self.browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeStarter:
    def __init__(self):
        self.engine = FakePlaywright()
        self.start_calls = 0

    def start(self):
        self.start_calls += 1
        return self.engine


class BrowserTestCase(unittest.TestCase):
    def setUp(self):
        self.starter = FakeStarter()
        patcher = patch("playwright.sync_api.sync_playwright", return_value=self.starter)
        self.addCleanup(patcher.stop)
        patcher.start()

    @property
    def chrome(self):
        return self.starter.engine.chromium


class LaunchTests(BrowserTestCase):
    def test_constructing_starts_nothing(self):
        CrawlerBrowser()
        self.assertEqual(self.starter.start_calls, 0)

    def test_browser_process_is_reused_across_contexts(self):
        """一次任务几十个页面 —— 每个页面都冷启动一次 Chromium 才是要解决的问题。"""
        engine = CrawlerBrowser()
        engine.new_context(viewport={"width": 1440, "height": 1000})
        engine.new_context(viewport={"width": 1440, "height": 1000})
        self.assertEqual(len(self.chrome.launches), 1)
        self.assertEqual(len(self.chrome.browser.contexts), 2)

    def test_context_options_reach_playwright_unchanged(self):
        engine = CrawlerBrowser()
        context = engine.new_context(user_agent="UA", locale="zh-CN",
                                     viewport={"width": 800, "height": 600})
        self.assertEqual(context.options,
                         {"user_agent": "UA", "locale": "zh-CN",
                          "viewport": {"width": 800, "height": 600}})

    def test_headless_flag_is_forwarded(self):
        CrawlerBrowser(headless=False).new_context()
        self.assertEqual(self.chrome.launches[0]["headless"], False)

    def test_first_proxy_is_used_and_scheme_normalised(self):
        CrawlerBrowser(proxies=["1.2.3.4:8080", "5.6.7.8:9090"]).new_context()
        self.assertEqual(self.chrome.launches[0]["proxy"], {"server": "http://1.2.3.4:8080"})

    def test_proxy_with_scheme_is_kept(self):
        CrawlerBrowser(proxies=["socks5://1.2.3.4:1080"]).new_context()
        self.assertEqual(self.chrome.launches[0]["proxy"], {"server": "socks5://1.2.3.4:1080"})

    def test_no_proxy_means_no_proxy_option(self):
        CrawlerBrowser(proxies=["", "  "]).new_context()
        self.assertNotIn("proxy", self.chrome.launches[0])

    def test_dead_browser_is_relaunched(self):
        """进程崩了不能把整个任务拖死：丢掉重开一个。"""
        engine = CrawlerBrowser()
        engine.new_context()
        self.chrome.browser.connected = False
        with self.assertLogs("app.crawler_browser", level="WARNING"):
            engine.new_context()
        self.assertEqual(len(self.chrome.launches), 2)


class ThreadOwnershipTests(BrowserTestCase):
    def test_use_from_another_thread_raises(self):
        """线程池有 3 个 worker，跨线程用 Playwright 同步 API 会以难查的方式失败。"""
        engine = CrawlerBrowser()
        engine.new_context()
        failures = []

        def worker():
            try:
                engine.new_context()
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        self.assertIn("创建线程", str(failures[0]))

    def test_idle_engine_handed_to_another_thread_is_refused_without_launching(self):
        engine = CrawlerBrowser()
        failures = []

        def worker():
            try:
                engine.new_context()
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.starter.start_calls, 0)


class CloseTests(BrowserTestCase):
    def test_close_is_idempotent(self):
        engine = CrawlerBrowser()
        engine.new_context()
        engine.close()
        engine.close()
        self.assertEqual(self.starter.engine.stopped, True)
        self.assertTrue(self.chrome.browser.closed)

    def test_close_on_an_unused_engine_does_nothing(self):
        CrawlerBrowser().close()
        self.assertEqual(self.starter.start_calls, 0)

    def test_close_never_raises(self):
        """在 finally 里抛异常会盖掉真正的失败原因。"""
        engine = CrawlerBrowser()
        engine.new_context()
        self.chrome.browser.fail_close = True
        with self.assertLogs("app.crawler_browser", level="WARNING"):
            engine.close()


class BrowserScopeTests(BrowserTestCase):
    def test_injected_browser_is_only_borrowed(self):
        shared = CrawlerBrowser()
        shared.new_context()
        with browser_scope(shared, ["1.2.3.4:8080"]) as engine:
            self.assertIs(engine, shared)
            engine.new_context()
        self.assertFalse(self.chrome.browser.closed)

    def test_scope_without_a_browser_builds_and_closes_its_own(self):
        with browser_scope(None, ["1.2.3.4:8080"]) as engine:
            engine.new_context()
            self.assertTrue(engine.running)
        self.assertTrue(self.chrome.browser.closed)
        self.assertEqual(self.chrome.launches[0]["proxy"], {"server": "http://1.2.3.4:8080"})

    def test_scope_closes_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with browser_scope(None) as engine:
                engine.new_context()
                raise ValueError("页面拿不到")
        self.assertTrue(self.chrome.browser.closed)


class RunBrowserWiringTests(unittest.TestCase):
    """_run 必须从头到尾用同一个浏览器 —— 这是「每页一次」降成「每任务一次」的关键。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager.data_dir = Path(temporary.name)
        self.manager.lock = threading.RLock()
        self.manager.controls = {}
        self.manager.active_runs = {}
        self.manager.task_secrets = {}
        self.manager._save = MagicMock()
        self.manager._checkpoint = MagicMock()
        self.manager._extract = MagicMock(side_effect=lambda url, soup, fields: {"url": url})
        self.seen = []

        def discover(seeds, request, task_id=None, browser=None):
            self.seen.append(browser)
            return list(seeds)

        def fetch_html(url, request, browser=None):
            self.seen.append(browser)
            return "<html></html>"

        self.manager._discover_news_urls = discover
        self.manager._fetch_html = fetch_html
        self.manager.tasks = {"task": {"id": "task", "runs": [], "result": None, "request": {
            "source": "news", "urls": ["https://example.com/a", "https://example.com/b"],
            "fields": [{"name": "url"}], "output_format": "json", "delay_seconds": 0,
        }}}

    def test_discovery_and_extraction_share_one_browser_and_close_it(self):
        closed = []
        original = CrawlerBrowser.close

        def record_and_close(engine):
            closed.append(engine)
            original(engine)

        with patch.object(CrawlerBrowser, "close", autospec=True, side_effect=record_and_close):
            self.manager._run("task")

        self.assertEqual(len(self.seen), 3)          # 1 次发现 + 2 次取正文
        shared = self.seen[0]
        self.assertIsNotNone(shared)
        self.assertTrue(all(item is shared for item in self.seen))
        self.assertEqual(closed, [shared])           # 收尾只关一次，且关的就是那一个

    def test_run_failure_still_closes_the_browser(self):
        self.manager._fetch_html = MagicMock(side_effect=RuntimeError("取不到"))
        closed = []
        original = CrawlerBrowser.close

        def record_and_close(engine):
            closed.append(engine)
            original(engine)

        with patch.object(CrawlerBrowser, "close", autospec=True, side_effect=record_and_close):
            self.manager._run("task")
        self.assertEqual(self.manager.tasks["task"]["status"], "failed")
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
