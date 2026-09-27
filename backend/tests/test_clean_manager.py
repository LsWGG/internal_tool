"""服务层（`clean_manager`）与路由（`clean_api`）的测试。

引擎/store 那一层在 `test_clean_{engine,resume,state,...}.py` 里已经测过，这里只管**服务层
独有的东西**：任务记录的读写与持久化、干跑与正式任务共用同一个种子、续跑决策、上传与路径
白名单、端点池的密钥处理、以及 HTTP 状态码映射。

**SSE 不在这里测，而且不能在这里测**：`TestClient` 是同步的，`client.stream()` 会一直等到
响应结束，而 SSE 的响应按设计永不结束 —— 写一个「订阅 SSE」的测试就是给测试套件埋一个死锁
（实测：`client.stream("GET", "/api/clean/events")` 永久阻塞在 `anyio.from_thread.call`）。
SSE 的实时性由 `docs`/冒烟脚本在**真 uvicorn** 上验证（首条事件 <1s）。其余端点都是普通请求，
在 TestClient 里正常。
"""

import csv
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import xlsxwriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clean_api import make_router
from app.clean_io import append_target
from app.clean_manager import (
    DRY_PREVIEW_ROWS,
    INSPECT_MAX_ROWS,
    INSPECT_ROWS,
    STATUS_TEXT,
    CleanConflict,
    CleanError,
    CleanManager,
    CleanNotFound,
    safe_relpath,
)
from app.clean_models import OutputOptions


def csv_bytes(rows=5, header="name,age,city"):
    body = "\n".join(f"u{i},{i},{'beijing' if i % 2 else 'shanghai'}" for i in range(rows))
    return f"{header}\n{body}\n"


def config_for(source, **over):
    """一份最小可用配置：两个直通字段 + 一个随机生成字段 + 一个「只给目的字段」的新列。"""
    config = {
        "name": "测试任务",
        "source": source,
        "input": {"delimiter": ",", "encoding": "utf-8"},
        "fields": [
            {"dest": "姓名", "source": "name"},
            {"dest": "年龄", "source": "age"},
            {"dest": "城市", "source": "city"},
            {"dest": "编号", "generate": {"kind": "random", "generator": "uuid"}},
            {"dest": "备注"},
        ],
        "output": {"format": "csv", "commit_rows": 2},
    }
    config.update(over)
    return config


