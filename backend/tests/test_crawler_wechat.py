import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import requests

from app.crawler_manager import CrawlerTaskManager


class WechatScheduledCollectionTests(unittest.TestCase):
    def manager_for(self, request):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = object.__new__(CrawlerTaskManager)
        manager.data_dir = Path(temporary.name)
        manager.lock = threading.RLock()
        manager.controls = {"test": {"paused": False, "cancelled": False}}
        manager.active_runs = {}
        manager.tasks = {"test": {
            "id": "test", "name": "公众号采集", "status": "queued",
            "request": request, "runs": [], "result": None,
        }}
        manager._save = MagicMock()
        manager._discover_wechat_urls = MagicMock(
            side_effect=RuntimeError("公开索引暂时没有返回结果")
        )
        return manager

    def test_incremental_schedule_records_failure_instead_of_hiding_it(self):
        manager = self.manager_for({
            "source": "wechat", "account_name": "测试公众号", "urls": [],
            "fields": ["title"], "max_items": -1, "cron": "*/5 * * * *",
            "interval_minutes": 0,
        })
        manager._run("test")
        task = manager.tasks["test"]
        self.assertEqual(task["status"], "failed")
        self.assertIn("公开索引暂时没有返回结果", task["error"])
        self.assertEqual(task["runs"][-1]["status"], "failed")

    def test_one_time_collection_still_reports_discovery_failure(self):
        manager = self.manager_for({
            "source": "wechat", "account_name": "测试公众号", "urls": [],
            "fields": ["title"], "max_items": 50, "cron": "",
            "interval_minutes": 0,
        })
        manager._run("test")
        self.assertEqual(manager.tasks["test"]["status"], "failed")
        self.assertIn("公开索引暂时没有返回结果", manager.tasks["test"]["error"])

    def test_signed_wechat_urls_share_a_stable_discovery_key(self):
        common = {
            "title": "同一篇文章", "account": "测试公众号",
            "published_at": "2026-09-08 10:00:00",
        }
        first = {"detail_url": "https://mp.weixin.qq.com/s?timestamp=1&signature=aaa", "prefill": common}
        second = {"detail_url": "https://mp.weixin.qq.com/s?timestamp=2&signature=bbb", "prefill": common}
        self.assertEqual(
            CrawlerTaskManager._discovery_key("wechat", first),
            CrawlerTaskManager._discovery_key("wechat", second),
        )

    def test_new_sogou_antispider_page_is_detected_without_retries(self):
        manager = object.__new__(CrawlerTaskManager)
        manager._checkpoint = MagicMock()
        manager._update = MagicMock()
        response = requests.Response()
        response.status_code = 200
        response.url = "https://weixin.sogou.com/antispider/?antip=wx_sh2"
        response._content = "此验证码用于确认这些请求是您的正常行为而不是自动程序发出的".encode()
        response.encoding = "utf-8"
        manager._request = MagicMock(return_value=response)
        with self.assertRaisesRegex(RuntimeError, "人工验证"):
            manager._discover_wechat_sogou_urls(
                "测试公众号", {"max_items": 10, "proxies": [], "delay_seconds": 0}, "test"
            )
        self.assertEqual(manager._request.call_count, 1)

    def test_primary_verification_automatically_switches_to_mobile_index(self):
        manager = object.__new__(CrawlerTaskManager)
        manager.data_dir = Path(self.temp_directory())
        manager._discover_wechat_sogou_urls = MagicMock(side_effect=RuntimeError("需要人工验证"))
        expected = [{"url": "https://mp.weixin.qq.com/s/article"}]
        manager._discover_wechat_mobile_urls = MagicMock(return_value=expected)
        self.assertEqual(
            manager._discover_wechat_urls("测试公众号", {"max_items": 10}, None), expected
        )

    def test_mobile_index_extracts_only_the_exact_account(self):
        manager = object.__new__(CrawlerTaskManager)
        manager._checkpoint = MagicMock()
        manager._update = MagicMock()
        response = requests.Response()
        response.status_code = 200
        response.url = "https://weixin.sogou.com/weixinwap?type=2&query=test&page=1"
        response._content = b'''<ul>
          <li><h4><a href="/link?url=wrong">wrong</a></h4>
              <p class="time"><span class="s2" data-sourcename="other">other</span></p></li>
          <li><h4><a href="/link?url=right">right title</a></h4>
              <p data-type="article_summary">summary</p><p class="time">
              <span class="s2" data-sourcename="target">target</span>
              <span class="s3" data-lastmodified="1788840000">date</span></p></li>
        </ul>'''
        response.encoding = "utf-8"
        manager._request = MagicMock(return_value=response)
        manager._resolve_sogou_wechat_url = MagicMock(
            return_value="https://mp.weixin.qq.com/s/right"
        )
        result = manager._discover_wechat_mobile_urls(
            "target", {"max_items": 1, "proxies": [], "delay_seconds": 0}, "test"
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["prefill"]["account"], "target")
        self.assertEqual(result[0]["prefill"]["title"], "right title")

    def temp_directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return temporary.name

    def test_incremental_discovery_reuses_recent_success_to_avoid_ip_verification(self):
        manager = object.__new__(CrawlerTaskManager)
        manager.data_dir = Path(self.temp_directory())
        manager._update = MagicMock()
        expected = [{"url": "https://mp.weixin.qq.com/s/article"}]
        manager._discover_wechat_sogou_urls = MagicMock(return_value=expected)
        manager._discover_wechat_mobile_urls = MagicMock()
        manager._discover_wechat_public_search_urls = MagicMock()
        request = {"max_items": -1}
        self.assertEqual(manager._discover_wechat_urls("测试公众号", request, "test"), expected)
        self.assertEqual(manager._discover_wechat_urls("测试公众号", request, "test"), expected)
        self.assertEqual(manager._discover_wechat_sogou_urls.call_count, 1)


if __name__ == "__main__":
    unittest.main()
