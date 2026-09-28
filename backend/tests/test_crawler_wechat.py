import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.crawler_adapters import wechat
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

    def test_account_filter_reports_the_publishers_it_actually_saw(self):
        """公众号名少一个字时，索引返回的全是别人的文章；报错必须说出实际发布方。"""
        manager = object.__new__(CrawlerTaskManager)
        manager._checkpoint = MagicMock()
        manager._update = MagicMock()
        response = requests.Response()
        response.status_code = 200
        response.url = "https://weixin.sogou.com/weixin?type=2&query=test&page=1"
        response._content = '''
          <ul class="news-list">
            <li><div class="txt-box"><h3><a href="/link?url=one">标题一</a></h3>
                <p class="account">果妈碎碎念</p></div></li>
            <li><div class="txt-box"><h3><a href="/link?url=two">标题二</a></h3>
                <p class="account">扬州妇幼</p></div></li>
            <li><div class="txt-box"><h3><a href="/link?url=three">标题三</a></h3>
                <p class="account">果妈碎碎念</p></div></li>
          </ul>'''.encode()
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        manager._request = MagicMock(return_value=response)
        with self.assertRaisesRegex(RuntimeError, "果妈碎碎念、扬州妇幼") as caught:
            manager._discover_wechat_sogou_urls(
                "给果果讲故事", {"max_items": 10, "proxies": [], "delay_seconds": 0}, "test"
            )
        # 第 1 页 0 条命中会继续翻第 2 页，两次都是同一份替身页面。
        self.assertIn("6 条提及该名称的文章", str(caught.exception))

    def test_primary_verification_automatically_switches_to_mobile_index(self):
        manager = object.__new__(CrawlerTaskManager)
        manager.data_dir = Path(self.temp_directory())
        manager._checkpoint = MagicMock()
        manager._update = MagicMock()
        expected = [{"url": "https://mp.weixin.qq.com/s/article"}]
        # 三级索引的降级在适配器内部完成，patch 目标随之落到适配器模块。
        with patch.object(wechat, "sogou_urls", MagicMock(side_effect=RuntimeError("需要人工验证"))), \
                patch.object(wechat, "mobile_urls", MagicMock(return_value=expected)):
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
        with patch.object(wechat, "resolve_sogou_url",
                          MagicMock(return_value="https://mp.weixin.qq.com/s/right")):
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
        manager._checkpoint = MagicMock()
        manager._update = MagicMock()
        expected = [{"url": "https://mp.weixin.qq.com/s/article"}]
        sogou_urls = MagicMock(return_value=expected)
        with patch.object(wechat, "sogou_urls", sogou_urls), \
                patch.object(wechat, "mobile_urls", MagicMock()), \
                patch.object(wechat, "public_search_urls", MagicMock()):
            request = {"max_items": -1}
            self.assertEqual(manager._discover_wechat_urls("测试公众号", request, "test"), expected)
            self.assertEqual(manager._discover_wechat_urls("测试公众号", request, "test"), expected)
        self.assertEqual(sogou_urls.call_count, 1)


if __name__ == "__main__":
    unittest.main()