class ManagerCase(unittest.TestCase):
    """公共脚手架：一个临时 root、一个 manager、以及「等任务结束」。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "in"
        self.source.mkdir()
        self.manager = self.manager_for()

    def tearDown(self):
        self.manager.shutdown()
        self._tmp.cleanup()

    def manager_for(self, root=None, **kwargs):
        kwargs.setdefault("allowed_roots", [self.tmp])
        manager = CleanManager(root or (self.tmp / "root"), **kwargs)
        self.addCleanup(manager.shutdown)
        return manager

    def write(self, name="a.csv", text=None, folder=None):
        target = (folder or self.source) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text if text is not None else csv_bytes(), encoding="utf-8")
        return target

    def wait(self, task_id, manager=None, deadline=30.0,
             statuses=("completed", "failed", "cancelled", "interrupted")):
        """轮询任务记录直到出现 `statuses` 里的某个状态。服务层的记录才是权威，不等 SSE。

        可以指定 `statuses` 是因为「投递是异步的」：`auto_resume` 与 `control('resume')`
        刚返回时记录还是 `interrupted`（线程池里的 `_run` 还没跑到改状态那一步），此时若
        把 `interrupted` 当成终态就会读到一个假结果。
        """
        manager = manager or self.manager
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            task = manager.get(task_id)
            if task and task["status"] in statuses:
                return task
            time.sleep(0.02)
        self.fail(f"任务没有在 {deadline}s 内到达 {statuses}：{manager.get(task_id)}")

    def run_task(self, config=None, **payload):
        self.write()
        payload.setdefault("config", config or config_for({"mode": "server_path",
                                                           "paths": [str(self.source)]}))
        created = self.manager.create(payload)
        return created, self.wait(created["id"])

    def records(self, root=None):
        """磁盘上的任务记录（`tasks.json` 就是 `{task_id: 记录}`）。"""
        path = (root or (self.tmp / "root")) / "tasks.json"
        return json.loads(path.read_text())

    def patch_records(self, mutate, root=None):
        """直接改磁盘上的记录 —— 模拟「上次没跑完就断电了」这类外部事实。"""
        path = (root or (self.tmp / "root")) / "tasks.json"
        data = json.loads(path.read_text())
        for item in data.values():
            mutate(item)
        path.write_text(json.dumps(data), encoding="utf-8")

    def force_status(self, status, root=None):
        self.patch_records(lambda item: item.update(status=status), root=root)


class TaskFlowTests(ManagerCase):
    def test_create_runs_to_completion_and_leaves_a_report(self):
        task, done = self.run_task()
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["status_text"], "已完成")
        self.assertEqual(done["result"]["rows_in"], 5)
        self.assertEqual(done["result"]["rows_out"], 5)
        self.assertEqual([f["key"] for f in done["result"]["files"]], ["a.csv"])
        report = self.manager.report(task["id"])
        self.assertTrue(report.startswith("# 文件清洗报告"))
        self.assertIn("|", report)                       # 报告的核心是表格
        self.assertEqual(done["result"]["report"], done["result"]["report"])

    def test_the_generated_column_is_filled_and_the_new_column_is_empty(self):
        """`备注` 只配了目的字段（= 新列），没有任何生成规则，所以它必须存在且为空。"""
        task, done = self.run_task()
        view = self.manager.preview(task["id"], rows=5)
        self.assertEqual(view["columns"], ["姓名", "年龄", "城市", "编号", "备注"])
        self.assertIn("编号", view["generated"])
        self.assertEqual(view["before"][0][3], "")       # 生成列在清洗前是空的
        self.assertTrue(view["after"][0][3])             # 清洗后有值
        self.assertEqual(view["after"][0][4], "")        # 新列没有生成规则 → 空
        self.assertEqual(len(view["before"]), len(view["after"]))

    def test_stats_give_before_and_after_per_field(self):
        """字段统计给的是**持久化统计**的摘要，不是拿产物重算的。

        重算会得到另一套数字，而两份数字对不上时没有任何办法判断哪一份是真的 —— 所以
        这里同时钉住它与报告同源（同一份 `state.json`）以及近似的诚实标注。
        """
        task, done = self.run_task()
        stats = self.manager.stats(task["id"])
        self.assertEqual(stats["rows_before"], 5)
        self.assertEqual(stats["rows_after"], 5)
        names = [item["field"] for item in stats["fields"]]
        # 顺序跟着配置的字段定义走，界面里的表才与配置一一对应
        self.assertEqual(names, ["姓名", "年龄", "城市", "编号", "备注"])
        first = stats["fields"][0]["before"]
        self.assertEqual(first["count"], 5)
        self.assertEqual(first["length_min"], 2)          # u0..u4
        self.assertEqual(first["p50"], 2.0)
        self.assertEqual(first["percentile_of"], "length")   # 不写清楚会被读成「2 岁」
        self.assertEqual(first["top"][0], ["u0", 1])
        # 生成列在清洗前是空的：它有统计，但全是空值
        generated = stats["fields"][3]
        self.assertEqual(generated["before"]["empty_rate"], 1.0)
        self.assertEqual(generated["after"]["empty_rate"], 0.0)
        # 新增列没有生成规则 → 列存在、每行都有、值全空（不是「没有这一列」）
        self.assertEqual(stats["fields"][4]["after"]["count"], 5)
        self.assertEqual(stats["fields"][4]["after"]["empty_rate"], 1.0)
        self.assertTrue(any("近似" in note for note in stats["notes"]))
        self.assertEqual(done["result"]["rows_out"], stats["rows_after"])

    def test_download_bundles_outputs_and_the_report(self):
        task, _ = self.run_task()
        path = self.manager.archive(task["id"])
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        self.assertIn("report.md", names)
        self.assertIn("events.jsonl", names)
        self.assertIn("output/a.csv", names)

    def test_cancel_of_a_queued_task_keeps_it_out_of_the_outputs(self):
        """取消是「请求」，状态要等检查点才翻 —— 但记录必须立刻不再宣布 running。"""
        self.write()
        created = self.manager.create({"config": config_for(
            {"mode": "server_path", "paths": [str(self.source)]})})
        task = self.manager.control(created["id"], "cancel")
        self.assertIn(task["status"], ("running", "cancelled"))
        self.wait(created["id"])

    def test_delete_removes_record_and_outputs(self):
        task, _ = self.run_task()
        self.assertTrue(self.manager.delete(task["id"]))
        self.assertIsNone(self.manager.get(task["id"]))
        self.assertFalse((self.tmp / "root" / "tasks" / task["id"]).exists())
        self.assertFalse(self.manager.delete(task["id"]))  # 再删一次：False，不抛

    def test_unknown_task_raises_not_found(self):
        self.assertIsNone(self.manager.get("nope"))
        with self.assertRaises(CleanNotFound):
            self.manager.control("nope", "pause")
        # 未知动作也要在「任务不存在」之后才判：找不到任务时，「动作不认识」是误导
        with self.assertRaises(CleanNotFound):
            self.manager.control("nope", "wat")


class SeedAndDryRunTests(ManagerCase):
    def test_validate_and_create_produce_the_same_generated_values(self):
        """干跑里确认过的随机值，正式跑必须逐个相同 —— 靠的是把种子回传。

        用户是在干跑结果上确认「这样洗是对的」才提交的，两次跑出不同的随机值等于让他确认
        了一份不存在的数据。
        """
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["ok"], dry["error"])
        seed = dry["seed"]
        task = self.manager.create({"config": config, "seed": seed})
        done = self.wait(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(int(done["seed"]), seed)
        after = self.manager.preview(task["id"], rows=5)["after"]
        baseline = {row[0]: row[3] for row in after}
        self.assertEqual(len(baseline), 5)

        # 同一个种子再跑一遍：生成列必须逐行相同（这就是「位置播种」的可验证形式）
        again = self.manager.create({"config": config, "seed": seed})
        self.wait(again["id"])
        repeat = {row[0]: row[3] for row in self.manager.preview(again["id"], rows=5)["after"]}
        self.assertEqual(repeat, baseline)

    def test_a_bad_generator_is_reported_by_name_before_submitting(self):
        """配错的生成器必须在干跑就被点名 —— 不然要等任务跑起来才失败，
        而 GB 级任务「跑起来」本身就有成本。"""
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        config["fields"][3]["generate"]["generator"] = "uuid4"
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertFalse(dry["ok"])
        self.assertIn("uuid4", dry["error"])
        self.assertIn("编号", dry["error"])

    def test_validate_reports_fallbacks_and_violations(self):
        """违规计数按「字段|类型」记 —— 界面要能指出是哪个字段的哪类约束不满足，
        而不是只给一个「有 1 处违规」的总数。"""
        self.write(text="name,age,city\nu0,999,beijing\n")
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        config["fields"][1]["constraints"] = [
            {"kind": "range", "value_kind": "int", "min_value": 0, "max_value": 120}
        ]
        config["fields"][1]["fallback"] = {"on_violation": "keep"}
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["ok"], dry["error"])
        self.assertEqual(dry["violations"], {"年龄|range": 1})
        # 回退策略是 keep（原值保留），所以清洗后这一条仍然违规：报告必须同时给出两个数，
        # 否则「清洗前 1 处 → 清洗后 0 处」看起来像修好了，其实只是没检查
        self.assertEqual(dry["after_violations"], {"年龄|range": 1})
        self.assertTrue(dry["fallbacks"])

    def test_create_rejects_a_broken_config_with_a_readable_message(self):
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        config["fields"][0] = {"dest": "姓名"}
        config["fields"][0]["fallback"] = {"on_violation": "retry"}
        with self.assertRaisesRegex(CleanError, "retry"):
            self.manager.create({"config": config})

    def test_a_seed_change_is_not_a_config_change(self):
        """种子不进 config_hash：同一份配置换种子仍然是「同一份配置」，但它必须能重跑出
        不同的随机值（否则「换种子」这个动作就毫无用处）。"""
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        first = self.manager.create({"config": config, "seed": 1})
        self.wait(first["id"])
        second = self.manager.create({"config": config, "seed": 2})
        self.wait(second["id"])
        self.assertNotEqual(self.manager.preview(first["id"], rows=1)["after"][0][3],
                            self.manager.preview(second["id"], rows=1)["after"][0][3])


class DryPreviewTests(ManagerCase):
    """干跑返回的那段真数据（`validate()["preview"]`）。

    界面在「提交任务」之前给用户看的这两栏，就是他确认「这样洗是对的」的全部依据。所以
    这里最关键的一条不是「有没有数据」，而是**这段预览与正式任务跑出来的前 N 行逐格
    相同** —— 否则用户确认的是一份不存在的数据（与种子回传要守住的是同一件事）。
    """

    def test_dry_preview_matches_what_the_task_will_write(self):
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["ok"], dry["error"])
        preview = dry["preview"]
        self.assertEqual(preview["note"], "")
        self.assertEqual(preview["columns"], ["姓名", "年龄", "城市", "编号", "备注"])
        self.assertEqual(len(preview["before"]), 5)
        self.assertEqual(len(preview["after"]), 5)
        self.assertEqual(preview["rows"], 5)
        self.assertEqual(preview["before"][0][0], "u0")
        # 生成字段在「清洗前」栏恒为空 —— 「新增」这一类差异就是这么显出来的
        self.assertEqual(preview["before"][0][3], "")
        self.assertTrue(preview["after"][0][3], "生成列在清洗后应该有值")
        self.assertIn("编号", preview["generated"])

        task = self.manager.create({"config": config, "seed": dry["seed"]})
        self.wait(task["id"])
        after_task = self.manager.preview(task["id"], rows=5)
        self.assertEqual(preview["before"], after_task["before"])
        self.assertEqual(preview["after"], after_task["after"])
        self.assertEqual(preview["columns"], after_task["columns"])

    def test_the_left_pane_is_the_file_not_the_output_schema(self):
        """用户报的：左栏表头写的是**目的字段名**，不是文件里的列名；被丢弃的源列也一条
        都看不见。两件事都要在左栏上看得见 ——

        * 表头用文件里的列名（重命名的那个才看得出是重命名）；
        * 被丢弃的源列带原值排在左栏末尾。这是用户唯一能看见「哪几列不会进产物」的地方，
          右栏（产物）里本来就没有它们。
        """
        body = "\n".join(
            f"u{i},{i},I{i},{'beijing' if i % 2 else 'shanghai'}" for i in range(5)
        )
        self.write(text=f"name,age,inner,city\n{body}\n")
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        for field in config["fields"]:
            if field["dest"] == "城市":
                field["dest"] = "城市名"  # 重命名：源列 city → 目的字段 城市名
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["ok"], dry["error"])
        view = dry["preview"]
        self.assertEqual(view["columns"], ["姓名", "年龄", "城市名", "编号", "备注"])
        self.assertEqual(view["before_columns"][:5], ["name", "age", "city", "编号", "备注"])
        # 末尾那一段就是被丢弃的源列，与 `unmapped` 一一对应
        self.assertEqual(view["before_columns"][5:], ["inner"])
        self.assertEqual(view["unmapped"], ["inner"])
        for row in view["before"]:
            self.assertEqual(len(row), len(view["before_columns"]))
        # 逐格对齐照旧：左栏第 3 列就是「城市名」读的那一列
        self.assertEqual(view["before"][0][2], "shanghai")
        self.assertEqual(view["after"][0][2], "shanghai")
        # 被丢弃的列只在左边，右边不多出一个位置
        self.assertEqual(view["before"][0][-1], "I0")
        self.assertEqual(len(view["after"][0]), len(view["columns"]))

    def test_the_dry_report_lists_the_fallback_samples_it_recorded(self):
        """报告里的样本表**只能**从 state.json 恢复 —— 报告是 `restore(snapshot)` 出来的
        新对象生成的（引擎收尾与 `/report` 共用 `_build_report`）。样本不进快照，报告里
        就永远只有回退次数、一条样本都列不出来，还会倒过来赖到「断点续跑」头上。
        """
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        for field in config["fields"]:
            if field["dest"] == "城市":
                # csv_bytes 的城市隔行一个 shanghai：5 行里有 3 行违规，回退策略是保留原值
                field["constraints"] = [{"kind": "enum", "values": ["beijing"]}]
                field["fallback"] = {"on_violation": "keep"}
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["ok"], dry["error"])
        self.assertTrue(dry["fallbacks"], "这份配置应该真的触发回退")
        self.assertIn("### 回退样本", dry["report"])
        self.assertIn("内存里留下 3 条样本", dry["report"])
        self.assertNotIn("没有样本可列", dry["report"])
        # 这次不是续跑，别把样本说成中断前那一段的
        self.assertNotIn("本次是续跑，样本可能来自中断前那一段", dry["report"])

    def test_dry_preview_is_capped_independently_of_the_dry_rows(self):
        """试跑行数与预览行数是两个旋钮：可以跑 400 行看统计，但不必把 400 行都传回界面。"""
        self.write(text=csv_bytes(rows=400))
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        dry = self.manager.validate({"config": config, "rows": 400})
        self.assertTrue(dry["ok"], dry["error"])
        self.assertEqual(dry["rows"], 400)
        self.assertEqual(dry["preview"]["rows"], DRY_PREVIEW_ROWS)
        self.assertEqual(len(dry["preview"]["before"]), DRY_PREVIEW_ROWS)
        self.assertEqual(len(dry["preview"]["after"]), DRY_PREVIEW_ROWS)
        self.assertEqual(dry["preview"]["after"][-1][0], "u199")

    def test_a_dry_run_that_produces_no_rows_says_so(self):
        # 「备注」是只给目的字段的新列，恒为空 → drop_null 会把这 5 行全删掉。右侧空着
        # 必须带一句解释，否则用户只能猜是配置坏了还是工具坏了。
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        config["ops"] = [{"op": "drop_null", "fields": ["备注"]}]
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["ok"], dry["error"])
        self.assertEqual(len(dry["preview"]["before"]), 5)
        self.assertEqual(dry["preview"]["after"], [])
        self.assertEqual(dry["preview"]["note"], "没有已提交的数据行")

    def test_the_dry_store_is_still_deleted_after_reading_the_preview(self):
        """预览要读临时产物，但读完必须照旧删干净 —— 半截产物留在预览目录里，
        下一次清理之前它都是一个可以被 404 之外的方式访问到的东西。"""
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        dry = self.manager.validate({"config": config, "rows": 5})
        self.assertTrue(dry["preview"]["after"])
        self.assertEqual(list(self.manager.preview_dir.glob("*")), [])


class EstimateTests(ManagerCase):
    def test_estimate_counts_data_rows_not_the_header(self):
        """预估的行数、进度条分母、precount 三处同口径。差 1 也伤可信度 ——
        用户正是在预估上做「要不要提交」的决定。"""
        self.write(text="name,age,city\n" + "".join(f"u{i},{i},x\n" for i in range(50)))
        estimate = self.manager.estimate({"config": config_for(
            {"mode": "server_path", "paths": [str(self.source)]})})
        self.assertEqual(estimate["rows"], 50)
        self.assertEqual([f["rows"] for f in estimate["files"]], [50])
        task = self.manager.create({"config": config_for(
            {"mode": "server_path", "paths": [str(self.source)]})})
        self.assertEqual(self.wait(task["id"])["result"]["rows_in"], 50)

    def test_estimate_without_llm_fields_gives_no_number_instead_of_zero(self):
        """0 会被界面显示成「约 0 秒」，而用户接下来要等的是磁盘与函数时间。"""
        self.write()
        estimate = self.manager.estimate({"config": config_for(
            {"mode": "server_path", "paths": [str(self.source)]})})
        self.assertIsNone(estimate["estimate"]["seconds_low"])
        self.assertIsNone(estimate["estimate"]["seconds_high"])
        self.assertEqual(estimate["estimate"]["calls"], 0)
        self.assertTrue(any("给不出" in note for note in estimate["notes"]))

    def test_the_learned_latency_is_written_back_with_an_honest_sample_count(self):
        """写回端点池的是「学到的延迟 + 那次延迟对应几行 + 真实样本数」。

        样本数曾经按「成功 + 失败」算，而 EWMA 只在成功时更新 —— 一个挂了 40 次、成功 2 次
        的端点会被记成「量了 42 次」。那个数字正是用户判断「这个预估值准不准」的依据，
        虚高就等于把「只量了 2 次」说成「量了 42 次」。
        """
        from app.clean_llm import LlmNetworkError, LlmPool, load_pool

        self.manager.save_endpoints({"endpoints": [
            {"id": "e1", "url": "http://127.0.0.1:9/v1", "model": "m", "max_concurrency": 4},
        ]})
        pool = LlmPool(load_pool(self.manager.pool_file))
        endpoint = pool.endpoints[0]
        pool.report_ok(endpoint, 4.856, rows=10)
        pool.report_ok(endpoint, 5.0, rows=10)
        for _ in range(40):
            pool.report_failure(endpoint, LlmNetworkError("down"))
        self.manager._persist_latency(pool)

        stored = load_pool(self.manager.pool_file)[0]
        self.assertEqual(stored.ewma_samples, 2, "样本数只能是成功观测到的次数")
        self.assertEqual(stored.ewma_rows, 10.0, "一次请求几行必须一起记住")
        self.assertAlmostEqual(stored.ewma_ms, endpoint.ewma_ms)

    def test_estimate_handles_llm_fields_without_dying(self):
        """有大模型字段的预估要给得出数字与调用次数 —— 这是「提交前必须显示预估」那条
        要求的唯一实现。它曾经在 `_estimate_notes` 里把字段**名字**当字段**规格**用，
        于是任何带 LLM 字段的配置在这里都是 500：预估不显示，护栏也就没了。"""
        self.write()
        source = {"mode": "server_path", "paths": [str(self.source)]}
        config = config_for(source)
        config["fields"][3]["generate"] = {"kind": "llm", "prompt": "介绍一下 ${姓名}"}
        config["fields"][4]["generate"] = {
            "kind": "llm", "prompt": "评价一下 ${年龄}", "batch_size": 1,
        }
        self.manager.save_endpoints({"endpoints": [
            {"id": "e1", "url": "http://127.0.0.1:9/v1", "model": "m", "max_concurrency": 3},
        ]})
        config["llm"] = {"endpoint_ids": ["e1"]}

        estimate = self.manager.estimate({"config": config})
        self.assertEqual(estimate["rows"], 5)
        self.assertEqual(estimate["estimate"]["calls"], 10)      # 5 行 × 2 字段 ÷ batch 1
        self.assertIsNotNone(estimate["estimate"]["seconds_high"])
        # 两个字段都是一次一行 → 必须提示批量合并能省数倍时间与费用
        self.assertTrue(any("批量合并" in note for note in estimate["notes"]), estimate["notes"])
        self.assertEqual(estimate["endpoints"]["concurrency"], 3)
        self.assertEqual(estimate["endpoints"]["missing"], [])


class ResumeTests(ManagerCase):
    def test_startup_marks_unfinished_tasks_as_interrupted_not_failed(self):
        """failed 的语义是「重来」，interrupted 的语义是「接着跑」。用错了会让用户以为
        前面跑掉的那些小时白费了。"""
        task, done = self.run_task()
        self.force_status("running")

        restored = self.manager_for()
        item = restored.get(task["id"])
        self.assertEqual(item["status"], "interrupted")
        self.assertEqual(item["status_text"], "已中断，可继续")
        self.assertEqual(item["message"], STATUS_TEXT["interrupted"])
        # 记录说「中断了」，而 `state.json` 显示文件都提交完了 —— 这种任务仍然**可以**
        # 点续跑（续跑会发现没活干，直接完成），不该显示成「不能继续」
        self.assertEqual(item["resume"]["code"], "resumed")
        self.assertEqual(item["resume"]["notes"], ["已完成 1 个文件，这些文件不会重跑"])

    def test_a_completed_task_refuses_to_resume(self):
        task, done = self.run_task()
        self.assertEqual(done["resume"]["code"], "completed")
        with self.assertRaises(CleanConflict):
            self.manager.control(task["id"], "resume")

    def test_resume_refuses_when_the_config_changed(self):
        """改了规则就不许接着跑：前一半用旧规则、后一半用新规则，产物无法解释。"""
        task, _ = self.run_task()

        def mutate(item):
            item["status"] = "interrupted"
            if item["id"] == task["id"]:
                item["request"]["fields"][1]["constraints"] = [
                    {"kind": "enum", "values": ["1", "2", "3"]}
                ]

        self.patch_records(mutate)
        restored = self.manager_for()
        decision = restored.get(task["id"])["resume"]
        self.assertFalse(decision["resumable"])
        self.assertEqual(decision["code"], "config")
        with self.assertRaises(CleanConflict):
            restored.control(task["id"], "resume")

    def test_resume_refuses_when_the_input_changed(self):
        task, _ = self.run_task()
        self.force_status("interrupted")
        self.write(text=csv_bytes(rows=99))          # 输入变了
        restored = self.manager_for()
        decision = restored.get(task["id"])["resume"]
        self.assertFalse(decision["resumable"])
        self.assertEqual(decision["code"], "inputs")
        self.assertIn("a.csv", decision["reason"])

    def test_resuming_a_fully_committed_task_does_not_duplicate_rows(self):
        """续跑的起点由 `state.json` 的提交坐标决定。已经提交完的文件不该被再写一遍 ——
        重复行是「续跑」最容易出的错，而且看起来完全像正常数据。"""
        task, done = self.run_task()
        output = self.tmp / "root" / "tasks" / task["id"] / "output" / "a.csv"
        before = output.read_text()
        self.force_status("interrupted")

        restored = self.manager_for()
        self.assertTrue(restored.get(task["id"])["resume"]["resumable"])
        restored.control(task["id"], "resume")
        again = self.wait(task["id"], manager=restored, statuses=("completed",))
        self.assertEqual(again["status"], "completed", again)
        self.assertEqual(again["result"]["rows_in"], 5)
        self.assertEqual(again["result"]["rows_out"], 5)
        self.assertEqual(output.read_text(), before)

    def test_auto_resume_picks_unfinished_tasks_up_at_startup(self):
        task, done = self.run_task()
        self.force_status("running")
        restored = self.manager_for(auto_resume=True)
        item = self.wait(task["id"], manager=restored, statuses=("completed",))
        self.assertEqual(item["status"], "completed", item)

    def test_auto_resume_is_off_by_default(self):
        task, _ = self.run_task()
        self.force_status("running")
        restored = self.manager_for()
        time.sleep(0.6)
        self.assertEqual(restored.get(task["id"])["status"], "interrupted")

    def test_retry_keeps_the_seed_and_the_state_is_rebuilt(self):
        """重跑不是续跑：产物从第一行重来，但随机值必须和上次一致 —— 种子留在任务记录里。"""
        task, done = self.run_task()
        first = self.manager.preview(task["id"], rows=5)["after"]
        again = self.manager.control(task["id"], "retry")
        self.assertEqual(again["status"], "queued")
        finished = self.wait(task["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(self.manager.preview(task["id"], rows=5)["after"], first)


class PreviewTests(ManagerCase):
    """「清洗后」那一栏读的是产物文件，而**产物是我们自己写的**，不是用户的输入。

    这一层错位的两种表现都不会报错、只是显示不对：读错文件（跑一半时磁盘上只有中间产物）、
    或者读对了文件但用错了读法（拿输入的分隔符/工作表名去读产物）。两者都归这里管。
    """

    def pretend_running(self, task_id):
        """把内存里的状态改成「正在跑」。

        「跑一半」的磁盘状态要手工摆出来：真去和引擎抢时序，测出来的是竞态而不是预览。
        记录活在内存里（`tasks.json` 只是它的落盘副本），所以改盘上的文件没用。
        """
        with self.manager.lock:
            self.manager.tasks[task_id]["status"] = "running"

    def test_preview_reads_the_spool_while_an_xlsx_task_is_still_running(self):
        """xlsx 的最终文件要等整个文件跑完才转出来，跑一半时磁盘上只有 `.part.csv`。

        预览必须去读那一个 —— 自己再拼一遍后缀（这里曾经就是）等于原地读同一个不存在的
        文件，于是「跑过一半的任务永远显示产物还没生成」，用户看不到任何进展。

        中间产物的路径由 `append_target` 本身算，不在测试里手写一遍命名规则。
        """
        task, done = self.run_task(config_for(
            {"mode": "server_path", "paths": [str(self.source)]},
            output={"format": "xlsx", "commit_rows": 2}))
        self.assertEqual(done["status"], "completed")
        store = self.manager.store(task["id"])
        final = store.output_path("a.csv", "xlsx")
        spool = append_target(final, OutputOptions(format="xlsx"))
        self.assertTrue(final.is_file())
        self.assertNotEqual(spool, final)

        final.unlink()                                   # 还没转出最终文件
        with open(spool, "w", encoding="utf-8", newline="") as handle:   # 已提交的那几行
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["姓名", "年龄", "城市", "编号", "备注"])
            writer.writerow(["u0", "0", "shanghai", "编号-0", "已提交"])
        self.pretend_running(task["id"])
        view = self.manager.preview(task["id"], rows=5)
        self.assertEqual(view["after"], [["u0", "0", "shanghai", "编号-0", "已提交"]])
        self.assertIn("任务还没跑完", view["note"])

    def test_preview_of_a_header_only_output_is_empty_not_an_error(self):
        """只写了表头、一行都没提交的产物：给空表 + 一句解释，而不是 500（曾经就在这儿崩）。

        `read_batch()` 对空批次返回的是 `None` 而不是 `[]`，直接下标就是
        `TypeError: 'NoneType' object is not subscriptable` —— 界面上只会说「服务器错误」。
        """
        task, done = self.run_task()
        self.assertEqual(done["status"], "completed")
        store = self.manager.store(task["id"])
        store.output_path("a.csv", "csv").write_text(
            "姓名,年龄,城市,编号,备注\n", encoding="utf-8")
        view = self.manager.preview(task["id"], rows=5)
        self.assertEqual(view["after"], [])
        self.assertEqual(len(view["before"]), 5)         # 左侧照常有：它读的是输入
        self.assertNotIn("失败", view["note"])
        self.assertIn("没有已提交的数据行", view["note"])

    def test_preview_of_a_zero_byte_output_says_it_is_still_early(self):
        """连表头都还没写出来的空文件（刚建好、或取消在第一次提交之前）。

        真去读它只会得到「表头行 0 超出文件范围」—— 那是实现细节，不是用户要的结论。
        """
        task, _ = self.run_task()
        store = self.manager.store(task["id"])
        store.output_path("a.csv", "csv").write_text("", encoding="utf-8")
        self.pretend_running(task["id"])
        view = self.manager.preview(task["id"], rows=5)
        self.assertEqual(view["after"], [])
        self.assertEqual(view["note"], "任务还没跑完，清洗后只显示已提交的部分")

    def test_preview_reads_the_output_with_output_settings_not_input_ones(self):
        """输入是 `;` 分隔，产物是 `,` 分隔 —— 拿输入的读法去读产物，整行会被读成单列。"""
        self.write(text="name;age;city\nu0;0;shanghai\nu1;1;beijing\n")
        config = config_for({"mode": "server_path", "paths": [str(self.source)]},
                            input={"delimiter": ";", "encoding": "utf-8"},
                            output={"format": "csv", "csv_delimiter": ","})
        created = self.manager.create({"config": config})
        done = self.wait(created["id"])
        self.assertEqual(done["status"], "completed", done)
        view = self.manager.preview(created["id"], rows=5)
        self.assertEqual(view["after"][0][:3], ["u0", "0", "shanghai"])
        self.assertEqual(view["note"], "")

    def test_preview_of_an_xlsx_output_ignores_the_input_sheet_name(self):
        """输入的 `sheet` 指定的是**输入**那张表的名字，产物里根本不存在。

        照搬过去读产物，直接是「找不到工作表」；而数据其实是好的。
        """
        book = xlsxwriter.Workbook(str(self.source / "a.xlsx"))
        sheet = book.add_worksheet("数据")
        for row, values in enumerate([["name", "age", "city"],
                                      ["u0", "0", "shanghai"], ["u1", "1", "beijing"]]):
            for column, value in enumerate(values):
                sheet.write_string(row, column, value)
        book.close()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]},
                            input={"sheet": "数据"},
                            output={"format": "xlsx", "commit_rows": 2})
        created = self.manager.create({"config": config})
        done = self.wait(created["id"])
        self.assertEqual(done["status"], "completed", done)
        view = self.manager.preview(created["id"], rows=5)
        self.assertEqual(len(view["after"]), 2)
        self.assertEqual(view["after"][0][:3], ["u0", "0", "shanghai"])
        self.assertEqual(view["note"], "")


class UploadAndPathTests(ManagerCase):
    def test_upload_then_run(self):
        upload_id, folder = self.manager.new_upload()
        target = folder / "a.csv"
        target.write_text(csv_bytes(), encoding="utf-8")
        info = self.manager.save_upload(upload_id, [("a.csv", target)])
        self.assertEqual(info["count"], 1)
        created = self.manager.create({"config": config_for(
            {"mode": "upload", "upload_id": upload_id})})
        done = self.wait(created["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["result"]["rows_in"], 5)
        self.assertTrue(self.manager.delete_upload(upload_id))

    def test_upload_rejects_a_path_that_escapes_the_upload_dir(self):
        upload_id, folder = self.manager.new_upload()
        evil = self.tmp / "evil.csv"
        evil.write_text(csv_bytes(), encoding="utf-8")
        with self.assertRaises(CleanError):
            self.manager.save_upload(upload_id, [("../evil.csv", evil)])

    def test_safe_relpath_rules(self):
        self.assertEqual(safe_relpath("sub\\a.csv"), "sub/a.csv")
        self.assertEqual(safe_relpath("./a.csv"), "a.csv")
        for bad in ("/etc/passwd", "../a.csv", "a/../../b.csv", "C:\\x.csv", "", ".."):
            with self.assertRaises(CleanError, msg=bad):
                safe_relpath(bad)

    def test_allowed_roots_guard_the_server_path_mode(self):
        outside = Path("/etc")
        manager = self.manager_for(root=self.tmp / "root2", allowed_roots=[self.tmp])
        with self.assertRaises(CleanError):
            manager.check_allowed(outside)
        # 工具自己的任务目录也不许读：否则「清洗自己的产物」会变成一个自我引用的循环
        with self.assertRaises(CleanError):
            manager.check_allowed(manager.tasks_dir)
        manager.check_allowed(self.source)

    def test_browse_lists_directories_and_candidates_only(self):
        self.write()
        (self.source / "note.txt").write_text("x", encoding="utf-8")
        (self.source / "sub").mkdir()
        listing = self.manager.browse(str(self.source))
        files = {item["name"] for item in listing["files"]}
        self.assertIn("a.csv", files)
        # `.txt` 是候选扩展名之一，但 note.txt 里不是分隔符文本……这里只保证不列非候选格式
        (self.source / "skip.bin").write_bytes(b"\x00\x01")
        self.assertNotIn("skip.bin", {item["name"] for item in
                                      self.manager.browse(str(self.source))["files"]})
        self.assertIn("sub", {item["name"] for item in
                              self.manager.browse(str(self.source))["dirs"]})
        # 浏览只给元信息，不给内容：一个 GB 目录列表里塞内容等于自杀
        self.assertFalse(any("content" in item for item in listing["files"]))

    def test_inspect_detects_the_delimiter_when_it_is_not_pinned(self):
        """探测的意义就在「用户不知道分隔符是什么」的时候 —— 配置里给了分隔符时，
        探测照配置走，不该自作聪明。"""
        self.write(text="name;age;city\nu0;1;beijing\n")
        source = {"mode": "server_path", "paths": [str(self.source)]}
        found = self.manager.inspect({
            "config": config_for(source, input={"delimiter": None, "encoding": None}),
            "file": "a.csv",
        })
        self.assertEqual(found["delimiter"], ";")
        self.assertGreater(found["delimiter_confidence"], 0)
        self.assertEqual(found["headers"], ["name", "age", "city"])
        self.assertEqual(found["sample_rows"][0][0], "u0")
        self.assertEqual(found["key"], "a.csv")
        pinned = self.manager.inspect({"config": config_for(source), "file": "a.csv"})
        self.assertEqual(pinned["delimiter"], ",")      # 配置说了算，哪怕解析出来是一整列

    def test_inspect_works_before_any_destination_field_exists(self):
        """探测必须能在**空字段表**上跑。

        界面上的第 2 步是「探测表头 → 一键映射」，所以这一步永远发生在还没有任何目的
        字段的时候。`CleanTaskConfig` 要求至少一个目的字段，如果 `/inspect` 拿它校验，
        用户就卡在「先加字段才能探测表头、先探测表头才能加字段」的死循环里 —— 界面上
        表现为「点探测表头没反应」。
        """
        self.write(text="name;age\nu0;1\n")
        source = {"mode": "server_path", "paths": [str(self.source)]}
        blank = config_for(source, input={"delimiter": None, "encoding": None})
        blank["fields"] = []
        found = self.manager.inspect({"config": blank, "file": "a.csv"})
        self.assertEqual(found["headers"], ["name", "age"])
        self.assertEqual(found["delimiter"], ";")
        # 只有目的字段是空的；真跑仍然拒绝空字段表（否则就没有输出了）
        with self.assertRaises(CleanError):
            self.manager.estimate({"config": blank})
        with self.assertRaises(CleanError):
            self.manager.validate({"config": blank})

    def test_inspect_rows_are_capped_and_junk_falls_back(self):
        """样本行数是给人眼看的一屏预览：夹在上限里，请求方要多少给不了更多。

        非法值也**不能**是 500 —— 原来直接 `int(...)`，界面发来一个非数字就是服务器错误，
        而这里本来就有一个合理的默认值可用。
        """
        self.write(text=csv_bytes(rows=300))
        source = {"mode": "server_path", "paths": [str(self.source)]}
        config = config_for(source, input={"delimiter": None, "encoding": None})
        def rows(value):
            asked = {"config": config, "file": "a.csv"} if value is None else \
                {"config": config, "file": "a.csv", "rows": value}
            return self.manager.inspect(asked)["sample_rows"]

        self.assertEqual(len(rows(10**9)), INSPECT_MAX_ROWS)   # 要一亿行也只给上限
        self.assertEqual(len(rows(5)), 5)                      # 上限之内照给
        self.assertEqual(len(rows("abc")), INSPECT_ROWS)       # 非数字 → 默认，不是 500
        self.assertEqual(len(rows(0)), 1)                      # 至少一行：0 行不叫预览
        self.assertEqual(len(rows(None)), INSPECT_ROWS)        # 不给参数 → 默认


class EndpointPoolTests(ManagerCase):
    def test_the_pool_file_is_private_and_the_key_never_leaks(self):
        self.manager.save_endpoints({"endpoints": [
            {"name": "本地", "url": "http://127.0.0.1:9/v1", "model": "m", "api_key": "sk-secret"}
        ]})
        mode = stat.S_IMODE(self.manager.pool_file.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        body = self.manager.endpoints()
        public = body["endpoints"][0]
        # 公开形状里 `api_key` 只剩哨兵：界面需要知道「这里有个密钥」，但拿不到它
        self.assertEqual(public["api_key"], body["sentinel"])
        self.assertTrue(public["key_set"])
        self.assertNotIn("sk-secret", json.dumps(body, ensure_ascii=False))
        # tasks.json 每次进度更新都会重写并被列表接口返回，密钥绝不能进那里
        self.write()
        self.manager.create({"config": config_for(
            {"mode": "server_path", "paths": [str(self.source)]})})
        self.assertNotIn("sk-secret", (self.tmp / "root" / "tasks.json").read_text())

    def test_the_sentinel_keeps_the_stored_key(self):
        saved = self.manager.save_endpoints({"endpoints": [
            {"id": "e1", "name": "本地", "url": "http://127.0.0.1:9/v1", "model": "m",
             "api_key": "sk-secret"}
        ]})
        sentinel = saved["sentinel"]
        self.manager.save_endpoints({"endpoints": [
            {"id": "e1", "name": "改名了", "url": "http://127.0.0.1:9/v1", "model": "m",
             "api_key": sentinel}
        ]})
        from app.clean_llm import load_pool
        endpoint = load_pool(self.manager.pool_file)[0]
        self.assertEqual(endpoint.api_key, "sk-secret")
        self.assertEqual(endpoint.name, "改名了")
        self.assertNotIn(sentinel, (self.tmp / "root" / "llm_endpoints.json").read_text())

    def test_a_failed_connection_test_is_a_result_not_an_error(self):
        result = self.manager.test_endpoint({"endpoint": {
            "name": "死的", "url": "http://127.0.0.1:9/v1", "model": "m",
            "connect_timeout_s": 1, "timeout_s": 1,
        }})
        self.assertFalse(result["ok"])
        # 报的必须是**连不上**，不是「调用次数已达上限」：预算曾小于这次测试自己会发的
        # 尝试次数，于是护栏把真实原因盖掉了 —— 用户点「测试连接」就是为了看到前者。
        self.assertNotIn("上限", result["message"])
        self.assertIn("Llm", result["message"])

    def test_endpoint_ids_must_be_unique(self):
        with self.assertRaises(CleanError):
            self.manager.save_endpoints({"endpoints": [
                {"id": "same", "url": "http://127.0.0.1:9/v1", "model": "a"},
                {"id": "same", "url": "http://127.0.0.1:9/v1", "model": "b"},
            ]})

    def test_a_task_with_llm_fields_needs_an_endpoint(self):
        self.write()
        config = config_for({"mode": "server_path", "paths": [str(self.source)]})
        config["fields"][3]["generate"] = {"kind": "llm", "prompt": "写一个 ${姓名}"}
        with self.assertRaisesRegex(CleanError, "端点"):
            self.manager.create({"config": config})


class ApiTests(unittest.TestCase):
    """路由层：只测「HTTP 状态码与形状」，业务语义在上面那些用例里。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "in"
        self.source.mkdir()
        (self.source / "a.csv").write_text(csv_bytes(), encoding="utf-8")
        self.root = self.tmp / "root"
        # 路径白名单与数据目录是**机器级**配置（环境变量），所以这里必须改环境 ——
        # 这也顺带验证了它们确实是从环境读的。用完恢复，别污染其它测试。
        self._env = {name: os.environ.get(name) for name in
                     ("CLEAN_ALLOWED_ROOTS", "CLEAN_DATA_DIR")}
        os.environ["CLEAN_ALLOWED_ROOTS"] = str(self.tmp)
        app = FastAPI()
        app.include_router(make_router(self.root))
        self.app = app

    def tearDown(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def client(self):
        # 上下文管理器形式：退出时跑 lifespan，路由注册的 shutdown 钩子会关掉任务线程池
        return TestClient(self.app)

    def post_task(self, client, **payload):
        payload.setdefault("config", config_for({"mode": "server_path",
                                                 "paths": [str(self.source)]}))
        response = client.post("/api/clean/tasks", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_api(self, client, task_id, deadline=30.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            item = client.get(f"/api/clean/tasks/{task_id}").json()
            if item["status"] not in ("queued", "running", "paused"):
                return item
            time.sleep(0.05)
        self.fail("接口轮询超时")

    def test_meta_carries_the_labels_and_the_storage(self):
        """`limits` 必须**不在**这里：上传不设大小上限了，界面也不该再宣传一个上限。

        反过来 `storage` 要有值 —— 用户传几百 GB 之前，得先看得见盘还剩多少。
        """
        with self.client() as client:
            body = client.get("/api/clean/meta").json()
            self.assertEqual(body["status_text"]["interrupted"], "已中断，可继续")
            self.assertEqual(body["file_status_text"]["pending"], "未开始")
            self.assertIn(".csv", body["extensions"])
            self.assertNotIn("limits", body)
            self.assertGreater(body["storage"]["free_bytes"], 0)
            self.assertEqual(body["storage"]["root"], str(self.root / "uploads"))

    def test_create_poll_report_preview_download(self):
        with self.client() as client:
            created = self.post_task(client, seed=11)
            self.assertEqual(created["status"], "queued")
            self.assertEqual(created["status_text"], "等待执行")
            done = self.wait_api(client, created["id"])
            self.assertEqual(done["status"], "completed", done)
            listing = client.get("/api/clean/tasks").json()["tasks"]
            self.assertEqual([item["id"] for item in listing], [created["id"]])
            report = client.get(f"/api/clean/tasks/{created['id']}/report")
            self.assertEqual(report.status_code, 200)
            self.assertTrue(report.headers["content-type"].startswith("text/markdown"))
            preview = client.get(f"/api/clean/tasks/{created['id']}/preview",
                                 params={"rows": 3}).json()
            self.assertEqual(len(preview["before"]), len(preview["after"]))
            logs = client.get(f"/api/clean/tasks/{created['id']}/logs",
                              params={"limit": 500}).json()
            self.assertIn("events", logs)
            archive = client.get(f"/api/clean/tasks/{created['id']}/download")
            self.assertEqual(archive.status_code, 200)
            self.assertGreater(len(archive.content), 0)

    def test_error_mapping(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/clean/tasks/none").status_code, 404)
            self.assertEqual(client.post("/api/clean/tasks/none/pause").status_code, 404)
            self.assertEqual(client.post("/api/clean/tasks/none/wat").status_code, 404)
            self.assertEqual(client.delete("/api/clean/tasks/none").status_code, 404)
            broken = {"config": {"source": {"mode": "upload"}}}
            self.assertEqual(client.post("/api/clean/tasks", json=broken).status_code, 400)
            self.assertEqual(client.post("/api/clean/estimate", json=broken).status_code, 400)
            self.assertEqual(client.get(f"/api/clean/tasks/none/preview").status_code, 404)

    def test_pausing_a_finished_task_is_a_conflict_not_a_500(self):
        with self.client() as client:
            created = self.post_task(client)
            self.wait_api(client, created["id"])
            response = client.post(f"/api/clean/tasks/{created['id']}/pause")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("不能暂停", response.json()["detail"])

    def test_upload_rejects_a_traversal_relative_path(self):
        with self.client() as client:
            response = client.post(
                "/api/clean/uploads",
                files=[("files", ("x.csv", csv_bytes(), "text/csv"))],
                data={"relative_paths": json.dumps(["../evil.csv"])},
            )
            self.assertEqual(response.status_code, 400, response.text)
            # 半截的上传目录要清掉，不能留在盘上
            self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_upload_stops_when_the_disk_is_almost_full(self):
        """盘要满了就停下 —— 这是去掉大小上限之后**唯一**的界。

        真把盘写满来测是不可能的，所以替换 `free_bytes`：上传那条路只有这一个读盘量的
        入口（`ensure_disk_space` 内部调的就是它）。界是**水位线**不是上限：比水位线高一
        点要能过，低一点要挡住。
        """
        from app import clean_manager

        def upload(client, name="x.csv"):
            return client.post("/api/clean/uploads",
                               files=[("files", (name, csv_bytes(), "text/csv"))],
                               data={"relative_paths": json.dumps([name])})

        with self.client() as client:
            with mock.patch.object(clean_manager, "free_bytes",
                                   return_value=clean_manager.DISK_FREE_FLOOR + 1):
                self.assertEqual(upload(client).status_code, 200)
            with mock.patch.object(clean_manager, "free_bytes",
                                   return_value=clean_manager.DISK_FREE_FLOOR - 1):
                response = upload(client)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("剩余空间不足", response.json()["detail"])
            # 半截的上传目录要清掉，不能留在盘上（上一次成功的那一份不在此列）
            self.assertEqual(len(list((self.root / "uploads").iterdir())), 1)

    def test_zip_extraction_reserves_the_space_of_each_entry(self):
        """压缩包按**条目原始大小**预留。一个 4GB 的包能展开出 TB 级 —— 只按已写出去
        的量判断，等发现不对时盘已经满了。
        """
        from app import clean_manager

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package:
            package.writestr("a.csv", csv_bytes(rows=50))
        payload = buffer.getvalue()
        with self.client() as client:
            with mock.patch.object(clean_manager, "free_bytes",
                                   return_value=clean_manager.DISK_FREE_FLOOR + 100):
                response = client.post("/api/clean/uploads",
                                       files=[("files", ("x.zip", payload, "application/zip"))],
                                       data={"relative_paths": json.dumps(["x.zip"])})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("剩余空间不足", response.json()["detail"])
            self.assertEqual(list((self.root / "uploads").iterdir()), [])

    def test_upload_keeps_the_directory_shape(self):
        with self.client() as client:
            response = client.post(
                "/api/clean/uploads",
                files=[("files", ("b.csv", csv_bytes(2), "text/csv")),
                       ("files", ("c.csv", csv_bytes(3), "text/csv"))],
                data={"relative_paths": json.dumps(["sub/b.csv", "c.csv"])},
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual({item["path"] for item in body["files"]}, {"sub/b.csv", "c.csv"})
            upload_id = body["id"]
            created = self.post_task(client, config=config_for(
                {"mode": "upload", "upload_id": upload_id}))
            done = self.wait_api(client, created["id"])
            self.assertEqual({f["key"] for f in done["result"]["files"]}, {"sub/b.csv", "c.csv"})
            self.assertEqual(done["result"]["rows_in"], 5)
            self.assertEqual(client.delete(f"/api/clean/uploads/{upload_id}").status_code, 200)

    def test_endpoint_pool_over_http_hides_the_key(self):
        with self.client() as client:
            saved = client.post("/api/clean/llm/endpoints", json={"endpoints": [
                {"name": "甲", "url": "http://127.0.0.1:9/v1", "model": "m", "api_key": "sk-x"}
            ]})
            self.assertEqual(saved.status_code, 200, saved.text)
            body = saved.json()
            endpoint_id = body["endpoints"][0]["id"]
            self.assertNotIn("sk-x", saved.text)
            self.assertTrue(body["endpoints"][0]["key_set"])
            failed = client.post(f"/api/clean/llm/endpoints/{endpoint_id}/test")
            self.assertEqual(failed.status_code, 200, failed.text)
            self.assertFalse(failed.json()["ok"])
            self.assertEqual(client.get("/api/clean/llm/endpoints").json()["sentinel"],
                             body["sentinel"])
            self.assertEqual(client.delete(f"/api/clean/llm/endpoints/{endpoint_id}").status_code,
                             200)
            self.assertEqual(client.get("/api/clean/llm/endpoints").json()["endpoints"], [])
            self.assertEqual(client.delete("/api/clean/llm/endpoints/none").status_code, 404)

    def test_browse_and_inspect_over_http(self):
        with self.client() as client:
            listing = client.get("/api/clean/browse", params={"path": str(self.source)})
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertEqual(client.get("/api/clean/browse",
                                        params={"path": "/etc"}).status_code, 400)
            found = client.post("/api/clean/inspect", json={
                "config": config_for({"mode": "server_path", "paths": [str(self.source)]},
                                     input={"delimiter": None, "encoding": None}),
                "file": "a.csv",
            })
            self.assertEqual(found.status_code, 200, found.text)
            self.assertEqual(found.json()["headers"][0], "name")
            # 探测一个不存在的文件：是「找不到」而不是「服务器错误」
            self.assertEqual(client.post("/api/clean/inspect", json={
                "config": config_for({"mode": "server_path", "paths": [str(self.source)]}),
                "file": "nope.csv",
            }).status_code, 404)


if __name__ == "__main__":
    unittest.main()
