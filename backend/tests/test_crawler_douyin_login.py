import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import crawler_douyin_login as login
from app.crawler_secrets import GLOBAL_COOKIE_TASK_ID, load_secrets, secrets_file, write_secret

SESSION_JAR = [
    {"name": "sessionid", "value": "s3cret", "domain": ".douyin.com", "path": "/"},
    {"name": "ttwid", "value": "t1", "domain": ".douyin.com", "path": "/"},
]
# 广告域下的 cookie 不属于登录态，不能带进请求头。
FOREIGN_JAR = [{"name": "tracker", "value": "x", "domain": ".doubleclick.net", "path": "/"}]


class TargetClosedError(Exception):
    """名字对齐 Playwright 的 TargetClosedError —— 被它按类名识别，不 import 真异常类。"""


class FakePage:
    def __init__(self, context):
        self.context = context

    def goto(self, url, **options):
        if self.context.fail_navigation:
            raise self.context.fail_navigation
        self.context.navigated.append((url, options))

    def evaluate(self, script):
        if self.context.fail_evaluate:
            raise self.context.fail_evaluate
        return self.context.page_storage.get("msToken", "")


class FakeContext:
    def __init__(self, options):
        self.options = options
        self.jar = []
        self.page_storage = {}
        self.navigated = []
        # 下面几个都是给用例设的"故障开关"和"登录进度"钩子。
        self.fail_navigation = None
        self.fail_evaluate = None
        self.fail_cookies = None
        self.when_polled = None

    def new_page(self):
        return FakePage(self)

    def cookies(self):
        if self.fail_cookies:
            raise self.fail_cookies
        if self.when_polled:
            self.when_polled(self)
        return list(self.jar)

    def storage_state(self):
        return {"cookies": list(self.jar)}


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = False
        self.connected = True
        # 故障开关挂在这里而不是上下文上：上下文是工作线程建的，用例抢不到那个时序。
        self.fail_navigation = None
        self.fail_cookies = None
        self.fail_evaluate = None

    def is_connected(self):
        return self.connected and not self.closed

    def new_context(self, **options):
        context = FakeContext(options)
        context.fail_navigation = self.fail_navigation
        context.fail_cookies = self.fail_cookies
        context.fail_evaluate = self.fail_evaluate
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launches = []
        self.fail_launch = None

    def launch(self, **options):
        if self.fail_launch:
            raise self.fail_launch
        self.launches.append(options)
        return self.browser


class FakePlaywright:
    def __init__(self):
        self.browser = FakeBrowser()
        self.chromium = FakeChromium(self.browser)

    def stop(self):
        pass


class FakeStarter:
    def __init__(self):
        self.engine = FakePlaywright()
        self.start_calls = 0

    def start(self):
        self.start_calls += 1
        return self.engine


