"""clean_python / clean_worker 的测试。

子进程契约是这一整套东西里最容易「看起来对」的部分：正常路径一次就过，真正会出问题的是
超时之后管道里残留的帧、被外面杀掉的进程、用户 `print()` 打断协议、以及一个函数块块
超时时的重启风暴。所以下面**故意走这些坏路径**，而不是只测一条 happy path。

真起子进程，所以这些用例比纯单元测试慢（每个约 50–300ms）。这是必须付的代价：
mock 掉子进程就等于不测这个模块。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest

from app.clean_models import PythonFunction, PythonOptions
from app.clean_python import WORKER_SCRIPT, PythonPool, PythonRunnerError
from app.clean_worker import PROTOCOL_VERSION, validate_source

IDENTITY = "def transform(row):\n    return {'b': row['a']}\n"
SLOW = "def transform(row):\n    import time\n    time.sleep(30)\n    return {'b': row['a']}\n"


def function(name="f", source=IDENTITY, **overrides) -> PythonFunction:
    data = {
        "name": name,
        "source": source,
        "input_fields": ["a"],
        "output_fields": ["b"],
    }
    data.update(overrides)
    return PythonFunction(**data)


class WorkerTestCase(unittest.TestCase):
    maxDiff = 1500

    def pool(self, functions, options=None, seed=0):
        events: list[tuple[str, str]] = []
        pool = PythonPool(
            functions, options or PythonOptions(), seed=seed,
            on_event=lambda level, message: events.append((level, message)),
        )
        self.addCleanup(pool.close)
        pool.events = events  # type: ignore[attr-defined]
        return pool

    def levels(self, pool, level: str) -> list[str]:
        return [m for lv, m in pool.events if lv == level]

    def wait_until(self, predicate, timeout: float = 3.0, message: str = ""):
        """等异步的读取线程把 stderr 收完。

        调用返回只代表**协议帧**到了；stderr 是另一根管道、另一个线程在读，它落后于
        协议帧是正常的。断言这类东西必须等，否则测的是调度运气。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        self.fail(message or "等待条件超时")

    def spawn_child(self):
        """直接起一个子进程来测协议本身（不经过父进程侧）。"""
        proc = subprocess.Popen(
            [sys.executable, "-u", "-E", str(WORKER_SCRIPT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def cleanup():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    stream.close()

        self.addCleanup(cleanup)
        return proc

    def assert_dead(self, workers):
        """关掉之后不能留下活着的子进程 —— 残留的子进程会一直等到超时才走。"""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(w.alive for w in workers):
            time.sleep(0.05)
        self.assertFalse([w.pid for w in workers if w.alive], "关闭后仍有子进程存活")


# =========================================================================== 正常路径


class HappyPathTests(WorkerTestCase):
    def test_row_mode_returns_sparse_changes(self):
        pool = self.pool([function()])
        result = pool.worker().call_rows("f", ["a"], [[1], [2]])
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.rows, [{"b": 1}, {"b": 2}])

    def test_returning_none_means_no_change(self):
        source = (
            "def transform(row):\n"
            "    if row['a'] < 0:\n"
            "        return None\n"
            "    return {'b': row['a']}\n"
        )
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [[1], [-1]])
        self.assertEqual(result.rows, [{"b": 1}, None])

    def test_only_declared_fields_are_written_back(self):
        # 返回了未声明的键：忽略 + 提示。用户以为写进去了、实际没有，是最坏的情况
        source = "def transform(row):\n    return {'b': 1, 'c': 2}\n"
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [[1]])
        self.assertEqual(result.rows, [{"b": 1}])
        self.assertTrue(any("未声明" in note for note in result.notes), result.notes)

    def test_column_mode_round_trip(self):
        source = "def transform(cols):\n    return {'b': [v * 2 for v in cols['a']]}\n"
        pool = self.pool([function(source=source, mode="column", input_fields=["a"])])
        result = pool.worker().call_columns("f", {"a": [1, 2, 3]})
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.columns, {"b": [2, 4, 6]})

    def test_column_mode_length_mismatch_is_a_shape_error(self):
        source = "def transform(cols):\n    return {'b': cols['a'][:1]}\n"
        pool = self.pool([function(source=source, mode="column", input_fields=["a"])])
        result = pool.worker().call_columns("f", {"a": [1, 2, 3]})
        self.assertEqual(result.failure, "shape_error")
        self.assertIn("等长", result.detail)

    def test_column_mode_wrong_return_type_is_a_shape_error(self):
        source = "def transform(cols):\n    return [1, 2]\n"
        pool = self.pool([function(source=source, mode="column", input_fields=["a"])])
        result = pool.worker().call_columns("f", {"a": [1]})
        self.assertEqual(result.failure, "shape_error")

    def test_hot_reuse_across_batches(self):
        """跨批复用是热复用的全部意义：第二批复用同一个子进程，不能重新启动。

        每块都重启的话，100 万行 / 2000 行一块 = 500 次启动 ≈ 2.5 分钟纯开销。
        """
        pool = self.pool([function()])
        worker = pool.worker()
        for _ in range(3):
            self.assertTrue(worker.call_rows("f", ["a"], [[1]]).ok)
        self.assertEqual(worker.stats["starts"], 1)
        self.assertEqual(worker.stats["calls"], 3)

    def test_seed_is_visible_to_the_function(self):
        source = "def transform(row):\n    return {'b': SEED}\n"
        pool = self.pool([function(source=source)], seed=42)
        self.assertEqual(pool.worker().call_rows("f", ["a"], [[1]]).rows, [{"b": 42}])

    def test_helper_functions_and_module_data_are_available(self):
        source = (
            "import re\n"
            "PATTERN = re.compile(r'^a+')\n"
            "\n"
            "def _count(text):\n"
            "    match = PATTERN.match(text)\n"
            "    return len(match.group(0)) if match else 0\n"
            "\n"
            "def transform(row):\n"
            "    return {'b': _count(row['a'])}\n"
        )
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [["aaaa"], ["bb"]])
        self.assertEqual(result.rows, [{"b": 4}, {"b": 0}])

    def test_non_json_values_survive_as_strings(self):
        # 值跨进程会变成 JSON：日期落成字符串。这是协议的有意取舍，钉住它免得被当成 bug 改
        source = (
            "def transform(row):\n"
            "    from datetime import date\n"
            "    return {'b': date(2026, 9, 26)}\n"
        )
        pool = self.pool([function(source=source)])
        self.assertEqual(pool.worker().call_rows("f", ["a"], [[1]]).rows, [{"b": "2026-09-26"}])

    def test_imports_work_inside_a_function(self):
        # 受限内建里没有 __import__ 的话，白名单里的模块也导不进来
        source = "def transform(row):\n    import math\n    return {'b': math.floor(row['a'])}\n"
        pool = self.pool([function(source=source)])
        self.assertEqual(pool.worker().call_rows("f", ["a"], [[1.9]]).rows, [{"b": 1}])


