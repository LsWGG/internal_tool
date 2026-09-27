"""clean_format（ruff 格式化）的测试。

真调 ruff：这个模块的全部价值就是「venv 里那一份 ruff 到底怎么回话」—— mock 掉 subprocess
等于把唯一要测的东西换成了自己的想法。缺 ruff 就 skip 并写清原因（与 test_clean_python
「真起子进程」的立场一致）。

三件事必须钉住：格式化**不改语义**（幂等、过闸门）、出错时**不抛异常**（返回错误说明，
让界面能原样显示 ruff 的插入符图示）、以及**不往服务的 cwd 里拉屎**（`.ruff_cache`）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from app.clean_format import format_source, ruff_command
from app.clean_worker import validate_source

MESSY = "def transform(row):\n    x = row['a']+1\n    if x>2:\n      return {'b':x}\n    return None\n"
NEAT = 'def transform(row):\n    x = row["a"] + 1\n    if x > 2:\n        return {"b": x}\n    return None\n'

_MISSING = ruff_command() is None
REASON = "环境里没有 ruff（uv pip install -r backend/requirements.txt 后重启）"


class RuffLookupTests(unittest.TestCase):
    def test_override_wins_when_it_points_at_something(self):
        with mock.patch.dict(os.environ, {"CLEAN_RUFF": "/bin/sh"}):
            self.assertEqual(ruff_command(), ["/bin/sh"])

    def test_override_to_a_missing_file_is_none_not_a_fallback(self):
        """写了 `CLEAN_RUFF` 就是「用这个」。悄悄退回别的 ruff，用户会以为配置生效了。"""
        with mock.patch.dict(os.environ, {"CLEAN_RUFF": "/nonexistent/ruff-xyz"}):
            self.assertIsNone(ruff_command())

    @unittest.skipIf(_MISSING, REASON)
    def test_default_lookup_finds_this_interpreter_s_module(self):
        """这次装 ruff 装进了 venv，所以要命中 `python -m ruff` 那一档。"""
        command = ruff_command()
        self.assertIsNotNone(command)
        self.assertEqual(command[-1], "ruff")


class FormatTests(unittest.TestCase):
    @unittest.skipIf(_MISSING, REASON)
    def test_messy_source_comes_back_neat(self):
        out, error = format_source(MESSY, "transform")
        self.assertEqual(error, "")
        self.assertEqual(out, NEAT)

    @unittest.skipIf(_MISSING, REASON)
    def test_formatting_is_idempotent(self):
        once, _ = format_source(MESSY, "transform")
        twice, error = format_source(once, "transform")
        self.assertEqual(error, "")
        self.assertEqual(twice, once)

    @unittest.skipIf(_MISSING, REASON)
    def test_formatted_source_still_passes_the_gate(self):
        """排版不能把代码排成闸门不认的样子（单双引号、隐式拼接都可能踩到正则式的检查）。"""
        out, _ = format_source(MESSY, "transform")
        self.assertEqual(validate_source(out, "transform"), [])

    @unittest.skipIf(_MISSING, REASON)
    def test_banned_name_survives_formatting_and_is_still_rejected(self):
        """`open(...)` 这种要拒的东西，格式化后依然要拒 —— 格式化不是洗白。"""
        out, _ = format_source("def transform(row):\n    return open('x')\n", "transform")
        self.assertNotEqual(out, "")
        self.assertEqual(len(validate_source(out, "transform")), 1)
        self.assertIn("open", validate_source(out, "transform")[0])

    @unittest.skipIf(_MISSING, REASON)
    def test_syntax_error_reports_ruff_s_words_and_keeps_the_source_untouched(self):
        out, error = format_source("def transform(:\n", "transform")
        self.assertEqual(out, "")
        self.assertIn("Failed to parse", error)
        # 报错里的文件名就是函数名（`--stdin-filename`），行号是源码里的行号 ——
        # 卡片里照着这句就能找到第 1 行。
        self.assertIn("transform.py:1:", error)

    @unittest.skipIf(_MISSING, REASON)
    def test_a_weird_function_name_only_shows_up_as_a_safe_label(self):
        """函数名是用户输入。它会被拼进 `--stdin-filename`，所以只能出现白名单字符。"""
        out, error = format_source(MESSY, "../../etc/passwd 中文")
        self.assertEqual(error, "")
        self.assertEqual(out, NEAT)

    @unittest.skipIf(_MISSING, REASON)
    def test_running_the_formatter_leaves_no_ruff_cache_in_the_cwd(self):
        """`--no-cache` 的具体验证：服务的 cwd（backend/）不该被写进缓存目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                format_source(MESSY, "transform")
                self.assertNotIn(".ruff_cache", os.listdir(tmp))
            finally:
                os.chdir(cwd)

    def test_missing_ruff_is_a_message_not_an_exception(self):
        """`ok:false` + 一句人话，和 `/llm/endpoints/test` 是同一条约定。"""
        with mock.patch("app.clean_format.ruff_command", return_value=None):
            out, error = format_source(MESSY, "transform")
        self.assertEqual(out, "")
        self.assertIn("未找到 ruff", error)


if __name__ == "__main__":
    unittest.main()
