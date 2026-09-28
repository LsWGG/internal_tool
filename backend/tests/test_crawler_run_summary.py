import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.crawler_manager import CrawlerTaskManager

BAD_URL = "https://bad.test/b"


class RunSummaryTests(unittest.TestCase):
    """一次运行结束后给用户看的那句话。

    非下载场景过去只报成功条数：50 条 URL 挂了 3 条，界面上仍是「采集完成」，
    用户会以为全都拿到了。这里锁住失败数会出现在消息里。
    """

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
        self.manager._extract = MagicMock(
            side_effect=lambda url, soup, fields: {"title": "标题", "url": url}
        )

        def fetch_html(url, request, browser=None):
            if url == BAD_URL:
                raise RuntimeError("连接被拒绝")
            return "<html><title>标题</title></html>"

        self.manager._fetch_html = fetch_html

    def run_generic(self, urls, **extra):
        request = {"source": "generic", "urls": urls,
                   "fields": [{"name": "title"}, {"name": "url"}],
                   "output_format": "json", "delay_seconds": 0, **extra}
        self.manager.tasks = {"task": {"id": "task", "request": request, "runs": [], "result": None}}
        self.manager._run("task")
        return self.manager.tasks["task"]

    def test_failed_urls_are_reported_in_the_completion_message(self):
        task = self.run_generic(["https://ok.test/a", "https://ok.test/b", BAD_URL])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["message"], "采集完成 · 成功 2 条，失败 1 条")
        self.assertEqual(task["result"]["failed_count"], 1)
        self.assertEqual([item["url"] for item in task["result"]["errors"]], [BAD_URL])

    def test_clean_run_keeps_the_plain_message(self):
        task = self.run_generic(["https://ok.test/a"])
        self.assertEqual(task["message"], "采集完成")
        self.assertEqual(task["result"]["failed_count"], 0)

    def test_run_where_everything_fails_still_reports_the_failure(self):
        """一条都没成功时 _run 会抛错走 failed 分支，错误详情不能丢。"""
        task = self.run_generic([BAD_URL])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "连接被拒绝")


if __name__ == "__main__":
    unittest.main()