# =========================================================================== 逐行错误


class RowErrorTests(WorkerTestCase):
    def test_one_bad_row_does_not_kill_the_batch(self):
        source = (
            "def transform(row):\n"
            "    if row['a'] == 2:\n"
            "        raise ValueError('我不喜欢 2')\n"
            "    return {'b': row['a']}\n"
        )
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [[1], [2], [3]])
        self.assertTrue(result.ok)
        self.assertEqual(result.rows, [{"b": 1}, None, {"b": 3}])
        self.assertEqual(result.error_pairs(), [(1, "ValueError: 我不喜欢 2")])
        self.assertEqual(pool.worker().stats["row_errors"], 1)

    def test_returning_a_non_dict_is_an_error_not_a_crash(self):
        source = "def transform(row):\n    return 42\n"
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [[1]])
        self.assertEqual(result.rows, [None])
        self.assertIn("dict 或 None", result.errors[0].error)

    def test_error_text_keeps_the_exception_type(self):
        source = "def transform(row):\n    return row['missing']\n"
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [[1]])
        self.assertIn("KeyError", result.errors[0].error)

    def test_an_error_in_one_function_does_not_affect_the_other(self):
        # 两个函数跑在同一个子进程里：一个炸了，另一个必须照常工作
        pool = self.pool([
            function(name="bad", source="def transform(row):\n    raise RuntimeError('x')\n"),
            function(name="good"),
        ])
        worker = pool.worker()
        self.assertEqual(worker.call_rows("bad", ["a"], [[1]]).rows, [None])
        self.assertEqual(worker.call_rows("good", ["a"], [[5]]).rows, [{"b": 5}])


# =========================================================================== 协议边界


