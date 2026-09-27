"""编辑器三件事的测试：静态检查、格式化（ruff）、调试试跑。

这三条都是**给用户看的**，所以断言全落在「面板上会显示成什么」上：行号对不对、`None`
是不是「不改动」、那一行报错有没有盖到隔壁行、`print` 有没有漏。真起子进程、真调 ruff
（缺 ruff 的用例 skip 并写清原因），因为「看起来对」和「跑起来对」在这里差得很远。

调试试跑有一条**作用域**不变量值得单独测：只把被测的那个函数放进配置再校验。旁边那张刚
添加、还没勾写回字段的卡片，不该让这个按钮报一句与被测函数无关的错。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clean_api import make_router
from app.clean_format import ruff_command
from app.clean_manager import CleanError, CleanManager

CSV = "a,b\n1,x\n2,y\n3,z\n"

IDENTITY = "def transform(row):\n    return {'b': row['a']}\n"
NO_CHANGE = "def transform(row):\n    return None\n"
UP = "def transform(row):\n    return {'b': row['a'].upper()}\n"
BAD_ROW = (
    "def transform(row):\n"
    "    if row['a'] == '2':\n"
    "        raise ValueError('这一行有问题')\n"
    "    return {'b': row['a'].upper()}\n"
)
TALKS = "def transform(row):\n    print('看到', row['a'])\n    return None\n"
SLEEPS = "def transform(row):\n    import time\n    time.sleep(30)\n    return None\n"
USES_C = "def transform(row):\n    return {'b': str(row['c'])}\n"
BANNED = "import os\n\n\ndef transform(row):\n    return {'b': os.getcwd()}\n"
COLUMNS = "def transform(columns):\n    return {'b': [v + '!' for v in columns['a']]}\n"
SHORT = "def transform(columns):\n    return {'b': ['只给一个']}\n"

_MISSING = ruff_command() is None
REASON = "环境里没有 ruff（uv pip install -r backend/requirements.txt 后重启）"


def fn(source, name="f", **over):
    data = {"name": name, "source": source, "input_fields": ["a"], "output_fields": ["b"]}
    data.update(over)
    return data


def config_for(source, functions, **over):
    config = {
        "name": "调试",
        "source": {"mode": "server_path", "paths": [str(source)]},
        "input": {"delimiter": ",", "encoding": "utf-8"},
        "fields": [
            {"dest": "a", "source": "a"},
            {"dest": "b", "source": "b"},
            # 文件里没有的新列：函数读它只能读到空值，这正是 `sample.missing` 要说的
            {"dest": "c"},
        ],
        "functions": list(functions),
        "output": {"format": "csv"},
    }
    config.update(over)
    return config


class FunctionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "in"
        self.source.mkdir()
        (self.source / "a.csv").write_text(CSV, encoding="utf-8")
        self.manager = CleanManager(self.tmp / "root", allowed_roots=[self.tmp])
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.manager.shutdown)

    def debug(self, functions, index=0, rows=None, **over):
        """跑一次调试。`rows` 是请求参数（不进配置），`**over` 才改配置 —— 两者混在一起
        会被 `extra="forbid"` 当场拒掉。"""
        payload = {"config": config_for(self.source, functions, **over), "index": index}
        if rows is not None:
            payload["rows"] = rows
        return self.manager.test_function(payload)


class DebugRunTests(FunctionCase):
    def test_each_row_shows_what_went_in_and_what_came_out(self):
        out = self.debug([fn(UP)])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["ran"])
        self.assertEqual([item["input"] for item in out["rows"]],
                         [{"a": "1"}, {"a": "2"}, {"a": "3"}])
        self.assertEqual([item["output"] for item in out["rows"]],
                         [{"b": "1"}, {"b": "2"}, {"b": "3"}])
        self.assertEqual(out["sample"]["missing"], [])
        self.assertEqual(out["sample"]["rows"], 3)
        self.assertTrue(out["sample"]["file"].endswith("a.csv"))

    def test_returning_none_means_the_row_is_untouched(self):
        out = self.debug([fn(NO_CHANGE)])
        self.assertEqual([item["output"] for item in out["rows"]], [None, None, None])
        self.assertEqual([item["error"] for item in out["rows"]], ["", "", ""])

    def test_one_bad_row_only_breaks_that_row(self):
        """一行抛异常不该让其余行的结果消失 —— 面板要能指出「就是这一行」."""
        out = self.debug([fn(BAD_ROW)])
        self.assertTrue(out["ok"], out)
        self.assertEqual([item["output"] for item in out["rows"]],
                         [{"b": "1"}, None, {"b": "3"}])
        self.assertIn("这一行有问题", out["rows"][1]["error"])
        self.assertEqual(out["rows"][0]["error"], "")

    def test_print_lands_in_printed(self):
        out = self.debug([fn(TALKS)])
        self.assertEqual(out["printed"], ["看到 1", "看到 2", "看到 3"])
        self.assertEqual(out["printed_omitted"], 0)

    def test_a_chatty_function_says_how_many_lines_were_left_out(self):
        """省略要**说出来**：静默截断会让人以为函数没打印。"""
        loud = "def transform(row):\n    for i in range(210):\n        print('行', i)\n    return None\n"
        out = self.debug([fn(loud)], rows=1)
        self.assertEqual(len(out["printed"]), 200)
        self.assertEqual(out["printed_omitted"], 10)

    def test_timeout_uses_the_function_s_own_budget(self):
        """调试里超时、正式任务里不超时的面板就是在说谎 —— 所以不偷偷调小超时。"""
        started = time.monotonic()
        out = self.debug([fn(SLEEPS, timeout_s=0.5)])
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertFalse(out["ok"])
        self.assertTrue(out["ran"])
        self.assertEqual(out["failure"], "timeout")
        self.assertIn("0.5", out["error"])

    def test_a_field_with_no_column_in_the_file_goes_in_empty_and_is_reported(self):
        out = self.debug([fn(USES_C, input_fields=["c"])])
        self.assertEqual(out["sample"]["missing"], ["c"])
        self.assertEqual(out["rows"][0]["input"], {"c": ""})
        self.assertEqual(out["rows"][0]["output"], {"b": ""})
        self.assertTrue(any("没有对应列" in note for note in out["notes"]), out["notes"])

    def test_the_phase_comes_back_and_post_warns_about_the_sample(self):
        pre = self.debug([fn(UP)])
        self.assertEqual(pre["phase"], "pre")
        self.assertFalse(any("清洗之后" in note for note in pre["notes"]))
        post = self.debug([fn(UP, phase="post")])
        self.assertEqual(post["phase"], "post")
        self.assertTrue(any("清洗之后" in note for note in post["notes"]), post["notes"])

    def test_column_mode_shows_the_columns(self):
        out = self.debug([fn(COLUMNS, mode="column")])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["columns"][0]["name"], "b")
        self.assertEqual(out["columns"][0]["input"], ["1", "2", "3"])
        self.assertEqual(out["columns"][0]["output"], ["1!", "2!", "3!"])
        self.assertEqual(out["rows"], [])

    def test_column_mode_returning_the_wrong_length_is_a_shape_error(self):
        out = self.debug([fn(SHORT, mode="column")])
        self.assertFalse(out["ok"])
        self.assertEqual(out["failure"], "shape_error")
        self.assertIn("等长", out["error"])

    def test_a_row_count_updates_the_sample(self):
        out = self.debug([fn(UP)], rows=2)
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(out["sample"]["rows"], 2)

    def test_the_reader_is_closed_even_when_the_run_explodes(self):
        """一次点击漏一个句柄，点几十次就是几百个 —— 而且泄漏的是输入文件。"""
        opened = []

        class Spy:
            delegate = None

            def __init__(self, *args, **kwargs):
                self.inner = Spy.delegate(*args, **kwargs)
                opened.append(self.inner)

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def close(self):
                self.inner.closed_by_us = True
                return self.inner.close()

        from app import clean_io

        original = clean_io.open_reader
        with mock.patch("app.clean_manager._head_reader",
                        side_effect=lambda *a, **k: Spy(*a, **k)):
            Spy.delegate = original
            self.debug([fn(BAD_ROW)])
            self.debug([fn(SLEEPS, timeout_s=0.5)])
        self.assertEqual(len(opened), 2)
        for reader in opened:
            self.assertTrue(getattr(reader, "closed_by_us", False), "reader 没有被关上")


class DebugScopeTests(FunctionCase):
    """作用域与前置条件：报错必须指着被测的那个函数。"""

    def test_a_half_written_neighbour_does_not_break_the_debug(self):
        neighbour = {"name": "半成品", "source": "def transform(row):\n    return {",
                     "input_fields": [], "output_fields": []}
        out = self.debug([neighbour, fn(UP)], index=1)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["rows"]), 3)

    def test_the_gate_stops_before_the_subprocess(self):
        with mock.patch("app.clean_manager.PythonWorker") as fake:
            out = self.debug([fn(BANNED)])
        self.assertEqual(fake.call_count, 0)
        self.assertFalse(out["ran"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["rows"], [])
        self.assertTrue(any(item["severity"] == "error" for item in out["problems"]))
        self.assertEqual(out["problems"][0]["line"], 1)
        self.assertIn("os", out["problems"][0]["text"])

    def test_an_out_of_range_index_is_a_config_error(self):
        with self.assertRaises(CleanError) as caught:
            self.debug([fn(UP)], index=3)
        self.assertIn("越界", str(caught.exception))

    def test_no_functions_at_all_is_a_config_error(self):
        with self.assertRaises(CleanError):
            self.debug([])

    def test_without_input_data_the_static_check_still_comes_back(self):
        """还没选数据来源时点这个按钮是正常的操作，不该只回一个红叉。"""
        (self.source / "a.csv").unlink()
        out = self.debug([fn(UP)])
        self.assertFalse(out["ran"])
        self.assertEqual(out["problems"], [])
        self.assertIn("没有找到可处理的文件", out["error"])
        self.assertTrue(any("静态检查" in note for note in out["notes"]))


class CheckTests(FunctionCase):
    def test_a_typo_comes_back_with_its_line(self):
        out = self.manager.check_function({"source": "def transform(row)\n    return None\n"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["problems"][0]["severity"], "error")
        self.assertEqual(out["problems"][0]["line"], 1)
        self.assertIn("语法错误", out["problems"][0]["text"])

    def test_a_missing_entry_is_a_warning_not_a_refusal(self):
        """入口没写对时子进程 init 会拒跑（那是 error）；这里提前提醒，且不阻断。"""
        out = self.manager.check_function({"source": "value = 1\n"})
        self.assertTrue(out["ok"], out)
        self.assertEqual([item["severity"] for item in out["problems"]], ["warning"])
        self.assertIn("transform", out["problems"][0]["text"])

    def test_checking_needs_no_config_at_all(self):
        """只收源码：配置还没填完就要能划线，否则用户会在一条无关的红字底下写代码。"""
        out = self.manager.check_function({"source": IDENTITY})
        self.assertEqual(out, {"ok": True, "problems": []})

    def test_every_problem_carries_a_line_for_the_editor(self):
        source = "import os\n\n\ndef transform(row):\n    return row.__class__\n"
        out = self.manager.check_function({"source": source})
        self.assertEqual([item["line"] for item in out["problems"]], [1, 5])


@unittest.skipIf(_MISSING, REASON)
class FormatTests(FunctionCase):
    def test_format_reports_what_changed(self):
        out = self.manager.format_function({"name": "f", "source": "x  =  1\n"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["source"], "x = 1\n")
        self.assertTrue(out["changed"])

    def test_format_of_neat_source_changes_nothing(self):
        """`changed=False` 是界面说「没有需要改的地方」的依据，所以它必须真的准。"""
        neat = 'def transform(row):\n    return {"b": row["a"]}\n'
        out = self.manager.format_function({"name": "f", "source": neat})
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["changed"])

    def test_a_syntax_error_is_a_result_not_an_exception(self):
        out = self.manager.format_function({"name": "f", "source": "def transform(:\n"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["source"], "")
        self.assertIn("Failed to parse", out["error"])


class FunctionApiTests(unittest.TestCase):
    """路由层：状态码与形状。业务语义由上面那些用例负责。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "in"
        self.source.mkdir()
        (self.source / "a.csv").write_text(CSV, encoding="utf-8")
        self._env = {name: os.environ.get(name) for name in
                     ("CLEAN_ALLOWED_ROOTS", "CLEAN_DATA_DIR")}
        os.environ["CLEAN_ALLOWED_ROOTS"] = str(self.tmp)
        app = FastAPI()
        app.include_router(make_router(self.tmp / "root"))
        self.app = app

    def tearDown(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def client(self):
        return TestClient(self.app)

    def test_check_route(self):
        with self.client() as client:
            response = client.post("/api/clean/functions/check", json={"source": BANNED})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["problems"][0]["line"], 1)

    def test_format_route(self):
        with self.client() as client:
            response = client.post("/api/clean/functions/format",
                                   json={"name": "f", "source": "x  =  1\n"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        if _MISSING:
            self.assertFalse(body["ok"])
            self.assertIn("未找到 ruff", body["error"])
        else:
            self.assertEqual(body["source"], "x = 1\n")

    def test_test_route(self):
        payload = {"config": config_for(self.source, [fn(UP)]), "index": 0, "rows": 2}
        with self.client() as client:
            response = client.post("/api/clean/functions/test", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(len(body["rows"]), 2)

    def test_test_route_out_of_range_is_400(self):
        payload = {"config": config_for(self.source, [fn(UP)]), "index": 9}
        with self.client() as client:
            response = client.post("/api/clean/functions/test", json=payload)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("越界", response.json()["detail"])

    def test_meta_carries_the_vocabulary_the_editor_may_offer(self):
        """补全词表必须来自运行时白名单：界面上能点的和后端能跑的得是同一份。"""
        with self.client() as client:
            body = client.get("/api/clean/meta").json()["python"]
        self.assertEqual(body["entry"], "transform")
        self.assertNotIn("open", body["builtins"])
        self.assertNotIn("eval", body["builtins"])
        self.assertNotIn("os", body["modules"])
        self.assertNotIn("__import__", body["builtins"])
        self.assertIn("len", body["builtins"])
        self.assertIn("math", body["modules"])
        self.assertIsInstance(body["formatter"]["available"], bool)


if __name__ == "__main__":
    unittest.main()
