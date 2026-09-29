"""引擎的端到端测试。

这个文件的重点是**断点续跑**（需求 1 的核心断言）：在提交点之间把进程打死，恢复之后的
产物必须与不中断运行**逐字节相同**。为此这里刻意用真子进程 + `os._exit`，而不是抛一个
「模拟崩溃」的异常 —— 后者会走 Python 的异常展开，flush 行为与真崩溃完全不同，
测出来的结论不能信。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import count
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.clean_engine import (
    CleanEngine,
    CleanEngineError,
    ResumeRefused,
    collect_inputs,
    normalize_field_name,
    resolve_mapping,
    state_to_report,
)
from app.clean_llm import CallBudget, LlmClient, LlmEndpoint, LlmPool
from app.clean_models import CleanTaskConfig, FieldSpec
from app.clean_state import FileState, StateStore
from app.task_manager import TaskCancelled

BACKEND = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # -- 造数据 ------------------------------------------------------------

    def write_csv(self, name, text, encoding="utf-8"):
        path = self.tmp / "in" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def rows_csv(self, name, header, rows):
        lines = [",".join(header)] + [",".join(str(cell) for cell in row) for row in rows]
        return self.write_csv(name, "\n".join(lines) + "\n")

    def config(self, fields, **over):
        payload = {
            "name": "测试",
            "source": {"mode": "server_path", "paths": [str(self.tmp / "in")]},
            "input": {},
            "fields": fields,
            "output": {"format": "csv", "commit_rows": 2},
        }
        for key, value in over.items():
            payload[key] = value
        return CleanTaskConfig.model_validate(payload)

    # -- 跑引擎 ------------------------------------------------------------

    def store(self, name="task"):
        return StateStore(self.tmp / name)

    def run_engine(self, config, task="task", *, resume=False, store=None, **kwargs):
        store = store or self.store(task)
        engine = CleanEngine("t1", config, store, seed=7, **kwargs)
        inputs = collect_inputs(
            [str(self.tmp / "in")] if (self.tmp / "in").exists() else [], config.input
        )
        return engine.run(inputs, resume=resume), store

    def outputs(self, store):
        return {
            path.relative_to(store.output_dir).as_posix(): path.read_bytes()
            for path in sorted(store.output_dir.rglob("*"))
            if path.is_file()
        }

    def counts(self, store):
        """每个文件的行数计数 —— 产物对但计数错（把没提交的尾巴数了两遍）也要拦下来。"""
        return {
            entry.key: (entry.rows_in, entry.rows_out, entry.dropped)
            for entry in store.load().ordered()
        }

    def output_text(self, store, key):
        return (store.output_dir / key).read_text(encoding="utf-8")


# =========================================================================== 正向路径


class FullRunTests(Base):
    def test_cleans_generates_and_reports(self):
        self.rows_csv(
            "a.csv",
            ["姓名 ", "城市", "年龄"],
            [[" 张三 ", "北京", "20"], ["李四", "上海", "abc"], ["王五", "", "30"]],
        )
        config = self.config(
            [
                {
                    "dest": "姓名",
                    "source": "姓名",
                    "constraints": [{"kind": "non_null", "severity": "error"}],
                },
                {"dest": "城市", "source": "城市"},
                {
                    "dest": "年龄",
                    "source": "年龄",
                    "constraints": [
                        {"kind": "range", "value_kind": "int", "min_value": 0, "max_value": 150}
                    ],
                },
                {
                    "dest": "编号",
                    "generate": {
                        "kind": "sequence",
                        "output_fields": ["编号"],
                        "start": 1,
                        "step": 1,
                        "width": 3,
                    },
                },
                {
                    "dest": "城市标签",
                    "generate": {
                        "kind": "expression",
                        "output_fields": ["城市标签"],
                        "expression": "${城市}-CN",
                    },
                },
            ],
            ops=[{"op": "trim", "field": "姓名"}],
        )
        result, store = self.run_engine(config)

        self.assertEqual(result.status, "completed", result.error)
        rows = self.output_text(store, "a.csv").splitlines()
        self.assertEqual(rows[0], "姓名,城市,年龄,编号,城市标签")
        self.assertEqual(rows[1], "张三,北京,20,001,北京-CN")
        # 空格被 trim 掉、序号是文件绝对的、模板引用了清洗后的值
        self.assertNotIn(" 张三 ", "\n".join(rows))

        counters = store.load().progress["counters"]
        self.assertEqual(counters["violations"], {"年龄|range": 1})
        self.assertEqual(counters["fallbacks"], {"年龄|range|keep": 1})

        report = (store.dir / "report.md").read_text(encoding="utf-8")
        for section in ("字段映射", "约束检查", "回退", "复现"):
            self.assertIn(section, report)
        self.assertIn("`年龄`", report)

    def test_missing_column_filled_and_reported(self):
        self.rows_csv("a.csv", ["姓名"], [["张三"]])
        self.rows_csv("b.csv", ["姓名", "城市"], [["李四", "上海"]])
        config = self.config(
            [
                {"dest": "姓名", "source": "姓名"},
                {"dest": "城市", "source": "城市", "default": "未知"},
            ]
        )
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(
            self.output_text(store, "a.csv").splitlines(), ["姓名,城市", "张三,未知"]
        )
        state = store.load()
        self.assertIn("缺少映射列：城市", state.files["a.csv"].message)
        # 未映射列与映射关系进报告
        report = (store.dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("a.csv", report)

    def test_strict_layout_fails_only_that_file(self):
        self.rows_csv("a.csv", ["姓名"], [["张三"]])
        self.rows_csv("b.csv", ["姓名", "城市"], [["李四", "上海"]])
        config = self.config(
            [{"dest": "姓名", "source": "姓名"}, {"dest": "城市", "source": "城市"}],
            output={"format": "csv", "commit_rows": 2, "layout": "strict"},
        )
        result, store = self.run_engine(config)
        # 一个坏文件不该废掉整批：b.csv 依然产出，任务整体记为失败并说清原因
        self.assertEqual(result.status, "failed")
        self.assertIn("a.csv", result.error)
        self.assertEqual(
            self.output_text(store, "b.csv").splitlines(), ["姓名,城市", "李四,上海"]
        )
        self.assertEqual(store.load().files["a.csv"].status, "failed")

    def test_on_missing_error_stops_the_file(self):
        self.rows_csv("a.csv", ["姓名"], [["张三"]])
        config = self.config(
            [{"dest": "姓名", "source": "姓名"}, {"dest": "城市", "source": "城市"}],
            output={"format": "csv", "commit_rows": 2, "on_missing": "error"},
        )
        result, _ = self.run_engine(config)
        self.assertEqual(result.status, "failed")

    def test_ffill_carries_across_batches_and_files_reset(self):
        self.rows_csv("a.csv", ["城市", "人"], [["北京", "甲"], ["", "乙"], ["", "丙"]])
        self.rows_csv("b.csv", ["城市", "人"], [["", "丁"], ["上海", "戊"]])
        config = self.config(
            [{"dest": "城市", "source": "城市"}, {"dest": "人", "source": "人"}],
            ops=[{"op": "ffill", "field": "城市"}],
        )
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(
            self.output_text(store, "a.csv").splitlines(),
            ["城市,人", "北京,甲", "北京,乙", "北京,丙"],
        )
        # 文件边界上 carry 必须清空：b.csv 开头是空的，没有可携带的值
        self.assertEqual(
            self.output_text(store, "b.csv").splitlines(),
            ["城市,人", ",丁", "上海,戊"],
        )

    def test_dedupe_file_scope_and_task_scope(self):
        self.rows_csv("a.csv", ["姓名", "单号"], [["张三", "1"], ["张三", "2"]])
        self.rows_csv("b.csv", ["姓名", "单号"], [["张三", "3"]])
        fields = [
            {"dest": "姓名", "source": "姓名"},
            {"dest": "单号", "source": "单号"},
        ]
        shared = {"format": "csv", "commit_rows": 2}
        per_file = self.config(
            fields, ops=[{"op": "dedupe", "subset": ["姓名"], "scope": "file"}],
            output=dict(shared),
        )
        result, store = self.run_engine(per_file, task="file-scope")
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(len(self.output_text(store, "a.csv").splitlines()), 2)  # 表头 + 1
        self.assertEqual(len(self.output_text(store, "b.csv").splitlines()), 2)

        across = self.config(
            fields, ops=[{"op": "dedupe", "subset": ["姓名"], "scope": "task"}],
            output=dict(shared),
        )
        result, store = self.run_engine(across, task="task-scope")
        self.assertEqual(result.status, "completed", result.error)
        # 任务范围：b.csv 里的张三与 a.csv 里已接受的那条重复，整行被丢掉
        self.assertEqual(self.output_text(store, "b.csv").splitlines(), ["姓名,单号"])

    def test_unique_constraint_is_enforced_across_files(self):
        self.rows_csv("a.csv", ["姓名"], [["1"], ["2"]])
        self.rows_csv("b.csv", ["姓名"], [["2"], ["3"]])
        config = self.config(
            [
                {
                    "dest": "姓名",
                    "source": "姓名",
                    "constraints": [{"kind": "unique", "severity": "error"}],
                    "fallback": {"on_violation": "keep"},
                }
            ]
        )
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        state = store.load()
        # 第二条 2 被判为重复并保留（keep），计数精确
        self.assertEqual(state.progress["counters"]["violations"], {"姓名|unique": 1})
        self.assertEqual(state.progress["counters"]["fallbacks"], {"姓名|unique|keep": 1})
        # 清洗后的复检不该报重复（keep 保留的那条仍是重复，所以这里恰好为 1）
        self.assertEqual(state.progress["after_counters"]["violations"], {"姓名|unique": 1})

    def test_unique_with_truncate_suffix_resolves_duplicates(self):
        self.rows_csv("a.csv", ["姓名"], [["甲"], ["甲"], ["甲"]])
        config = self.config(
            [
                {
                    "dest": "姓名",
                    "source": "姓名",
                    "constraints": [{"kind": "unique", "severity": "error"}],
                    "fallback": {"on_violation": "truncate", "unique_suffix": "_x"},
                }
            ]
        )
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        values = self.output_text(store, "a.csv").splitlines()[1:]
        self.assertEqual(len(set(values)), 3)
        self.assertEqual(store.load().progress["after_counters"]["violations"], {})

    @staticmethod
    def llm_fields():
        return [
            {"dest": "姓名", "source": "姓名"},
            {
                "dest": "摘要",
                "generate": {"kind": "llm", "output_fields": ["摘要"], "prompt": "总结 ${姓名}"},
            },
        ]

    def test_llm_field_without_endpoints_is_refused_at_config_time(self):
        """最省事的拦法是创建期就拒绝：跑到一半才发现没配模型，用户已经等了很久。"""
        with self.assertRaises(ValidationError) as caught:
            self.config(self.llm_fields())
        self.assertIn("没有选择任何大模型端点", str(caught.exception))

    def test_llm_field_with_only_disabled_endpoints_fails_fast(self):
        """配置层过了，但池里没有可用端点（全被禁用）—— 引擎启动时就说清楚。

        不这么做的话每行都拿到空值、被判违规、再走一遍回退：任务「跑完」了，产物全是空，
        报告里是几百万条回退记录，而用户要的是「一句话告诉他端点没开」。
        """
        self.rows_csv("a.csv", ["姓名"], [["张三"]])
        config = self.config(self.llm_fields(), llm={"endpoint_ids": ["e1"]})
        pool = LlmPool(
            endpoints=[
                LlmEndpoint(
                    id="e1",
                    url="http://127.0.0.1:9/v1/chat/completions",
                    model="m",
                    enabled=False,
                )
            ]
        )
        result, store = self.run_engine(config, llm_pool=pool)
        self.assertEqual(result.status, "failed")
        self.assertIn("没有启用的端点", result.error)
        # 状态与报告照样落盘（`run` 不抛异常的契约）
        self.assertEqual(store.load().status, "failed")
        self.assertIn("没有启用的端点", (store.dir / "report.md").read_text(encoding="utf-8"))

    def test_llm_field_with_an_enabled_endpoint_starts(self):
        """端点启用时引擎必须能跑起来 —— 这条路径曾经直接 TypeError。

        `_check_llm_ready` 结尾那段冷却提示调的是必填参数的 `untried(exclude)`，调用点漏了实参。
        上面两个用例一个停在「没配端点」、一个停在「端点全禁用」，都在那一行之前返回，所以谁也没
        走到这里 —— 用户的 ollama 配好、端点一启用，试跑就崩在 `_build_runtime` 里，一次请求都
        没发出去。这里端点指向一个没人监听的端口：**不发真请求**，要的只是「跑起来了」。
        """
        self.rows_csv("a.csv", ["姓名"], [["张三"]])
        config = self.config(self.llm_fields(), llm={"endpoint_ids": ["e1"]})
        pool = LlmPool(
            endpoints=[
                LlmEndpoint(
                    id="e1",
                    url="http://127.0.0.1:9/v1/chat/completions",
                    model="m",
                    enabled=True,
                    max_retries=0,
                    timeout_s=5.0,
                )
            ]
        )
        result, store = self.run_engine(config, llm_pool=pool)
        # 站起来了：这一行按「生成失败」处理（空值 + 报告里一条说明），任务照常收尾
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(self.output_text(store, "a.csv").splitlines(), ["姓名,摘要", "张三,"])
        self.assertIn("生成失败", (store.dir / "report.md").read_text(encoding="utf-8"))

    def test_a_budget_spent_in_the_last_chunk_still_shows_up_in_the_report(self):
        """额度在**最后一个分块里**花完时，报告也必须说「调用次数已达上限」。

        护栏状态是在每个字段、每个分块**之前**看的（`_llm_stop_reason`）。所以最后一口气把
        额度用光时没人再回头看那一眼：state.json 里 `exhausted` 是 False，报告里那句警告整条
        消失 —— 用户看到的是一份「正常完成」，而后面几万行其实全是空值。并发之后一轮就能把
        预算花完，这条路更容易走到。
        """
        self.rows_csv("a.csv", ["姓名"], [["张三"], ["李四"], ["王五"]])
        # commit_rows=0 → 大模型字段用 COMMIT_ROWS_LLM（2000），3 行就只有一个分块
        config = self.config(self.llm_fields(),
                             llm={"endpoint_ids": ["e1"], "max_calls": 3},
                             output={"format": "csv", "commit_rows": 0})
        budget = CallBudget(max_calls=3)
        client = LlmClient(
            LlmPool([LlmEndpoint(id="e1", url="https://e1.test/v1", model="m")]),
            budget=budget,
            transport=lambda *args: (200, json.dumps(
                {"choices": [{"message": {"content": "值"}}]}), {}),
        )
        result, store = self.run_engine(config, llm_client=client)
        self.assertEqual(result.status, "completed", result.error)
        self.assertTrue(budget.exhausted)
        self.assertTrue(store.load().llm["exhausted"], "state.json 里没记下额度已耗尽")
        self.assertIn("调用次数已达上限", (store.dir / "report.md").read_text("utf-8"))

    def test_verify_every_reports_no_mismatch(self):
        self.rows_csv("a.csv", ["年龄"], [["20"], ["30"], ["abc"]])
        config = self.config(
            [
                {
                    "dest": "年龄",
                    "source": "年龄",
                    "constraints": [
                        {"kind": "range", "value_kind": "int", "min_value": 0, "max_value": 99}
                    ],
                }
            ],
            output={"format": "csv", "commit_rows": 2, "verify_every": 1},
        )
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        self.assertFalse([note for note in store.load().notes if "不一致" in note])

    def test_empty_input_produces_a_clean_empty_result(self):
        self.rows_csv("a.csv", ["姓名"], [])
        config = self.config([{"dest": "姓名", "source": "姓名"}])
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(self.output_text(store, "a.csv"), "姓名\n")

    def test_unmapped_source_column_is_reported_not_silently_dropped(self):
        self.rows_csv("a.csv", ["姓名", "备注"], [["张三", "x"]])
        config = self.config([{"dest": "姓名", "source": "姓名"}])
        _, store = self.run_engine(config)
        mapping = store.load().extra["mapping"]["a.csv"]
        self.assertEqual(mapping["unmapped"], ["备注"])
        self.assertEqual(self.output_text(store, "a.csv").splitlines()[0], "姓名")


# =========================================================================== 取消与续跑


class KillAfter:
    """第 n 次检查点抛取消 —— 与用户点「停止」走同一条路径。"""

    def __init__(self, n):
        self.n = n
        self.paused = False

    def checkpoint(self):
        self.n -= 1
        if self.n <= 0:
            raise TaskCancelled()
        return True


def jumpy_clock(step=10.0):
    """每次读数往后跳 `step` 秒。用来绕开进度节流（`PROGRESS_INTERVAL`），
    让「每批之后」都有一次 `on_progress` 回调 —— 否则节流会把回调吞掉，
    测试里想按行数对准取消点就变成了碰运气。"""
    ticks = count()
    return lambda: 1000.0 + step * next(ticks)


class CancelAfterRows:
    """累计处理到 N 行之后，在**下一个检查点**取消。

    不能只数检查点次数：检查点既在块之间，也在生成字段的过程中（一个两万行的块配两个
    大模型字段就是几十分钟不可中断）。数次数会让「取消落在第一次提交之前」随机出现，
    而这里要的是确定的落点 —— 提交点之后、下一次提交之前。`see` 给 `on_progress` 用，
    它只做标记，抛取消仍然由真正的 `checkpoint()` 来做（与生产路径一致）。
    """

    def __init__(self, rows):
        self.rows = rows
        self.armed = False
        self.paused = False

    def see(self, payload):
        if int(payload.get("done", 0)) >= self.rows:
            self.armed = True

    def checkpoint(self):
        if self.armed:
            raise TaskCancelled()
        return True


class ResumeTests(Base):
    HEADER = ["姓名", "城市", "单号"]
    ROWS = [
        ["u1", "北京", "1"],
        ["u1", "北京", "2"],
        ["", "上海", "3"],
        ["u2", "", "4"],
        ["u2", "", "5"],
        ["u3", "广州", "6"],
        ["u4", "深圳", "7"],
        ["", "杭州", "8"],
        ["u5", "成都", "9"],
    ]

    def fields(self):
        return [
            {
                "dest": "姓名",
                "source": "姓名",
                "constraints": [{"kind": "non_null", "severity": "error"}],
                "fallback": {"on_violation": "keep"},
            },
            {"dest": "城市", "source": "城市", "default": "未知"},
            {
                "dest": "单号",
                "source": "单号",
                "constraints": [{"kind": "unique", "severity": "error"}],
                "fallback": {"on_violation": "keep"},
            },
            {
                "dest": "城市标签",
                "generate": {
                    "kind": "expression",
                    "output_fields": ["城市标签"],
                    "expression": "${城市}-CN",
                },
            },
        ]

    def scenario(self, **over):
        self.rows_csv("a.csv", self.HEADER, self.ROWS)
        self.rows_csv("b.csv", self.HEADER, self.ROWS[:4])
        output = {"format": "csv", "commit_rows": 2}
        output.update(over.pop("output", {}))
        return self.config(
            self.fields(),
            ops=[
                {"op": "ffill", "field": "城市"},
                {"op": "dedupe", "subset": ["姓名"], "scope": "task"},
            ],
            output=output,
            **over,
        )

    def test_cancel_then_resume_is_byte_identical(self):
        config = self.scenario()
        baseline, base_store = self.run_engine(config, task="baseline")
        self.assertEqual(baseline.status, "completed", baseline.error)

        cancelled, store = self.run_engine(
            config, task="resumed", control=KillAfter(3), resume=False
        )
        self.assertEqual(cancelled.status, "cancelled")
        partial = self.outputs(store)
        self.assertNotEqual(partial, self.outputs(base_store))

        again, _ = self.run_engine(config, task="resumed", resume=True)
        self.assertEqual(again.status, "completed", again.error)
        self.assertEqual(self.outputs(store), self.outputs(base_store))
        self.assertEqual(self.counts(store), self.counts(base_store))

    def test_resume_refused_when_config_changes(self):
        config = self.scenario()
        _, store = self.run_engine(config, task="refused", control=KillAfter(3))
        changed = self.scenario()
        changed.fields[1].default = "另一个默认值"
        with self.assertRaises(ResumeRefused) as caught:
            self.run_engine(changed, task="refused", resume=True)
        self.assertTrue(caught.exception.decision.reason)
        # 拒绝续跑必须发生在任何副作用之前：产物原样不动
        self.assertEqual(store.load().status, "cancelled")

    def test_resume_refused_when_input_changes(self):
        config = self.scenario()
        _, _ = self.run_engine(config, task="input", control=KillAfter(3))
        self.rows_csv("a.csv", self.HEADER, self.ROWS + [["u9", "西安", "9"]])
        with self.assertRaises(ResumeRefused):
            self.run_engine(config, task="input", resume=True)

    def test_resume_after_done_task_is_a_noop(self):
        config = self.scenario()
        config.output.commit_rows = 2
        first, store = self.run_engine(config, task="noop")
        self.assertEqual(first.status, "completed", first.error)
        before = self.outputs(store)
        again, _ = self.run_engine(config, task="noop", resume=True)
        self.assertEqual(again.status, "completed", again.error)
        self.assertEqual(self.outputs(store), before)

    def test_resume_truncates_uncommitted_tail(self):
        """磁盘上有一段「写了但没提交」的行，续跑必须按提交点字节数把它们砍掉。

        这段尾巴真实存在：一批数据写完只是进了 writer 的缓冲，提交点才 flush。取消发生在
        两次提交之间时，`suspend()` 关句柄会把缓冲区刷到磁盘上 —— 磁盘上的字节数比
        state.json 记的多。截断坐标取错（比如取「已 flush 的字节数」而不是提交点）就会让
        续跑在尾巴后面接着写，产出一份中间重复了一段、而报告完全正常的文件。
        """
        # 提交间隔大于 2×读取批上限（20000）时，两次提交之间会夹着没提交的批次
        rows = [[f"u{index}", "北京", str(index)] for index in range(110_000)]
        self.rows_csv("a.csv", ["姓名", "城市", "单号"], rows)
        config = self.config(
            [
                {"dest": "姓名", "source": "姓名"},
                {"dest": "城市", "source": "城市"},
                {"dest": "单号", "source": "单号"},
            ],
            output={"format": "csv", "commit_rows": 50_000},
        )
        baseline, base_store = self.run_engine(config, task="tail-base")
        self.assertEqual(baseline.status, "completed", baseline.error)

        control = CancelAfterRows(80_000)
        _, store = self.run_engine(
            config,
            task="tail",
            control=control,
            on_progress=control.see,
            clock=jumpy_clock(),
        )
        state = store.load()
        self.assertEqual(state.status, "cancelled")
        entry = state.files["a.csv"]
        self.assertEqual(entry.rows_in, 60_000)  # 上一次提交点
        target = store.output_dir / "a.csv"
        # 尾巴确实在磁盘上：文件比提交点记录的坐标长
        self.assertGreater(target.stat().st_size, entry.out_bytes)
        with open(target, "ab") as handle:
            handle.write("垃圾行,不,该,留,着\n".encode("utf-8"))

        again, _ = self.run_engine(config, task="tail", resume=True)
        self.assertEqual(again.status, "completed", again.error)
        self.assertEqual(self.outputs(store), self.outputs(base_store))
        self.assertEqual(self.counts(store), self.counts(base_store))


class CrashTests(Base):
    """真崩溃：子进程在提交点之间 `os._exit(9)`，父进程再恢复。

    用子进程而不是「抛个异常模拟一下」，是因为只有 `_exit` 才会连 Python 层的缓冲区一起
    丢掉 —— 而「丢掉未 flush 的字节」正是恢复逻辑要处理的那种损坏。
    """

    CHILD = r'''
import json, os, sys
from itertools import count
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from app.clean_engine import CleanEngine, collect_inputs
from app.clean_models import CleanTaskConfig
from app.clean_state import StateStore

config = CleanTaskConfig.model_validate(json.loads(Path(sys.argv[1]).read_text()))
task_dir, crash_at = Path(sys.argv[2]), int(sys.argv[3])
ticks = count()
# 假的时钟：让进度节流形同虚设，于是「每批之后」都有机会崩
clock = lambda: 1000.0 + 10.0 * next(ticks)

def on_progress(payload):
    if payload["done"] >= crash_at:
        os._exit(9)

engine = CleanEngine("crash", config, StateStore(task_dir), seed=7,
                     on_progress=on_progress, clock=clock)
inputs = collect_inputs([str(Path(p)) for p in config.source.paths], config.input)
engine.run(inputs)
os._exit(0)
'''

    def scenario(self):
        rows = [[f"u{index}", "北京" if index % 2 else "", str(index)] for index in range(9)]
        self.rows_csv("a.csv", ["姓名", "城市", "单号"], rows)
        self.rows_csv("b.csv", ["姓名", "城市", "单号"], rows[:3])
        return self.config(
            [
                {"dest": "姓名", "source": "姓名"},
                {"dest": "城市", "source": "城市"},
                {
                    "dest": "单号",
                    "source": "单号",
                    "constraints": [{"kind": "unique", "severity": "error"}],
                    "fallback": {"on_violation": "keep"},
                },
            ],
            ops=[{"op": "ffill", "field": "城市"}],
            output={"format": "csv", "commit_rows": 2},
        )

    def crash_run(self, config, task, crash_at):
        script = self.tmp / "child.py"
        script.write_text(self.CHILD, encoding="utf-8")
        payload = self.tmp / "config.json"
        payload.write_text(config.model_dump_json(), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(BACKEND))
        proc = subprocess.run(
            [sys.executable, str(script), str(payload), str(self.tmp / task),
             str(crash_at), str(BACKEND)],
            env=env, capture_output=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 9, proc.stderr.decode("utf-8", "replace")[-2000:])
        return StateStore(self.tmp / task)

    def test_crash_between_commits_then_resume_is_byte_identical(self):
        config = self.scenario()
        baseline, base_store = self.run_engine(config, task="base")
        self.assertEqual(baseline.status, "completed", baseline.error)

        store = self.crash_run(config, "crash", crash_at=4)
        state = store.load()
        self.assertIsNotNone(state)
        self.assertEqual(state.status, "running")
        committed = state.files["a.csv"].rows_in
        self.assertEqual(committed, 2, "崩在第 4 行之后，只该提交了前 2 行")
        self.assertNotEqual(self.outputs(store), self.outputs(base_store))

        again, _ = self.run_engine(config, task="crash", resume=True)
        self.assertEqual(again.status, "completed", again.error)
        self.assertEqual(self.outputs(store), self.outputs(base_store))
        self.assertEqual(self.counts(store), self.counts(base_store))
        final = store.load()
        self.assertEqual(final.files["a.csv"].rows_in, 9)
        self.assertEqual(final.files["b.csv"].rows_in, 3)
        self.assertEqual(final.resume_count, 1)

    def test_crash_after_last_file_then_resume_finishes_the_rest(self):
        config = self.scenario()
        baseline, base_store = self.run_engine(config, task="base2")
        self.assertEqual(baseline.status, "completed", baseline.error)
        # 崩在 b.csv 的中间（a.csv 共 9 行，所以第 11 行之后就是 b.csv）
        store = self.crash_run(config, "crash2", crash_at=11)
        state = store.load()
        # b.csv 可能还没进过任何提交点（状态文件是提交时才写的），两种情形都不该有已提交行
        self.assertEqual(state.files["a.csv"].rows_in, 9)
        self.assertEqual((state.files.get("b.csv") or FileState(key="b.csv")).rows_in, 0)
        again, _ = self.run_engine(config, task="crash2", resume=True)
        self.assertEqual(again.status, "completed", again.error)
        self.assertEqual(self.outputs(store), self.outputs(base_store))
        self.assertEqual(self.counts(store), self.counts(base_store))


# =========================================================================== 试跑


class DryRunTests(Base):
    """试跑（`row_limit`）：配置干跑靠它把「一个 GB 文件」变成几百行。

    干跑跑的是**同一条流水线**（映射 → 规则 → 生成 → 约束 → 回退 → 统计），所以它能拦下
    真正会在正式任务里犯的错。代价是它必须能截断：不截断的话「校验一下配置」就等于把整个
    任务跑一遍，还会把大模型调用全花掉。
    """

    def test_row_limit_truncates_by_row_not_by_batch(self):
        # 一批最多 20000 行。若实现成「读满一批就停」，请求前 5 行会实跑 20000 行 ——
        # 恰好是干跑最不该花的那部分（每行一次大模型调用）。
        self.rows_csv("a.csv", ["n"], [[str(i)] for i in range(50)])
        config = self.config([{"dest": "n", "source": "n"}])
        result, store = self.run_engine(config, row_limit=5)
        self.assertEqual(result.status, "completed")
        entry = store.load().files["a.csv"]
        self.assertEqual(entry.rows_in, 5)
        self.assertEqual(entry.rows_out, 5)
        self.assertEqual(self.output_text(store, "a.csv"), "n\n0\n1\n2\n3\n4\n")
        # 产物是半截的，报告必须说出来 —— 否则这份报告看起来和正式任务的一模一样。
        self.assertIn("试跑", result.report)

    def test_row_limit_larger_than_the_file_changes_nothing(self):
        self.rows_csv("a.csv", ["n"], [["1"], ["2"]])
        config = self.config([{"dest": "n", "source": "n"}])
        result, store = self.run_engine(config, row_limit=999)
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.output_text(store, "a.csv"), "n\n1\n2\n")

    def test_row_limit_still_runs_constraints_and_fallbacks(self):
        """截断只砍行数，不砍流水线 —— 违规与回退必须照常出现，否则干跑就没意义了。"""
        self.rows_csv("a.csv", ["年龄"], [["20"], ["abc"], ["30"]])
        config = self.config(
            [
                {
                    "dest": "年龄",
                    "source": "年龄",
                    "constraints": [
                        {"kind": "range", "value_kind": "int", "min_value": 0, "max_value": 150}
                    ],
                    "fallback": {"on_violation": "keep"},
                }
            ]
        )
        result, store = self.run_engine(config, row_limit=2)
        self.assertEqual(store.load().files["a.csv"].rows_in, 2)
        self.assertIn("回退", result.report)
        self.assertIn("abc", result.report)


# =========================================================================== 纯函数


class MappingTests(unittest.TestCase):
    def test_normalize_strips_zero_width_and_case(self):
        self.assertEqual(normalize_field_name(" 姓​名 "), "姓名")
        self.assertEqual(normalize_field_name("Ｎａｍｅ"), "name")

    def test_exact_then_folded_then_give_up(self):
        fields = [
            FieldSpec.model_validate({"dest": "a", "source": "Name"}),
            FieldSpec.model_validate({"dest": "b", "source": "ＮＡＭＥ"}),  # 只有规范化能命中
            FieldSpec.model_validate({"dest": "c", "source": "不存在"}),
        ]
        mapping = resolve_mapping(["name", "Name", "备注"], fields)
        self.assertEqual(mapping.index, {"a": 1, "b": 0})
        self.assertEqual(mapping.missing, ["c"])
        self.assertEqual(mapping.unmapped, ("备注",))

    def test_duplicate_headers_take_the_first_column(self):
        fields = [
            FieldSpec.model_validate({"dest": "a", "source": "name"}),
            FieldSpec.model_validate({"dest": "b", "source": "name"}),
        ]
        mapping = resolve_mapping(["name", "name"], fields)
        # 重复表头取第一次出现的那一列（否则映射与取值会指向不同的列）
        self.assertEqual(mapping.index, {"a": 0, "b": 0})
        self.assertEqual(mapping.column_map, {"name": "a"})

    def test_never_falls_back_to_position(self):
        fields = [FieldSpec.model_validate({"dest": "a", "source": "不存在"})]
        mapping = resolve_mapping(["甲", "乙"], fields)
        self.assertEqual(mapping.missing, ["a"])
        self.assertEqual(mapping.index, {})


class ReportFromStateTests(Base):
    def test_report_can_be_rebuilt_from_state_alone(self):
        self.rows_csv("a.csv", ["姓名"], [["张三"], ["李四"]])
        config = self.config([{"dest": "姓名", "source": "姓名"}])
        result, store = self.run_engine(config)
        self.assertEqual(result.status, "completed", result.error)
        rebuilt = state_to_report(config, store.load(), task_id="t1")
        self.assertIn("文件清洗报告", rebuilt)
        self.assertIn("a.csv", rebuilt)

    def test_engine_without_inputs_is_an_error_not_a_crash(self):
        config = self.config([{"dest": "姓名", "source": "姓名"}])
        with self.assertRaises(CleanEngineError):
            CleanEngine("t", config, self.store("no-store")).run([], resume=True)


if __name__ == "__main__":
    unittest.main()

class ColumnFilterIntegrationTests(Base):
    def test_filters_rows_in_pipeline_and_reports_counts(self):
        self.rows_csv('filter.csv',['name','status','amount'],[
            ['Alice','done',120],['Bob','pending',20],['Carol','done',50],['Dave','done',200]])
        config=self.config([{'dest':name,'source':name} for name in ['name','status','amount']],
            ops=[{'op':'filter','field':'status','condition':'equals','value':'done'},
                 {'op':'filter','field':'amount','condition':'gte','value':'100'}])
        _,store=self.run_engine(config)
        contents='\n'.join(value.decode('utf-8-sig') for value in self.outputs(store).values())
        self.assertIn('Alice',contents)
        self.assertIn('Dave',contents)
        self.assertNotIn('Bob',contents)
        self.assertNotIn('Carol',contents)
        self.assertEqual(list(self.counts(store).values()),[(4,2,2)])
        from app.clean_report import _op_target
        self.assertIn('保留命中行',_op_target(config.ops[0]))