class ProtocolTests(WorkerTestCase):
    def test_user_print_does_not_corrupt_the_protocol(self):
        """用户 `print()` 必须无害：协议走 dup 出来的真 stdout，fd 1 被换成 stderr。"""
        source = (
            "def transform(row):\n"
            "    print('调试：', row['a'])\n"
            "    print('第二行')\n"
            "    return {'b': row['a']}\n"
        )
        pool = self.pool([function(source=source)])
        result = pool.worker().call_rows("f", ["a"], [[1], [2]])
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.rows, [{"b": 1}, {"b": 2}])
        # 输出没丢，只是走 stderr 到了日志里。转发在另一根管道、另一个线程上，协议帧回来
        # 不代表它已经到了 —— 不等就是在测调度运气（这条曾偶发失败）。
        self.wait_until(
            lambda: any("调试：" in line for line in self.levels(pool, "info")),
            message=f"stderr 没转发到日志：{self.levels(pool, 'info')}",
        )

    def test_stderr_is_rate_limited_but_keeps_the_tail(self):
        source = (
            "def transform(row):\n"
            "    for i in range(30):\n"
            "        print('行', i)\n"
            "    return {'b': 1}\n"
        )
        pool = self.pool([function(source=source)], PythonOptions(stderr_per_minute=5))
        worker = pool.worker()
        self.assertTrue(worker.call_rows("f", ["a"], [[1]]).ok)
        # 先等转发线程读完再数：`call_rows` 返回只代表协议帧到了，stderr 是另一根管道、
        # 另一个线程在读。转发顺着读顺序做，所以「行 29」在 tail 里 ⇒ 30 行已读完 ⇒
        # 前 5 行的转发决定早已做出，此刻的数量才是最终值（不等就是在测调度运气）。
        self.wait_until(
            lambda: "行 29" in "\n".join(worker._tail), message="stderr 尾部没有收到最后一行"
        )
        # 限速只影响日志转发；tail 始终保留最后几行（失败时靠它定位）
        self.assertEqual(worker.stats["stderr_lines"], 30)
        # 30 行只转发了 5 行，剩下的记在「被省略」计数里（下一次窗口切换时报出来）
        self.assertEqual(len(self.levels(pool, "info")), 5)

    def test_unknown_function_name_is_a_protocol_error(self):
        pool = self.pool([function()])
        result = pool.worker().call_rows("nope", ["a"], [[1]])
        self.assertEqual(result.failure, "protocol_error")
        self.assertIn("nope", result.detail)

    def test_child_rejects_a_protocol_version_mismatch(self):
        """直接对着子进程跑：协议版本不对时必须明确拒绝，而不是按旧版语义硬解释下去。"""
        proc = self.spawn_child()
        frame = {"id": 1, "op": "init", "protocol": PROTOCOL_VERSION + 99, "functions": []}
        proc.stdin.write(json.dumps(frame).encode() + b"\n")
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        self.assertEqual(reply["id"], 1)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["kind"], "protocol_error")
        self.assertEqual(proc.wait(timeout=5), 3)

    def test_child_rejects_a_first_frame_that_is_not_init(self):
        proc = self.spawn_child()
        proc.stdin.write(b'{"id": 7, "op": "call", "fn": "f"}\n')
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        self.assertEqual(reply["kind"], "protocol_error")
        self.assertIn("init", reply["error"])
        self.assertEqual(proc.wait(timeout=5), 3)

    def test_child_exits_cleanly_on_eof(self):
        """stdin 关掉就退出：父进程自己被杀时，子进程不能留在原地。"""
        proc = self.spawn_child()
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0)


# =========================================================================== 超时与重启


