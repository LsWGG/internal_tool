import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.crawler_adapters import ADAPTERS
from app.crawler_adapters.base import SourceAdapter
from app.crawler_manager import CrawlerTaskManager
from app.crawler_secrets import GLOBAL_COOKIE_TASK_ID, load_secrets, secrets_file, write_secret

COOKIE = "sessionid=abc123; ttwid=xyz789"


class SecretsFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def test_written_file_is_owner_only(self):
        """存的是登录凭据，不能被同机其他用户读到。"""
        write_secret(self.data_dir, "task-1", "douyin_cookie", COOKIE)
        mode = stat.S_IMODE(os.stat(secrets_file(self.data_dir, "task-1")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_round_trips_and_keeps_other_keys(self):
        write_secret(self.data_dir, "task-1", "douyin_cookie", COOKIE)
        self.assertEqual(load_secrets(self.data_dir, "task-1"), {"douyin_cookie": COOKIE})

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(load_secrets(self.data_dir, "nope"), {})

    def test_corrupt_file_reads_as_empty_instead_of_raising(self):
        path = secrets_file(self.data_dir, "task-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 JSON", "utf-8")
        self.assertEqual(load_secrets(self.data_dir, "task-1"), {})

    def test_unknown_keys_are_ignored(self):
        path = secrets_file(self.data_dir, "task-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"douyin_cookie": COOKIE, "别的": "value"}), "utf-8")
        self.assertEqual(load_secrets(self.data_dir, "task-1"), {"douyin_cookie": COOKIE})

    def test_unknown_key_cannot_be_written(self):
        with self.assertRaises(ValueError):
            write_secret(self.data_dir, "task-1", "别的", "value")

    def test_blank_value_does_not_overwrite(self):
        write_secret(self.data_dir, "task-1", "douyin_cookie", COOKIE)
        write_secret(self.data_dir, "task-1", "douyin_cookie", "   ")
        self.assertEqual(load_secrets(self.data_dir, "task-1")["douyin_cookie"], COOKIE)


class SecretsRestartTests(unittest.TestCase):
    """重启后仍能拿到 cookie —— 这是这次改动的全部意义。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)

    def manager_with_task(self, request):
        manager = object.__new__(CrawlerTaskManager)
        manager.data_dir = self.data_dir
        manager.lock = threading.RLock()
        manager.active_runs = {}
        manager.controls = {}
        manager.task_secrets = {}
        manager._save = MagicMock()
        manager.tasks = {"task-1": {"id": "task-1", "request": request, "runs": [], "result": None}}
        return manager

    def test_secrets_survive_a_restart(self):
        first = self.manager_with_task({"source": "douyin", "keyword": "某账号"})
        first.task_secrets["task-1"] = {"douyin_cookie": COOKIE}
        write_secret(self.data_dir, "task-1", "douyin_cookie", COOKIE)

        # 模拟重启：全新实例，内存里什么都没有。
        restarted = self.manager_with_task({"source": "douyin", "keyword": "某账号"})
        self.assertEqual(restarted._secrets_for("task-1"), {"douyin_cookie": COOKIE})
        # 回填之后不再重复读盘。
        self.assertEqual(restarted.task_secrets["task-1"], {"douyin_cookie": COOKIE})

    def test_memory_wins_over_disk(self):
        manager = self.manager_with_task({"source": "douyin"})
        write_secret(self.data_dir, "task-1", "douyin_cookie", "旧值")
        manager.task_secrets["task-1"] = {"douyin_cookie": "新值"}
        self.assertEqual(manager._secrets_for("task-1"), {"douyin_cookie": "新值"})

    def test_task_without_secrets_gets_empty_dict(self):
        manager = self.manager_with_task({"source": "news"})
        self.assertEqual(manager._secrets_for("task-1"), {})

    def test_run_merges_persisted_cookie_into_the_request(self):
        """_run 拿到的 request 里要有 cookie —— 否则富化会静默退化成匿名请求。"""
        write_secret(self.data_dir, "task-1", "douyin_cookie", COOKIE)
        manager = self.manager_with_task({"source": "douyin", "keyword": "某账号"})
        manager._checkpoint = MagicMock()
        manager._update = MagicMock()
        manager._start_run = MagicMock(return_value="run-1")
        captured = {}

        def fake_discover(value, ctx):
            captured.update(ctx.request)
            return []

        # 抖音的发现已经走适配器协议，注册表持有的是当初的函数对象引用，
        # patch 模块属性不会生效，必须换掉注册项本身。
        with patch.dict(ADAPTERS, {"douyin": SourceAdapter("tiktok", "抖音", discover=fake_discover)}):
            manager._run("task-1")
        self.assertEqual(captured.get("douyin_cookie"), COOKIE)


class SecretsDoNotLeakTests(unittest.TestCase):
    """cookie 落盘了，但不能因此出现在任何对外可见的地方。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.manager = CrawlerTaskManager(self.data_dir)

    def create_douyin_task(self):
        # 用 cron 而不是立即执行：create 对定时任务只登记不提交线程池，
        # 这条测试就不会真去联网。
        return self.manager.create({
            "source": "douyin", "tiktok_mode": "keyword", "keyword": "某关键词",
            "douyin_cookie": COOKIE, "cron": "0 3 * * *",
        })

    def test_cookie_is_persisted_but_absent_from_records_and_responses(self):
        task = self.create_douyin_task()
        task_id = task["id"]

        # 落盘了，且是 owner-only。
        path = secrets_file(self.data_dir, task_id)
        self.assertEqual(load_secrets(self.data_dir, task_id), {"douyin_cookie": COOKIE})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

        # 但任务记录、tasks.json、API 响应里都不能有。
        self.assertNotIn(COOKIE, json.dumps(task, ensure_ascii=False))
        self.assertNotIn(COOKIE, json.dumps(self.manager.list(), ensure_ascii=False))
        self.assertNotIn(COOKIE, json.dumps(self.manager.get(task_id), ensure_ascii=False))
        self.assertNotIn(COOKIE, (self.data_dir / "tasks.json").read_text("utf-8"))

    def test_deleting_the_task_removes_the_credentials(self):
        task = self.create_douyin_task()
        self.assertTrue(self.manager.delete(task["id"]))
        self.assertFalse(secrets_file(self.data_dir, task["id"]).exists())


class GlobalCookieFallbackTests(unittest.TestCase):
    """扫码登录存下的那份 cookie：表单没填时它顶上，表单填了就听表单的。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.manager = CrawlerTaskManager(self.data_dir)
        write_secret(self.data_dir, GLOBAL_COOKIE_TASK_ID, "douyin_cookie", COOKIE)

    def create(self, **overrides):
        # 用 cron 而不是立即执行：create 对定时任务只登记不提交线程池，不会真联网。
        payload = {"source": "douyin", "tiktok_mode": "keyword", "keyword": "人民日报",
                   "fields": [{"name": "url"}], "cron": "0 3 * * *"}
        payload.update(overrides)
        return self.manager.create(payload)

    def test_blank_form_falls_back_to_the_saved_cookie(self):
        task = self.create()
        self.assertEqual(self.manager.task_secrets[task["id"]], {"douyin_cookie": COOKIE})
        # 也复制进任务目录：这个任务重启后得自己能用。
        self.assertEqual(load_secrets(self.data_dir, task["id"]), {"douyin_cookie": COOKIE})
        self.assertEqual(stat.S_IMODE(os.stat(secrets_file(self.data_dir, task["id"])).st_mode), 0o600)

    def test_auto_filled_cookie_still_never_shows_up_outside(self):
        task = self.create()
        self.assertNotIn(COOKIE, json.dumps(task, ensure_ascii=False))
        self.assertNotIn(COOKIE, json.dumps(self.manager.list(), ensure_ascii=False))
        self.assertNotIn(COOKIE, (self.data_dir / "tasks.json").read_text("utf-8"))

    def test_form_cookie_wins_over_the_saved_one(self):
        task = self.create(douyin_cookie="sessionid=表单里填的")
        self.assertEqual(self.manager.task_secrets[task["id"]], {"douyin_cookie": "sessionid=表单里填的"})

    def test_other_sources_never_borrow_it(self):
        task = self.manager.create({"source": "generic", "urls": ["https://example.com/a"],
                                    "fields": [{"name": "url"}], "cron": "0 3 * * *"})
        self.assertNotIn(task["id"], self.manager.task_secrets)

    def test_data_preview_uses_the_saved_cookie(self):
        captured = {}

        def fake_discover(value, ctx):
            captured.update(ctx.request)
            url = "https://www.douyin.com/user/demo"
            return [{"url": url, "prefill": {"url": url}}]

        # 适配器注册表持有的是当初的函数对象引用，patch 模块属性不生效，得换掉注册项本身。
        with patch.dict(ADAPTERS, {"douyin": SourceAdapter("tiktok", "抖音", discover=fake_discover)}):
            preview = self.manager.data_preview({
                "source": "douyin", "tiktok_mode": "user", "keyword": "人民日报",
                "fields": [{"name": "url"}]})
        self.assertEqual(captured.get("douyin_cookie"), COOKIE)
        self.assertEqual(preview["rows"], [{"url": "https://www.douyin.com/user/demo"}])


if __name__ == "__main__":
    unittest.main()
