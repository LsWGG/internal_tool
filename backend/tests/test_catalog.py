"""工具分类表（设置弹窗第二个 TAB）：服务层与路由。

这层没有业务逻辑，值得测的是**这份数据的语义**：它是「用户改过的东西」而不是全量快照
（所以稀疏必须保住、`description: ""` 必须是有意义的覆盖），以及文件坏掉时首页不能跟着塌。
"""

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.catalog_api import make_router
from app.catalog_manager import (NAME_MAX, CatalogError, CatalogManager, load_catalog,
                                 save_catalog, validate)


def catalog(order=None, categories=None, tools=None) -> dict:
    return {"catalog": {"order": order or [], "categories": categories or {}, "tools": tools or {}}}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file = Path(self._tmp.name) / "catalog.json"
        self.manager = CatalogManager(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def on_disk(self) -> dict:
        return json.loads(self.file.read_text("utf-8"))

    def test_nothing_saved_yet_is_an_empty_catalog(self):
        self.assertEqual(self.manager.get(), {"catalog": {"order": [], "categories": {},
                                                          "tools": {}}})

    def test_only_the_field_that_changed_is_written(self):
        """稀疏是这份数据的语义：改名不该把说明和图标一起冻进文件。

        冻进去的后果不是文件大一点 —— 是代码以后改描述、换图标，动过设置的用户永远看不到。
        """
        self.manager.save(catalog(order=["geo", "images"], categories={"geo": {"name": "地理"}}))
        self.assertEqual(self.on_disk()["categories"], {"geo": {"name": "地理"}})

    def test_clearing_a_description_is_a_real_override(self):
        """`{"description": ""}` = 用户把说明清空了，不是「没写」。"""
        self.manager.save(catalog(order=["images"], categories={"images": {"description": ""}}))
        self.assertEqual(self.on_disk()["categories"], {"images": {"description": ""}})

    def test_saving_what_was_read_changes_nothing(self):
        payload = catalog(order=["geo", "documents", "3f9a2b1c4d5e"],
                          categories={"geo": {"name": "地理", "icon": "map"},
                                      "3f9a2b1c4d5e": {"name": "我的分类", "description": "常用",
                                                       "icon": "gear"}},
                          tools={"docker": "3f9a2b1c4d5e"})
        self.manager.save(payload)
        once = self.on_disk()
        self.manager.save(self.manager.get())
        self.assertEqual(self.on_disk(), once)

    def test_a_broken_file_is_an_empty_catalog(self):
        self.file.write_text("{ 不是 json", "utf-8")
        self.assertEqual(load_catalog(self.file), {"order": [], "categories": {}, "tools": {}})

    def test_an_unknown_version_is_an_empty_catalog(self):
        self.file.write_text(json.dumps({"version": 99, "order": ["geo"]}), "utf-8")
        self.assertEqual(load_catalog(self.file)["order"], [])

    def test_one_bad_entry_does_not_take_the_rest_with_it(self):
        """手改坏了文件时：能救的救回来，坏的那条跳过。前端合并会把缺的分类按代码顺序补回来。"""
        self.file.write_text(json.dumps({
            "version": 1,
            "order": ["geo", "", "geo", "images"],
            "categories": {"geo": {"name": "地理"}, "images": "不是对象"},
            "tools": {"docker": "images", "map": 3},
        }), "utf-8")
        saved = load_catalog(self.file)
        self.assertEqual(saved["order"], ["geo", "images"])
        self.assertEqual(saved["categories"], {"geo": {"name": "地理"}})
        self.assertEqual(saved["tools"], {"docker": "images"})


class ValidationTests(unittest.TestCase):
    """整表 POST 的边界：每条对应一个 400 的人话文案。"""

    def assert_rejected(self, payload, fragment):
        with self.assertRaises(CatalogError) as caught:
            validate(payload)
        self.assertIn(fragment, str(caught.exception))

    def test_duplicate_category_ids(self):
        self.assert_rejected(catalog(order=["geo", "geo"]), "分类 id 重复：geo")

    def test_blank_category_id(self):
        self.assert_rejected(catalog(order=["geo", "   "]), "分类 id 不能为空")

    def test_a_category_name_may_not_be_blank(self):
        self.assert_rejected(catalog(order=["geo"], categories={"geo": {"name": "   "}}),
                             "分类 geo 的名称不能为空")

    def test_an_icon_may_not_be_blank(self):
        self.assert_rejected(catalog(order=["geo"], categories={"geo": {"icon": ""}}),
                             "分类 geo 的图标不能为空")

    def test_a_name_that_is_too_long(self):
        self.assert_rejected(catalog(order=["geo"], categories={"geo": {"name": "地" * (NAME_MAX + 1)}}),
                             f"不能超过 {NAME_MAX} 个字")

    def test_the_payload_must_carry_a_catalog(self):
        self.assert_rejected({"orders": []}, "catalog 必须是对象")

    def test_order_must_be_a_list(self):
        self.assert_rejected({"catalog": {"order": "geo"}}, "catalog.order 必须是数组")

    def test_a_tool_must_point_at_a_string(self):
        self.assert_rejected(catalog(order=["geo"], tools={"map": 3}), "工具 map 的分类必须是字符串")

    def test_a_cleared_description_is_accepted(self):
        """反着的用例，和上面那条「清空说明是真覆盖」是一对。"""
        saved = validate(catalog(order=["images"], categories={"images": {"description": ""}}))
        self.assertEqual(saved["categories"], {"images": {"description": ""}})

    def test_unknown_keys_are_dropped_rather_than_rejected(self):
        """与 links 一样的宽松：多余的键丢掉，不 400。前端的乐观回显里就带着自己那份状态。"""
        saved = validate(catalog(order=["geo"], categories={"geo": {"name": "地理", "colour": "red"}}))
        self.assertEqual(saved["categories"], {"geo": {"name": "地理"}})


class ApiTests(unittest.TestCase):
    """路由层：只测 HTTP 状态码与形状。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        app = FastAPI()
        app.include_router(make_router(self.root))
        self.app = app

    def tearDown(self):
        self._tmp.cleanup()

    def client(self):
        return TestClient(self.app)

    def test_the_empty_table_is_an_empty_catalog(self):
        with self.client() as client:
            response = client.get("/api/catalog")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"catalog": {"order": [], "categories": {},
                                                           "tools": {}}})

    def test_save_then_read_over_http(self):
        payload = catalog(order=["geo", "images"], categories={"geo": {"name": "地理"}},
                          tools={"docker": "images"})
        with self.client() as client:
            saved = client.post("/api/catalog", json=payload)
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["catalog"]["tools"], {"docker": "images"})
            self.assertEqual(client.get("/api/catalog").json(), saved.json())

    def test_a_bad_payload_answers_400_with_the_reason(self):
        with self.client() as client:
            response = client.post("/api/catalog", json=catalog(order=["geo", "geo"]))
            self.assertEqual(response.status_code, 400)
            self.assertIn("分类 id 重复", response.json()["detail"])

    def test_the_path_without_a_trailing_slash_is_not_a_redirect(self):
        """写成 `/api/catalog/` 的话，前端那个不带斜杠的请求会先吃一个 307。"""
        with self.client() as client:
            self.assertNotIn(client.get("/api/catalog").status_code, (307, 308))

    def test_the_file_is_world_readable_and_leaves_no_temp_file(self):
        """这里没有密钥，不学端点池锁 0600；`.tmp` 是原子写的中间产物，不该留下。"""
        with self.client() as client:
            client.post("/api/catalog", json=catalog(order=["geo"]))
        saved = self.root / "catalog.json"
        self.assertNotEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class SavedShapeTests(unittest.TestCase):
    """落盘形态本身：`version` 与三个键，顺序稳定。"""

    def test_the_file_carries_a_version(self):
        with tempfile.TemporaryDirectory() as raw:
            file = Path(raw) / "catalog.json"
            save_catalog(file, {"order": ["geo"], "categories": {}, "tools": {}})
            self.assertEqual(json.loads(file.read_text("utf-8")),
                             {"version": 1, "order": ["geo"], "categories": {}, "tools": {}})


if __name__ == "__main__":
    unittest.main()