class TimeoutTests(WorkerTestCase):
    def test_timeout_kills_the_process_group_and_restarts_lazily(self):
        pool = self.pool(
            [function(name="slow", source=SLOW)],
            PythonOptions(max_restarts=5, timeout_default_s=1.0),
        )
        worker = pool.worker()
        started = time.monotonic()
        result = worker.call_rows("slow", ["a"], [[1]])
        elapsed = time.monotonic() - started
        self.assertEqual(result.failure, "timeout")
        self.assertLess(elapsed, 10, "超时应当在 timeout_s 附近结束，而不是等函数自己跑完")
        self.assertFalse(worker.alive, "超时后子进程必须已经被杀")
        self.assertTrue(self.levels(pool, "error"))

        # 下一次调用才重启（不在超时那一刻重启，免得一个块块超时的函数反复付启动代价）
        self.assertEqual(pool.restarts, 0)
        self.assertEqual(worker.call_rows("slow", ["a"], [[1]]).failure, "timeout")
        self.assertEqual(pool.restarts, 1)

    def test_restart_budget_disables_the_pool(self):
        pool = self.pool(
            [function(name="slow", source=SLOW)],
            PythonOptions(max_restarts=2, timeout_default_s=0.5),
        )
        worker = pool.worker()
        for _ in range(5):
            result = worker.call_rows("slow", ["a"], [[1]])
        self.assertEqual(result.failure, "disabled")
        self.assertTrue(pool.disabled)
        self.assertLessEqual(pool.restarts, 3)
        # 禁用必须说清原因，否则报告里只有一句「跳过了」
        self.assertIn("重启次数已达上限", pool.reason)
        self.assertTrue(any("上限" in message for message in self.levels(pool, "error")))

    def test_external_kill_is_recovered_with_a_restart(self):
        """子进程被外面杀掉（OOM killer、段错误）也要走重启计数，否则会无限重启。"""
        pool = self.pool([function()], PythonOptions(max_restarts=3))
        worker = pool.worker()
        self.assertTrue(worker.call_rows("f", ["a"], [[1]]).ok)
        os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
        time.sleep(0.2)
        result = worker.call_rows("f", ["a"], [[2]])
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.rows, [{"b": 2}])
        self.assertEqual(pool.restarts, 1)
        # 重启原因要能定位：退出码是最起码的线索
        self.assertTrue(any("退出码" in message for message in self.levels(pool, "warn")))

    def test_stale_frames_from_the_killed_round_are_discarded(self):
        """超时被杀之后管道里可能残留上一轮的帧 —— id 不匹配必须丢弃，不能当成本次结果。"""
        pool = self.pool(
            [function(name="slow", source=SLOW), function(name="fast")],
            PythonOptions(timeout_default_s=0.5, max_restarts=5),
        )
        worker = pool.worker()
        self.assertEqual(worker.call_rows("slow", ["a"], [[1]]).failure, "timeout")
        result = worker.call_rows("fast", ["a"], [[7]])
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.rows, [{"b": 7}])


# =========================================================================== 内存与装载


class LimitsTests(WorkerTestCase):
    def test_memory_limit_becomes_a_block_level_error(self):
        source = (
            "def transform(row):\n"
            "    x = [0] * (200 * 1000 * 1000)\n"
            "    return {'b': len(x)}\n"
        )
        pool = self.pool(
            [function(source=source)], PythonOptions(memory_mb=256, max_restarts=2)
        )
        worker = pool.worker()
        result = worker.call_rows("f", ["a"], [[1]], timeout=20)
        self.assertEqual(result.failure, "memory_error")
        # 撞过上限的进程不留着：下一批不会更好
        self.assertFalse(worker.alive)

    def test_low_memory_limit_is_reported(self):
        pool = self.pool([function()], PythonOptions(memory_mb=200))
        worker = pool.worker()
        worker.start()
        self.assertTrue(
            any("256MB" in message for message in self.levels(pool, "warn")),
            self.levels(pool, "warn"),
        )

    def test_a_bad_function_disables_the_whole_pool(self):
        """装载失败是配置问题，重试一万次也是同一份源码 —— 必须整体禁用。"""
        bad = function(
            name="bad", source="def transform(row):\n    return open('/etc/passwd')\n"
        )
        pool = self.pool([bad], PythonOptions(max_restarts=5))
        result = pool.worker().call_rows("bad", ["a"], [[1]])
        self.assertEqual(result.failure, "init_error")
        self.assertTrue(pool.disabled)
        # 第二次调用连子进程都不该起
        self.assertEqual(pool.worker().call_rows("bad", ["a"], [[1]]).failure, "disabled")

    def test_module_level_exception_is_an_init_error(self):
        pool = self.pool([function(source="raise RuntimeError('装载就炸')\n")])
        result = pool.worker().call_rows("f", ["a"], [[1]])
        self.assertEqual(result.failure, "init_error")
        self.assertIn("装载就炸", result.detail)

    def test_missing_entry_point_is_an_init_error(self):
        pool = self.pool([function(source="def other(row):\n    return {}\n")])
        result = pool.worker().call_rows("f", ["a"], [[1]])
        self.assertEqual(result.failure, "init_error")
        self.assertIn("transform", result.detail)


# =========================================================================== 池与线程