class LoginTestCase(unittest.TestCase):
    """模块态是全局的，每条用例都得从 idle 起步；旧线程的收尾由身份守卫挡住。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

        login._state.update({"state": "idle", "message": "", "thread": None})
        login._cancelled.clear()
        login._confirmed.clear()
        self.addCleanup(login._cancelled.clear)
        self.addCleanup(login._confirmed.clear)

        self.starter = FakeStarter()
        patcher = patch("playwright.sync_api.sync_playwright", return_value=self.starter)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 默认给足超时，需要压缩时间的用例自己再覆盖。
        for name, value in (("POLL_INTERVAL_S", 0.01), ("LOGIN_TIMEOUT_S", 30),
                            ("GOTO_TIMEOUT_S", 5)):
            stub = patch.object(login, name, value)
            stub.start()
            self.addCleanup(stub.stop)

    @property
    def chrome(self):
        return self.starter.engine.chromium

    @property
    def context(self):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.chrome.browser.contexts:
                return self.chrome.browser.contexts[0]
            time.sleep(0.005)
        self.fail("浏览器上下文一直没建起来")

    def wait_for(self, expected, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if login._state["state"] == expected:
                return
            time.sleep(0.01)
        self.fail(f"等 {expected} 超时，当前是 {login._state['state']}：{login._state['message']}")

    def run_login(self, prepare):
        """跑完一整轮登录。prepare 拿到假上下文，用来安排"用户做了什么"。"""
        login.start_login(self.data_dir)
        prepare(self.context)
        self.wait_for("done")

    def header(self):
        return load_secrets(self.data_dir, GLOBAL_COOKIE_TASK_ID)["douyin_cookie"]


class LoginFlowTests(LoginTestCase):
    def test_scan_success_saves_cookie_owner_only(self):
        self.run_login(lambda context: setattr(context, "jar", list(SESSION_JAR)))
        path = secrets_file(self.data_dir, GLOBAL_COOKIE_TASK_ID)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # 顺序稳定、格式就是浏览器 Cookie 头的样子。
        self.assertEqual(self.header(), "sessionid=s3cret; ttwid=t1")

    def test_saved_state_never_carries_the_cookie_value(self):
        self.run_login(lambda context: setattr(context, "jar", list(SESSION_JAR)))
        snapshot = login.state(self.data_dir)
        self.assertTrue(snapshot["saved"])
        self.assertTrue(snapshot["saved_at"])
        self.assertNotIn("s3cret", json.dumps(snapshot, ensure_ascii=False))

    def test_foreign_domains_are_left_out_of_the_header(self):
        self.run_login(lambda context: setattr(context, "jar", list(SESSION_JAR + FOREIGN_JAR)))
        self.assertNotIn("tracker", self.header())

    def test_login_is_noticed_while_polling(self):
        """不点确认也应该能登进去：页面上扫码成功就会种下 sessionid。"""
        self.run_login(lambda context: setattr(context, "when_polled",
                                               lambda ctx: ctx.jar.extend(SESSION_JAR)))
        self.assertEqual(self.header(), "sessionid=s3cret; ttwid=t1")

    def test_login_page_is_opened_in_a_visible_window(self):
        self.run_login(lambda context: setattr(context, "jar", list(SESSION_JAR)))
        self.assertEqual(self.chrome.launches[0]["headless"], False)
        self.assertEqual(self.context.navigated[0][0], login.LOGIN_URL)
        self.assertEqual(self.context.options["locale"], "zh-CN")

    def test_timeout_without_a_login_saves_nothing(self):
        with patch.object(login, "LOGIN_TIMEOUT_S", 0.15):
            login.start_login(self.data_dir)
            self.wait_for("failed")
        self.assertIn("超时", login._state["message"])
        self.assertFalse(secrets_file(self.data_dir, GLOBAL_COOKIE_TASK_ID).exists())

    def test_cancel_stops_the_wait_and_closes_the_browser(self):
        login.start_login(self.data_dir)
        login.cancel_login()
        self.wait_for("cancelled")
        self.assertFalse(secrets_file(self.data_dir, GLOBAL_COOKIE_TASK_ID).exists())
        self.assertTrue(self.chrome.browser.closed)

    def test_confirm_without_a_real_login_reports_that_clearly(self):
        login.start_login(self.data_dir)
        login.confirm_login()
        self.wait_for("failed")
        self.assertIn("未检测到登录成功", login._state["message"])
        self.assertFalse(secrets_file(self.data_dir, GLOBAL_COOKIE_TASK_ID).exists())

    def test_closing_the_window_is_reported_as_such(self):
        self.chrome.browser.fail_cookies = TargetClosedError("Target page, context or browser has been closed")
        login.start_login(self.data_dir)
        self.wait_for("failed")
        self.assertIn("窗口已关闭", login._state["message"])
        self.assertTrue(self.chrome.browser.closed)

    def test_closing_the_window_during_navigation_is_not_a_crash(self):
        self.chrome.browser.fail_navigation = TargetClosedError("Target page has been closed")
        login.start_login(self.data_dir)
        self.wait_for("failed")
        self.assertIn("窗口已关闭", login._state["message"])

    def test_missing_playwright_is_reported_without_a_traceback(self):
        self.chrome.fail_launch = ImportError("No module named 'playwright'")
        login.start_login(self.data_dir)
        self.wait_for("failed")
        self.assertIn("未安装浏览器组件", login._state["message"])

    def test_internal_failure_does_not_leak_the_cookie_into_the_message(self):
        self.chrome.browser.fail_navigation = RuntimeError("boom sessionid=s3cret")
        login.start_login(self.data_dir)
        self.wait_for("failed")
        self.assertNotIn("s3cret", login._state["message"])

    def test_ms_token_is_fetched_from_the_page_when_the_jar_lacks_it(self):
        def log_in(context):
            context.jar.extend(SESSION_JAR)
            context.page_storage["msToken"] = "from-storage"

        self.run_login(log_in)
        self.assertIn("msToken=from-storage", self.header())

    def test_ms_token_failure_still_saves_the_cookie(self):
        def log_in(context):
            context.jar.extend(SESSION_JAR)
            context.fail_evaluate = RuntimeError("页面已经换掉了")

        self.run_login(log_in)
        self.assertNotIn("msToken", self.header())


class LoginControlTests(LoginTestCase):
    def test_second_start_is_refused_while_running(self):
        login.start_login(self.data_dir)
        with self.assertRaises(ValueError):
            login.start_login(self.data_dir)
        login.cancel_login()
        self.wait_for("cancelled")

    def test_confirm_and_cancel_are_refused_when_idle(self):
        for call in (login.confirm_login, login.cancel_login):
            with self.assertRaises(ValueError):
                call()

    def test_a_terminal_state_can_be_restarted(self):
        with patch.object(login, "LOGIN_TIMEOUT_S", 0.05):
            login.start_login(self.data_dir)
            self.wait_for("failed")
        login.start_login(self.data_dir)
        self.assertEqual(login.state(self.data_dir)["state"], "running")
        login.cancel_login()
        self.wait_for("cancelled")

    def test_a_stale_worker_cannot_overwrite_the_current_run(self):
        """上一轮线程收尾晚了一步，不能把新一轮的 running 打成失败。"""
        stale = threading.Thread(target=lambda: None)
        login._state.update({"state": "running", "message": "等待扫码登录…", "thread": stale})
        login._finish("failed", "上一轮的失败")
        self.assertEqual(login.state(self.data_dir)["state"], "running")


class LoginStateOnDiskTests(LoginTestCase):
    def test_saved_flag_comes_from_the_file_not_from_memory(self):
        """重启后内存里什么都没有，但凭据还在 —— 标记必须照样为真。"""
        self.assertFalse(login.state(self.data_dir)["saved"])
        write_secret(self.data_dir, GLOBAL_COOKIE_TASK_ID, "douyin_cookie", "sessionid=abc")
        snapshot = login.state(self.data_dir)
        self.assertTrue(snapshot["saved"])
        self.assertTrue(snapshot["saved_at"])
        secrets_file(self.data_dir, GLOBAL_COOKIE_TASK_ID).unlink()
        self.assertEqual(login.state(self.data_dir), {
            "state": "idle", "message": "", "saved": False, "saved_at": ""})


class CookieHeaderTests(unittest.TestCase):
    def test_header_is_sorted_and_drops_unsafe_entries(self):
        header = login._cookie_header([
            {"name": "ttwid", "value": "b"},
            {"name": "sessionid", "value": "a"},
            {"name": "broken", "value": "x;y"},
            {"name": "empty", "value": ""},
            {"name": "换行", "value": "n"},
            {"name": "注入", "value": "v\r\nX-Evil: 1"},
        ])
        self.assertEqual(header, "sessionid=a; ttwid=b")

    def test_first_occurrence_wins(self):
        self.assertEqual(login._cookie_header([
            {"name": "ttwid", "value": "first"}, {"name": "ttwid", "value": "second"},
        ]), "ttwid=first")


if __name__ == "__main__":
    unittest.main()
