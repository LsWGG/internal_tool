"""GitHub 热门日报的大模型翻译与历史保留。

翻译整批走一次请求，失败不阻断日报：译不出来的行保留英文原文，由 status 说明原因。
这里只验证「失败怎么说」和「说什么」——真正调模型的部分一律打桩。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.github_trending_manager import GitHubTrendingManager


def reply(payload):
    """把模型返回包装成 requests 的响应对象。"""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
    return response


class TranslationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = GitHubTrendingManager(Path(temporary.name))
        self.repositories = [
            {"name": "a/one", "description": "A unified library of SOTA model optimization techniques."},
            {"name": "b/two", "description": "The open-source app everyone uses to manage agents at work."},
        ]
        settings = patch("app.github_trending_manager.resolve_model",
                         return_value=("https://api.example.com/v1/chat/completions", "sk-test", "demo-model"))
        settings.start()
        self.addCleanup(settings.stop)

    def run_translation(self, post):
        # 退避的 sleep 一并打桩，否则重试路径会真睡 1+3 秒。
        with patch("app.github_trending_manager.requests.post", post), \
                patch("app.github_trending_manager.time.sleep"):
            return self.manager._translate_descriptions(self.repositories)

    def test_every_description_is_translated_in_one_request(self):
        post = MagicMock(return_value=reply({"translations": [
            {"id": 0, "zh": "统一的最先进模型优化技术库。"},
            {"id": 1, "zh": "人人都在工作中用来管理智能体的开源应用。"}]}))
        result = self.run_translation(post)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["translated_count"], 2)
        self.assertEqual(self.repositories[0]["description_zh"], "统一的最先进模型优化技术库。")
        self.assertNotIn("message", result)

    def test_code_fenced_reply_is_still_parsed(self):
        content = '```json\n{"translations": [{"id": 0, "zh": "一"}, {"id": 1, "zh": "二"}]}\n```'
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        result = self.run_translation(MagicMock(return_value=response))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["translated_count"], 2)

    def test_partial_reply_keeps_the_english_for_the_rest(self):
        post = MagicMock(return_value=reply({"translations": [{"id": 0, "zh": "只有第一条翻出来了。"}]}))
        result = self.run_translation(post)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["translated_count"], 1)
        self.assertEqual(self.repositories[0]["description_zh"], "只有第一条翻出来了。")
        self.assertEqual(self.repositories[1]["description_zh"], self.repositories[1]["description"])
        self.assertIn("1/2", result["message"])

    def test_unusable_reply_keeps_english_and_says_so(self):
        result = self.run_translation(MagicMock(return_value=reply({"translations": []})))
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["translated_count"], 0)
        self.assertEqual(self.repositories[0]["description_zh"], self.repositories[0]["description"])
        self.assertIn("没有返回可用的翻译内容", result["message"])

    def test_reply_that_is_not_json_at_all_keeps_english(self):
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": "抱歉，我无法完成。"}}]}
        result = self.run_translation(MagicMock(return_value=response))
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(self.repositories[0]["description_zh"], self.repositories[0]["description"])

    def test_rows_without_a_description_do_not_hit_the_model(self):
        self.repositories = [{"name": "a/one", "description": "暂无简介"}]
        post = MagicMock()
        result = self.run_translation(post)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["translated_count"], 0)
        post.assert_not_called()

    def test_missing_model_config_tells_the_user_where_to_fix_it(self):
        with patch("app.github_trending_manager.resolve_model", return_value=("", "", "")):
            result = self.manager._translate_descriptions(self.repositories)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["translated_count"], 0)
        self.assertEqual(self.repositories[0]["description_zh"], self.repositories[0]["description"])
        self.assertIn("模型设置", result["message"])


class RequestFailureTests(unittest.TestCase):
    """外网抽风是常态：要说清失败在哪一步，重试要带退避，异常原文要落日志。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = GitHubTrendingManager(Path(temporary.name))
        self.repositories = [{"name": "a/one", "description": "An English description."}]
        settings = patch("app.github_trending_manager.resolve_model",
                         return_value=("https://api.example.com/v1/chat/completions", "sk-test", "demo-model"))
        settings.start()
        self.addCleanup(settings.stop)

    def run_translation(self, post):
        with patch("app.github_trending_manager.requests.post", post), \
                patch("app.github_trending_manager.time.sleep") as sleep:
            return self.manager._translate_descriptions(self.repositories), sleep

    def test_timeout_is_retried_with_backoff_and_explained(self):
        post = MagicMock(side_effect=requests.Timeout("HTTPSConnectionPool: Read timed out"))
        result, sleep = self.run_translation(post)
        self.assertEqual(post.call_count, GitHubTrendingManager.TRANSLATION_ATTEMPTS)
        # 退避：第一次重试前 1 秒，第二次前 3 秒。
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])
        self.assertEqual(result["status"], "fallback")
        self.assertIn("超时", result["message"])
        self.assertIn("重试 3 次", result["message"])

    def test_timeout_keeps_the_english_text(self):
        post = MagicMock(side_effect=requests.Timeout("Read timed out"))
        result, _ = self.run_translation(post)
        self.assertEqual(result["translated_count"], 0)
        self.assertEqual(self.repositories[0]["description_zh"], "An English description.")

    def test_http_error_reports_the_status_and_points_at_the_settings(self):
        response = MagicMock(status_code=401)
        error = requests.HTTPError("401 Client Error", response=response)
        result, _ = self.run_translation(MagicMock(side_effect=error))
        self.assertIn("HTTP 401", result["message"])
        self.assertIn("模型设置", result["message"])

    def test_connection_error_reports_the_underlying_reason(self):
        error = requests.ConnectionError("Connection refused: api.example.com")
        result, _ = self.run_translation(MagicMock(side_effect=error))
        self.assertIn("连接失败", result["message"])
        self.assertIn("Connection refused", result["message"])

    def test_the_original_exception_is_logged_for_next_time(self):
        # 页面上只有一句「请求失败或超时」时，日志是唯一的线索来源。
        error = requests.ConnectionError("Connection refused: api.example.com")
        with patch("app.github_trending_manager.requests.post", MagicMock(side_effect=error)), \
                patch("app.github_trending_manager.time.sleep"), \
                self.assertLogs("app.github_trending_manager", level="WARNING") as logs:
            self.manager._translate_descriptions(self.repositories)
        self.assertTrue(any("Connection refused" in line for line in logs.output))
        self.assertEqual(len(logs.output), GitHubTrendingManager.TRANSLATION_ATTEMPTS)

    def test_a_transient_failure_still_succeeds_on_a_later_attempt(self):
        good = reply({"translations": [{"id": 0, "zh": "翻出来了。"}]})
        post = MagicMock(side_effect=[requests.Timeout("Read timed out"), good])
        result, _ = self.run_translation(post)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.repositories[0]["description_zh"], "翻出来了。")


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.manager = GitHubTrendingManager(self.directory)

    def write_report(self, day):
        (self.directory / f"{day}.json").write_text(json.dumps({
            "date": day, "generated_at": f"{day}T00:01:00+08:00", "title": "t",
            "repository_count": 0, "translation": {"status": "completed", "translated_count": 0},
            "repositories": []}), "utf-8")

    def test_history_keeps_fifteen_days_and_drops_the_sixteenth(self):
        self.assertEqual(GitHubTrendingManager.RETENTION_DAYS, 15)
        today = datetime(2026, 10, 12).date()
        days = [(today - timedelta(days=offset)).isoformat() for offset in range(17)]
        for day in days:
            self.write_report(day)
        self.manager._cleanup()
        left = sorted(path.stem for path in self.directory.glob("????-??-??.json"))
        self.assertEqual(len(left), 15)
        self.assertEqual(left, sorted(days[:15]))

    def test_listing_reports_returns_at_most_the_retention_window(self):
        today = datetime(2026, 10, 12).date()
        for offset in range(17):
            self.write_report((today - timedelta(days=offset)).isoformat())
        self.assertEqual(len(self.manager.dates()), 15)


if __name__ == "__main__":
    unittest.main()