class PoolTests(WorkerTestCase):
    def test_each_thread_gets_its_own_worker(self):
        pool = self.pool([function()])
        main_worker = pool.worker()
        self.assertIs(pool.worker(), main_worker)

        seen: list = []
        failures: list[str] = []

        def run():
            # 断言在线程里抛是静默的，得把结果搬回主线程
            try:
                worker = pool.worker()
                result = worker.call_rows("f", ["a"], [[1]])
                seen.append((worker, result.ok))
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(seen), 1)
        worker, ok = seen[0]
        self.assertTrue(ok)
        self.assertIsNot(worker, main_worker)
        self.assertIsNotNone(worker.pid)
        self.assertNotEqual(worker.pid, main_worker.pid)

    def test_worker_refuses_to_be_shared_across_threads(self):
        pool = self.pool([function()])
        worker = pool.worker()
        self.assertTrue(worker.call_rows("f", ["a"], [[1]]).ok)
        error: list[str] = []

        def run():
            try:
                worker.call_rows("f", ["a"], [[1]])
            except PythonRunnerError as exc:
                error.append(str(exc))

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertTrue(error, "跨线程调用必须直接报错，而不是静默串行化")

    def test_close_kills_all_children(self):
        pool = self.pool([function()])
        pool.worker().call_rows("f", ["a"], [[1]])
        workers = list(pool._workers)
        self.assertTrue(workers[0].alive)
        pool.close()
        self.assert_dead(workers)

    def test_calls_after_close_do_not_start_a_new_child(self):
        pool = self.pool([function()])
        worker = pool.worker()
        self.assertTrue(worker.call_rows("f", ["a"], [[1]]).ok)
        worker.close()
        # 关掉之后再调用如果静默起了新子进程，那就是个泄漏，不是功能
        self.assertEqual(worker.call_rows("f", ["a"], [[1]]).failure, "closed")
        self.assertIsNone(worker.pid)

    def test_pool_context_manager_closes_children(self):
        with PythonPool([function()]) as pool:
            pool.worker().call_rows("f", ["a"], [[1]])
            workers = list(pool._workers)
        self.assert_dead(workers)

    def test_each_pool_gets_its_own_process(self):
        pools = [PythonPool([function()]) for _ in range(2)]
        self.addCleanup(lambda: [p.close() for p in pools])
        pids = []
        for item in pools:
            worker = item.worker()
            self.assertTrue(worker.call_rows("f", ["a"], [[1]]).ok)
            pids.append(worker.pid)
        self.assertEqual(len(set(pids)), 2)

    def test_a_closed_worker_does_not_disable_the_pool(self):
        # 关掉一个 worker 只是「本线程不用了」，不该拖累别的线程
        pool = self.pool([function()])
        first = pool.worker()
        first.call_rows("f", ["a"], [[1]])
        first.close()
        self.assertEqual(first.call_rows("f", ["a"], [[1]]).failure, "closed")
        self.assertFalse(pool.disabled)


# =========================================================================== 静态闸门


class GateTests(unittest.TestCase):
    def test_allowed_imports_pass(self):
        self.assertEqual(validate_source("import math, re, orjson\nimport datetime\n"), [])

    def test_filesystem_and_network_modules_are_rejected(self):
        for module in ("os", "sys", "subprocess", "socket", "shutil", "pathlib", "requests"):
            with self.subTest(module=module):
                self.assertTrue(validate_source(f"import {module}\n"), module)

    def test_from_import_is_checked(self):
        self.assertTrue(validate_source("from os import path\n"))
        self.assertEqual(validate_source("from math import floor\n"), [])

    def test_relative_imports_are_rejected(self):
        self.assertTrue(validate_source("from . import helper\n"))

    def test_banned_builtins_are_rejected(self):
        for name in ("open", "eval", "exec", "compile", "__import__", "input", "globals"):
            with self.subTest(name=name):
                self.assertTrue(validate_source(f"def transform(row):\n    return {name}('x')\n"))

    def test_dunder_attributes_are_rejected(self):
        self.assertTrue(validate_source("def transform(row):\n    return row.__class__\n"))
        self.assertTrue(validate_source("x = (1).__class__.__base__\n"))

    def test_module_level_loop_needs_a_break(self):
        self.assertTrue(validate_source("while True:\n    pass\n"))
        self.assertTrue(validate_source("for i in range(3):\n    print(i)\n"))
        self.assertEqual(validate_source("for i in range(3):\n    break\n"), [])

    def test_loops_inside_functions_are_fine(self):
        # 函数体内的循环是每一行都要跑的，不能按模块级规则拦
        source = (
            "def transform(row):\n"
            "    total = 0\n"
            "    for ch in row['a']:\n"
            "        total += ord(ch)\n"
            "    return {'b': total}\n"
        )
        self.assertEqual(validate_source(source), [])

    def test_syntax_error_is_reported_with_the_line(self):
        problems = validate_source("def transform(row)\n    return {}\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("语法错误", problems[0])

    def test_the_gate_is_applied_to_every_function(self):
        self.assertTrue(validate_source("import os\n", "f1")[0].startswith("函数 f1："))


if __name__ == "__main__":
    unittest.main()
