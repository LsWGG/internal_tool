import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.crawler_manager import CrawlerTaskManager


class CumulativeExportPlaceholderTests(unittest.TestCase):
    """评论受限时导出占位行的那条路径。

    分支里原本引用了方法内不存在的 source/request/targets/names，一旦累计库没有
    数据就必崩 NameError —— 这组测试锁住它现在真的能产出占位行。
    """

    def manager_for(self, request):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = object.__new__(CrawlerTaskManager)
        manager.data_dir = Path(temporary.name)
        manager.lock = threading.RLock()
        manager.active_runs = {}
        manager._save = MagicMock()
        manager.tasks = {"test": {
            "id": "test", "name": "评论采集", "status": "completed",
            "request": request, "runs": [], "result": None,
        }}
        return manager

    def empty_cumulative_database(self, manager):
        """造出「累计库存在但一条记录都没有」的状态。

        这是那条占位分支的真实前提：库文件在（早先的版本迁移把 records 清过一次，
        而当时归档的 run 文件已经不在了），于是一次运行都没有数据可导入。
        """
        manager._append_cumulative("test", "old-run", [])

    def test_comments_task_without_rows_exports_video_placeholder(self):
        manager = self.manager_for({
            "source": "douyin", "tiktok_mode": "comments",
            "keyword": "https://www.douyin.com/video/7300000000000000000?is_from_webapp=1",
            "urls": [], "fields": ["url", "text", "author"], "output_format": "json",
        })
        self.empty_cumulative_database(manager)
        output, count = manager.export_cumulative("test")
        self.assertEqual(count, 1)
        rows = json.loads(Path(output).read_text("utf-8"))
        # 查询串要去掉（与 _tiktok_video_url 一致），其余字段留空。
        self.assertEqual(rows[0]["url"], "https://www.douyin.com/video/7300000000000000000")
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(rows[0]["author"], "")

    def test_placeholder_always_carries_url_even_when_not_a_selected_field(self):
        manager = self.manager_for({
            "source": "tiktok", "tiktok_mode": "comments",
            "keyword": "https://www.tiktok.com/@someone/video/7300000000000000000",
            "urls": [], "fields": ["text"], "output_format": "json",
        })
        self.empty_cumulative_database(manager)
        output, count = manager.export_cumulative("test")
        rows = json.loads(Path(output).read_text("utf-8"))
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["url"], "https://www.tiktok.com/@someone/video/7300000000000000000")
        self.assertEqual(rows[0]["text"], "")

    def test_non_comments_task_still_reports_no_data(self):
        """占位行只服务评论模式；别的源没有数据就该照旧报错，不能凭空造行。"""
        manager = self.manager_for({
            "source": "news", "urls": ["https://example.com/a"],
            "fields": ["title"], "output_format": "json",
        })
        self.empty_cumulative_database(manager)
        with self.assertRaisesRegex(ValueError, "没有可导出的累计数据"):
            manager.export_cumulative("test")

    def test_comments_task_without_any_target_still_reports_no_data(self):
        manager = self.manager_for({
            "source": "douyin", "tiktok_mode": "comments",
            "keyword": "随便写的搜索词", "urls": [],
            "fields": ["url"], "output_format": "json",
        })
        self.empty_cumulative_database(manager)
        # 关键词解不出视频链接时不能造出空行冒充数据。
        with self.assertRaisesRegex(ValueError, "没有可导出的累计数据"):
            manager.export_cumulative("test")

    def test_unknown_task_raises(self):
        manager = self.manager_for({"source": "news"})
        with self.assertRaisesRegex(ValueError, "采集任务不存在"):
            manager.export_cumulative("missing")


if __name__ == "__main__":
    unittest.main()
