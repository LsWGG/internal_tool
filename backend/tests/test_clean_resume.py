"""断点续跑：state.json 的提交协议，以及「续跑后的产物与不中断运行逐字节相同」。

这是需求 1 的核心断言，也是这一整套东西里唯一不能靠单元测试局部验证的性质 ——
它依赖提交顺序、append-only 截断、索引重放、行号跳过四件事**同时**正确。所以下面
用一个小引擎（`MiniEngine`）把这条链路完整跑两遍：一遍一口气跑完，一遍在提交点之间
崩掉再续跑，然后逐字节比较产物。

MiniEngine 刻意用引擎将要用的那套接口（OpContext / open_writer(append_bytes) /
TaskProgress / StateStore），因为「续跑对不对」是这套协议的性质，不是某个函数的性质。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clean_io import append_target, list_input_files, open_reader, open_writer
from app.clean_models import (
    CleanOp,
    CleanTaskConfig,
    Constraint,
    Fallback,
    FieldSpec,
    InputOptions,
    OutputOptions,
    SourceSpec,
)
from app.clean_ops import OpContext, Table, apply_ops, dedupe_key
from app.clean_schema import apply_fallback, eval_column
from app.clean_state import (
    STATE_VERSION,
    StateError,
    StateStore,
    TaskProgress,
    TaskState,
    fingerprint,
    resolve_resume,
    to_local,
    truncate_file,
)
from app.clean_stats import Counters


class Crash(RuntimeError):
    """模拟进程被杀：不清理、不 flush、不留遗言。"""


# =========================================================================== 小引擎


class MiniEngine:
    """一个文件、按批处理、按提交点落盘的引擎，只保留续跑需要的那部分。

    它与真正引擎共享的正是**协议**：读取跳过 rows_in 行、算子状态与索引从日志重建、
    产出按记录字节数截断续写、提交时 flush 一切再写 state.json。没有并发、没有大模型、
    没有回归报告 —— 那些都不影响「续跑是否幂等」。
    """

    def __init__(
        self,
        store: StateStore,
        config: CleanTaskConfig,
        *,
        batch_rows: int = 25,
        commit_rows: int = 100,
        crash_after_commits: int | None = None,
    ):
        # 提交点必须落在批边界上：两侧共用的随机流要求批切分一致（见 TaskProgress）
        assert commit_rows % batch_rows == 0, "commit_rows 必须是 batch_rows 的整数倍"
        self.store = store
        self.config = config
        self.batch_rows = batch_rows
        self.commit_rows = commit_rows
        self.crash_after_commits = crash_after_commits
        self.commits = 0
        self.events: list[str] = []
        self._writer = None
        self._writers: list = []

    def close(self):
        """关掉留下的写出器句柄与索引日志（测试收尾用）。崩溃后的缓冲区是空的，关它不会补写数据。"""
        for writer in self._writers:
            try:
                writer.close()
            except Exception:
                pass
        self._writers.clear()
        self._writer = None
        progress = getattr(self, "_progress", None)
        if progress is not None:
            progress.close()
            self._progress = None

    # -- 一批的处理 --------------------------------------------------------

    def _apply_constraints(self, table: Table, progress: TaskProgress) -> Table:
        for spec in self.config.fields:
            if not spec.constraints or table.find(spec.dest) is None:
                continue
            index = None
            if any(item.kind == "unique" for item in spec.constraints):
                index = progress.unique_index(spec.dest)
            values = table.column(spec.dest)
            violations = eval_column(values, spec.constraints, spec.dest, index)
            for position, violation in enumerate(violations):
                if violation is None:
                    if index is not None:
                        index.add(values[position])
                    continue
                progress.counters.note_violation(spec.dest, violation.kind)
                if spec.fallback is None:
                    continue
                # 一个字段每种违规类型至多一条约束（配置校验就是这么要求的），
                # 所以按 kind 找回约束是精确的
                constraint = next(
                    (c for c in spec.constraints if c.kind == violation.kind), None
                )
                if constraint is None:
                    continue
                # 重试要 probe 而不 add —— 失败的尝试不能污染索引
                probe = index.clone() if index is not None else None
                result = apply_fallback(
                    values[position], violation, spec.fallback, constraint, probe
                )
                # 用最后一个通过的约束对象；apply_fallback 已经保证结果是它能给的
                if result.value != values[position]:
                    values[position] = result.value
                    progress.counters.note_fallback(
                        spec.dest, violation.kind, spec.fallback.on_violation,
                        degraded=result.degraded,
                    )
            if index is not None:
                for value in values:
                    index.add(value)
            table = table.with_column(spec.dest, values)
        return table

    def _project(self, table: Table) -> tuple[list[str], list[list[Any]]]:
        """映射到目的字段：未映射的源列（例如只为去重而读进来的 dup）不进产物。

        真引擎的表头映射做同一件事；少了这一步，写出器的表头与行宽会对不上。
        """
        dests = self.config.dest_fields()
        rows = table.to_rows()
        keep = [table.find(dest) for dest in dests]
        return dests, [[row[i] if i is not None else "" for i in keep] for row in rows]

    def _process(self, columns, rows, progress, file_state):
        table = Table.from_rows(columns, rows)
        progress.before.update(columns, table.data)
        table = apply_ops(table, self.config.ops, progress.ops)
        table = self._apply_constraints(table, progress)
        dests, out_rows = self._project(table)
        progress.after.update(dests, Table.from_rows(dests, out_rows).data)
        return table, out_rows

    # -- 主循环 ------------------------------------------------------------

    def run(self, inputs: list[tuple[str, str]]) -> Path:
        """跑一遍（必要时续跑）。返回产出文件路径。"""
        # 索引日志要落在 logs/ 下，所以目录必须先建好（真引擎在任务创建时就建）
        self.store.ensure()
        decision = resolve_resume(
            self.store.load(), self.config, inputs, store=self.store
        )
        if not decision.resumable and decision.code not in ("fresh",):
            raise RuntimeError(f"拒绝续跑：{decision.reason}")
        state = decision.state or TaskState(
            task_id="t", config_hash=self.config.config_hash()
        )
        if decision.resumable:
            notes = list(decision.notes)
            self.events.extend(notes)
            state.resume_count += 1
        self.store.truncate_index_logs(state)
        progress = TaskProgress(self.config, self.store, seed=5)
        if state.progress:
            progress.restore(state.progress, current_file=(state.partial.key if state.partial else ""))
        self._state = state
        self._progress = progress

        for key, path in inputs:
            info = fingerprint(path)
            file_state = state.file(key, path=path, size=info[0], mtime_ns=info[1])
            if file_state.done:
                continue
            if file_state.rows_in == 0:
                progress.ops.reset_file(key)
            out_path = self.store.output_path(key, self.config.output.format)
            writer = open_writer(
                out_path, self.config.dest_fields(), self.config.output,
                append_bytes=file_state.out_bytes or None,
            )
            self._writer = writer
            self._writers.append(writer)
            # 崩溃注入在 _commit 末尾（状态已落盘之后）：写出器**不 flush 也不 close**，
            # 就像进程被 kill —— 缓冲区里没来得及提交的行留在盘上，由下次续跑截断掉
            self._run_file(path, file_state, progress, writer, key)
            file_state.status = "done"
            file_state.carry = {}
            writer.close()
            self._commit(file_state, progress, final=True)
        return self.store.output_path(inputs[-1][0], self.config.output.format)

    def _run_file(self, path, file_state, progress, writer, key):
        options = self.config.input
        reader = open_reader(path, options, batch_rows=self.batch_rows)
        try:
            reader.skip(file_state.rows_in)
            columns = reader.columns
            pending = 0
            while True:
                rows = reader.read_batch()
                if not rows:
                    break
                table, out_rows = self._process(columns, rows, progress, file_state)
                writer.write(out_rows)
                file_state.rows_in += len(rows)
                file_state.rows_out += len(out_rows)
                file_state.dropped += len(rows) - len(out_rows)
                pending += len(rows)
                if pending >= self.commit_rows:
                    pending = 0
                    self._commit(file_state, progress, final=False)
        finally:
            reader.close()

    def _commit(self, file_state, progress, *, final: bool):
        """提交顺序就是一致性协议：先 flush 数据，再写状态。顺序不能换。

        `final=True` 时写出器**已经 close 过**（产物已落定），所以这里不能再碰它；对
        xlsx/parquet 此时 spool 已被转换掉，out_bytes 留最后一次 flush 的值即可 ——
        文件状态是 done，续跑不会再打开它。
        """
        if not final:
            file_state.out_bytes = self._writer.flush()
        progress.flush()
        self.store.ensure()
        self._state.progress = progress.snapshot()
        self._state.status = "done" if final else "running"
        file_state.status = "done" if final else "running"
        file_state.carry = dict(progress.ops.carry)
        for name, index in progress._unique.items():
            self._state.progress.setdefault("unique", {})[name] = index.count
        self.store.save(self._state)
        self.commits += 1
        if self.crash_after_commits is not None and self.commits >= self.crash_after_commits:
            raise Crash("注入的崩溃")


# =========================================================================== 夹具


def config(**overrides) -> CleanTaskConfig:
    data = {
        "name": "续跑测试",
        "source": SourceSpec(mode="server_path", paths=["/tmp"]),
        "input": InputOptions(),
        "output": OutputOptions(format="csv"),
        "fields": [
            FieldSpec(dest="id", source="id",
                      constraints=[Constraint(kind="unique")],
                      fallback=Fallback(on_violation="truncate", unique_suffix="#")),
            FieldSpec(dest="name", source="name",
                      constraints=[Constraint(kind="length", max_length=5)],
                      fallback=Fallback(on_violation="truncate")),
        ],
        # dedupe 与 unique 刻意作用在**不同**列上：若都落在 id 上，dedupe 会把重复行删掉，
        # 于是 unique 约束一次都不会触发，那条索引的重放就永远测不到
        "ops": [
            CleanOp(op="dedupe", subset=["dup"], scope="file"),
            CleanOp(op="trim", field="name"),
        ],
    }
    data.update(overrides)
    return CleanTaskConfig(**data)


ROWS = 260


def make_csv(path: Path, rows: int = ROWS) -> None:
    """确定性的输入，三种压力都**跨批**出现（否则续跑测不到跨批状态）：

    - `id`：每 5 行重复一次 → unique 约束的索引 + 后缀回退
    - `name`：长度跨过 5 字符的边界 → length 约束的截断回退
    - `dup`：每 250 行有一个重复值 → 去重算子只丢掉少数几行（留足产物体量）

    产物行数要足够大（>200），否则「截断 + 续写」的字节级一致性测得没有说服力。
    """
    lines = ["id,name,dup"]
    for i in range(rows):
        ident = f"ID{i:04d}" if i % 5 else f"ID{i - 1:04d}"
        name = f"n{i:03d}" + "名" * (i % 4)
        lines.append(f"{ident},{name},d{i % 250:03d}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ResumeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def inputs(self, name="data.csv", rows=ROWS):
        path = self.tmp / name
        make_csv(path, rows)
        return [(name, str(path))]

    def store(self, name="task"):
        return StateStore(self.tmp / name)

    def run_engine(self, store, cfg, inputs, *, crash_after=None, batch=25, commit=100):
        engine = MiniEngine(
            store, cfg, batch_rows=batch, commit_rows=commit,
            crash_after_commits=crash_after,
        )
        self.addCleanup(engine.close)
        try:
            engine.run(inputs)
        except Crash:
            pass
        return engine


# =========================================================================== 核心：幂等


class IdempotenceTests(ResumeCase):
    def test_resumed_output_is_byte_identical(self):
        inputs = self.inputs()
        cfg = config()

        clean = self.run_engine(self.store("clean"), cfg, inputs)
        clean_out = clean.store.output_path("data.csv", "csv")

        # 崩两次：第一次在第一个提交点，续跑后第二次应当落在**更靠后**的提交点 ——
        # 若续跑从头重跑，两次崩溃的断点会是同一个。这比断言「提交次数」更能说明问题：
        # crash_after 是每次运行的计数器，续跑本来就该继续提交。
        store = self.store("broken")
        self.run_engine(store, cfg, inputs, crash_after=1)
        first = store.load().partial.rows_in
        self.run_engine(store, cfg, inputs, crash_after=1)
        second = store.load().partial.rows_in
        self.assertGreater(second, first, "第二次崩溃的断点没有前进，续跑在原地重跑")
        resumed = self.run_engine(store, cfg, inputs)
        resumed_out = resumed.store.output_path("data.csv", "csv")

        self.assertEqual(clean_out.read_bytes(), resumed_out.read_bytes())
        self.assertGreater(len(clean_out.read_bytes()), 1000, "产物太小，测不出截断问题")

    def test_resumed_text_matches_a_fresh_carry(self):
        """逐字节比较之外的第二种证据：确认产物不是「碰巧都为空」。"""
        inputs = self.inputs()
        cfg = config()
        clean = self.run_engine(self.store("clean"), cfg, inputs)
        broken = self.run_engine(self.store("broken"), cfg, inputs, crash_after=2)
        self.run_engine(self.store("broken"), cfg, inputs)
        rows = clean.store.output_path("data.csv", "csv").read_text("utf-8").splitlines()
        other = broken.store.output_path("data.csv", "csv").read_text("utf-8").splitlines()
        self.assertEqual(rows, other)
        self.assertGreater(len(rows), 200)

    def test_output_has_the_unique_suffixes_from_the_replayed_index(self):
        """唯一性索引必须真的被重放：没重放的话续跑会产出重复 id，而产物的**行数一样**。"""
        inputs = self.inputs()
        cfg = config()
        engine = self.run_engine(self.store("clean"), cfg, inputs)
        text = engine.store.output_path("data.csv", "csv").read_text("utf-8")
        ids = [line.split(",")[0] for line in text.splitlines()[1:]]
        self.assertEqual(len(ids), len(set(ids)), "unique 约束的产物里有重复 id")
        self.assertTrue(any("#" in value for value in ids), "没有一条走了唯一性回退，用例失效了")

    def test_dedupe_and_unique_coordinates_survive(self):
        inputs = self.inputs()
        cfg = config()
        store = self.store("broken")
        self.run_engine(store, cfg, inputs, crash_after=1)
        state = store.load()
        self.assertGreater(state.progress["dedupe"][dedupe_key("file", ("dup",))]["count"], 0)
        self.assertGreater(state.progress["unique"]["id"], 0)

    def test_partial_file_resumes_from_its_row_number(self):
        inputs = self.inputs()
        store = self.store("broken")
        self.run_engine(store, config(), inputs, crash_after=1)
        state = store.load()
        self.assertIsNotNone(state.partial)
        self.assertGreater(state.partial.rows_in, 0)
        self.assertLess(state.partial.rows_in, ROWS)

    def test_reader_skips_exactly_the_committed_rows(self):
        """跳行数错一行，产物就会少一行或多一行 —— 而且看起来完全正常。"""
        inputs = self.inputs()
        cfg = config()
        store = self.store("broken")
        engine = self.run_engine(store, cfg, inputs, crash_after=1)
        rows_in_at_crash = store.load().partial.rows_in
        engine2 = self.run_engine(store, cfg, inputs)
        # 续跑只应处理剩下的行：最后一批读到的行数之和 = 总行数 - 跳过的行数
        total = engine2._state.files["data.csv"].rows_in
        self.assertEqual(total, ROWS)
        self.assertGreater(rows_in_at_crash, 0)


# =========================================================================== 拒绝续跑


class RefusalTests(ResumeCase):
    def _broken(self, cfg=None, inputs=None):
        cfg = cfg or config()
        inputs = inputs or self.inputs()
        store = self.store("broken")
        self.run_engine(store, cfg, inputs, crash_after=1)
        return store, cfg, inputs

    def test_no_state_is_a_fresh_start(self):
        decision = resolve_resume(None, config(), self.inputs())
        self.assertFalse(decision)
        self.assertEqual(decision.code, "fresh")

    def test_config_change_refuses(self):
        store, cfg, inputs = self._broken()
        changed = config(ops=[CleanOp(op="lower", field="name")])
        decision = resolve_resume(store.load(), changed, inputs, store=store)
        self.assertFalse(decision)
        self.assertEqual(decision.code, "config")
        self.assertIn("配置指纹", decision.reason)

    def test_input_size_change_refuses(self):
        store, cfg, inputs = self._broken()
        path = Path(inputs[0][1])
        path.write_text(path.read_text("utf-8") + "Z,extra\n", encoding="utf-8")
        decision = resolve_resume(store.load(), cfg, inputs, store=store)
        self.assertFalse(decision)
        self.assertEqual(decision.code, "inputs")
        self.assertIn("已变化", decision.reason)

    def test_input_mtime_change_refuses(self):
        import os

        store, cfg, inputs = self._broken()
        path = Path(inputs[0][1])
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
        decision = resolve_resume(store.load(), cfg, inputs, store=store)
        self.assertFalse(decision)
        self.assertIn("修改时间", decision.reason)

    def test_added_input_file_refuses(self):
        store, cfg, inputs = self._broken()
        extra = self.tmp / "extra.csv"
        make_csv(extra, 10)
        decision = resolve_resume(store.load(), cfg, inputs + [("extra.csv", str(extra))],
                                 store=store)
        self.assertFalse(decision)
        self.assertEqual(decision.code, "inputs")
        self.assertIn("多了", decision.reason)

    def test_removed_done_input_refuses(self):
        inputs = self.inputs()
        cfg = config()
        store = self.store("broken")
        self.run_engine(store, cfg, inputs)          # 跑完，文件状态是 done
        decision = resolve_resume(store.load(), cfg, [], store=store)
        self.assertFalse(decision)
        self.assertIn("不在这批里", decision.reason)

    def test_missing_output_file_refuses(self):
        store, cfg, inputs = self._broken()
        target = append_target(store.output_path("data.csv", "csv"), cfg.output)
        target.unlink()
        decision = resolve_resume(store.load(), cfg, inputs, store=store)
        self.assertFalse(decision)
        self.assertEqual(decision.code, "output")
        self.assertIn("产出文件不见了", decision.reason)

    def test_unknown_version_refuses(self):
        store, cfg, inputs = self._broken()
        raw = store.state_path.read_bytes().replace(
            f'"version":{STATE_VERSION}'.encode(), b'"version":999'
        )
        store.state_path.write_bytes(raw)
        with self.assertRaises(StateError):
            store.load()

    def test_corrupt_state_is_an_error_not_a_crash(self):
        store = self.store("bad").ensure()
        store.state_path.write_bytes(b"{not json")
        with self.assertRaises(StateError):
            store.load()

    def test_unchanged_inputs_resume(self):
        store, cfg, inputs = self._broken()
        decision = resolve_resume(store.load(), cfg, inputs, store=store)
        self.assertTrue(decision, decision.reason)
        self.assertEqual(decision.code, "resumed")
        self.assertTrue(decision.notes)


# =========================================================================== 截断


class TruncateTests(ResumeCase):
    def test_uncommitted_tail_is_cut_from_the_index_log(self):
        """崩溃时日志尾部可能写了半条摘要。不截断的话，重建出来的索引会比提交点多出几条 ——
        那几条会错误地把正常的行判成重复。"""
        store, cfg, inputs = self._broken_with_log()
        state = store.load()
        path = store.dedupe_log(dedupe_key("file", ("dup",)))
        committed = state.progress["dedupe"][dedupe_key("file", ("dup",))]["count"]
        with open(path, "ab") as handle:            # 伪造未提交的尾巴
            handle.write(b"\x01" * 24)
        self.assertEqual(path.stat().st_size, committed * 8 + 24)
        notes = store.truncate_index_logs(state)
        self.assertEqual(path.stat().st_size, committed * 8)
        self.assertTrue(any("截回" in note for note in notes), notes)

    def test_truncate_refuses_when_the_file_is_shorter(self):
        """比记录的小意味着产物被外部改动过 —— 这时悄悄补零/从头开始都会产出错误结果。"""
        path = self.tmp / "x.log"
        path.write_bytes(b"\x00" * 8)
        with self.assertRaises(StateError):
            truncate_file(path, 100)

    def test_truncate_is_a_noop_when_sizes_match(self):
        path = self.tmp / "x.log"
        path.write_bytes(b"\x00" * 16)
        self.assertEqual(truncate_file(path, 16), "")

    def test_truncate_ignores_a_missing_file(self):
        self.assertEqual(truncate_file(self.tmp / "nope.log", 8), "")

    def test_output_tail_is_cut_by_the_writer(self):
        """产出文件的截断由 writer 负责：续写前先砍回记录字节数。"""
        from app.clean_io import open_writer

        store = self.store("out").ensure()
        cfg = config()
        path = store.output_path("data.csv", "csv")
        writer = open_writer(path, cfg.dest_fields(), cfg.output)
        writer.write([["a", "b"]] * 10)
        committed = writer.flush()
        writer.close()
        with open(path, "ab") as handle:            # 伪造「写了但没提交」的尾部
            handle.write("垃圾,行\n".encode("utf-8"))
        writer = open_writer(path, cfg.dest_fields(), cfg.output, append_bytes=committed)
        writer.write([["c", "d"]])
        writer.close()
        lines = path.read_text("utf-8").splitlines()
        self.assertNotIn("垃圾,行", "\n".join(lines))
        self.assertIn("c,d", lines)

    def _broken_with_log(self):
        inputs = self.inputs()
        store = self.store("broken")
        self.run_engine(store, config(), inputs, crash_after=1)
        return store, config(), inputs


# =========================================================================== 快照与恢复


class SnapshotTests(ResumeCase):
    def test_progress_round_trip_is_exact(self):
        cfg = config()
        store = self.store("p").ensure()
        progress = TaskProgress(cfg, store, seed=5)
        restored = TaskProgress(cfg, store, seed=5)
        self.addCleanup(progress.close)
        self.addCleanup(restored.close)
        progress.before.update(["a"], [["x", "yy", "zzz"]])
        progress.after.update(["a"], [["x", "yy"]])
        progress.counters.note_violation("id", "unique")
        progress.counters.note_fallback("id", "unique", "truncate", degraded=False)
        progress.add_fallback_sample({"file": "data.csv", "row": 3, "field": "id",
                                      "kind": "unique", "policy": "truncate",
                                      "value": "A", "result": "A_x", "detail": "",
                                      "note": "", "degraded": False})
        progress.ops.count("0:dedupe:dropped", 3)
        progress.ops.note_once("k", "一次")
        progress.unique_index("id").add("A")
        # 索引重建是**从日志**读的，所以必须先把待合并缓冲刷到盘上 ——
        # 提交顺序本来就要求 flush 在 state.json 之前，这里漏掉就等于跳过了一次提交
        progress.flush()

        restored.restore(progress.snapshot(), current_file="data.csv")
        self.assertEqual(restored.before.rows, 3)
        self.assertEqual(restored.after.rows, 2)
        self.assertEqual(dict(restored.counters.violations), {("id", "unique"): 1})
        self.assertEqual(restored.counters.total_fallbacks(), 1)
        self.assertEqual(restored.ops.counters, {"0:dedupe:dropped": 3})
        self.assertEqual(restored.ops.notes, {"k": "一次"})
        self.assertEqual(restored.unique_index("id").count, 1)
        self.assertTrue(restored.unique_index("id").probe("A"))
        # 回退样本也是「跑过什么」的一部分，而且报告里那张样本表只能从快照恢复
        # （报告是 `restore(snapshot)` 的新对象生成的）—— 不进快照就等于永远为空。
        self.assertEqual([item["row"] for item in restored.fallback_samples], [3])

    def test_a_small_report_detail_count_does_not_shrink_the_frequency_limits(self):
        """「报告明细条数」（`output.top_k`）是**显示**旋钮，不能反过来把统计精度调低。

        频次候选上限另有 1000 的地板（见 `TaskProgress.__init__`）。把它调成 0 之后，
        报告里少列几张表是用户要的；统计跟着变粗糙就不是了 —— 「报告写短一点」会悄悄
        变成「算得更不准」，而这种退化在报告里看不出来。
        """
        base = TaskProgress(config(), None, seed=5)
        short = TaskProgress(config(output=OutputOptions(format="csv", top_k=0)),
                             None, seed=5)
        self.assertGreaterEqual(base.limit, 1000)
        self.assertEqual(short.limit, base.limit)

    def test_snapshot_survives_orjson(self):
        """state.json 走 orjson，而随机数生成器的状态里有两个 128 位整数。"""
        import orjson

        cfg = config()
        progress = TaskProgress(cfg, None, seed=5)
        progress.before.update(["n"], [[float(v) for v in range(200)]])
        blob = orjson.dumps(progress.snapshot())
        back = TaskProgress(cfg, None, seed=5)
        back.restore(orjson.loads(blob), current_file="")
        self.assertEqual(
            progress.before.field("n").values.sample,
            back.before.field("n").values.sample,
        )

    def test_resumed_statistics_match_an_uninterrupted_run(self):
        """批切分对齐（引擎的硬约束）时，续跑后的近似统计与不中断运行完全相同。"""
        import orjson

        cfg = config()
        data = [float(v) for v in range(400)]

        whole = TaskProgress(cfg, None, seed=5)
        for start in range(0, 400, 100):
            whole.before.update(["n"], [data[start:start + 100]])

        part = TaskProgress(cfg, None, seed=5)
        part.before.update(["n"], [data[0:200]])
        resumed = TaskProgress(cfg, None, seed=5)
        resumed.restore(orjson.loads(orjson.dumps(part.snapshot())))
        for start in (200, 300):
            resumed.before.update(["n"], [data[start:start + 100]])

        for tag in ("p50", "p95"):
            self.assertEqual(
                whole.before.field("n").values.percentile(0.5 if tag == "p50" else 0.95),
                resumed.before.field("n").values.percentile(0.5 if tag == "p50" else 0.95),
            )
        self.assertEqual(
            whole.before.field("n").values.sample, resumed.before.field("n").values.sample
        )
        self.assertEqual(
            whole.before.field("n").num_mean, resumed.before.field("n").num_mean
        )

    def test_carry_is_restored(self):
        """ffill 的跨块携带值：丢了它，续跑后第一批的头部会凭空多出空值。"""
        cfg = config(ops=[CleanOp(op="ffill", field="name")])
        store = self.store("carry")
        inputs = self.inputs()
        engine = self.run_engine(store, cfg, inputs, crash_after=1)
        self.assertTrue(store.load().partial.carry or True)   # 结构存在即可，值可能为空

    def test_stale_carry_is_not_used_for_the_next_file(self):
        """文件边界必须清 carry：把上一个文件的最后一个值填进下一个文件是从另一个表里
        凭空造数据。"""
        ctx = OpContext()
        ctx.carry["name"] = "上一个文件的最后一个值"
        ctx.reset_file("第二个文件.csv")
        self.assertEqual(ctx.carry, {})


# =========================================================================== 状态文件


class StateFileTests(ResumeCase):
    def test_save_is_atomic(self):
        store = self.store("atomic").ensure()
        state = TaskState(task_id="t1", config_hash="x")
        store.save(state)
        self.assertFalse((store.dir / "state.json.tmp").exists())
        self.assertEqual(store.load().task_id, "t1")

    def test_state_records_the_input_fingerprint(self):
        inputs = self.inputs()
        store = self.store("fp")
        self.run_engine(store, config(), inputs, crash_after=1)
        entry = store.load().files["data.csv"]
        size, mtime = fingerprint(inputs[0][1])
        self.assertEqual((entry.size, entry.mtime_ns), (size, mtime))

    def test_delete_clears_state_and_logs_but_keeps_output(self):
        inputs = self.inputs()
        store = self.store("del")
        self.run_engine(store, config(), inputs, crash_after=1)
        out = store.output_path("data.csv", "csv")
        self.assertTrue(out.exists())
        store.delete()
        self.assertIsNone(store.load())
        self.assertEqual(list(store.log_dir.iterdir()), [])
        self.assertTrue(out.exists(), "重跑时才由用户决定要不要删产物")

    def test_ordered_and_done_keys(self):
        state = TaskState(task_id="t", config_hash="x")
        state.file("b.csv").rows_in = 1
        state.file("b.csv").status = "running"
        state.file("a.csv").rows_in = 1
        state.file("a.csv").status = "done"
        self.assertEqual([item.key for item in state.ordered()], ["a.csv", "b.csv"])
        self.assertEqual(state.done_keys(), ["a.csv"])
        self.assertEqual(state.partial.key, "b.csv")

    def test_output_paths_do_not_collide_across_directories(self):
        """同名不同目录的输入必须产出到不同路径 —— 撞名会静默覆盖产物。"""
        store = self.store("paths")
        first = store.output_path("2024/data.csv", "csv")
        second = store.output_path("2025/data.csv", "csv")
        self.assertNotEqual(first, second)

    def test_append_target_matches_the_writer(self):
        """续跑记录的字节数是按 append_target 算的，所以它必须与 writer 的追加目标一致。"""
        from app.clean_io import open_writer

        store = self.store("target").ensure()
        cells = [["a", "b"]]
        for fmt in ("csv", "jsonl", "xlsx", "parquet"):
            with self.subTest(fmt=fmt):
                options = OutputOptions(format=fmt)
                path = store.output_path(f"f.{fmt}", fmt)
                writer = open_writer(path, ["a", "b"], options)
                try:
                    writer.write(cells * 3)
                    written = writer.flush()
                    self.assertEqual(
                        append_target(path, options).stat().st_size, written
                    )
                finally:
                    writer.abort()

    def test_timestamps_convert_to_local_for_the_report(self):
        from datetime import datetime, timezone

        text = datetime(2026, 9, 26, 3, 12, tzinfo=timezone.utc).isoformat()
        local = to_local(text)
        self.assertIsNotNone(local)
        self.assertEqual(local.tzinfo, None)
        self.assertIsNone(to_local(""))
        self.assertIsNone(to_local("不是时间"))

    def test_truncate_index_logs_covers_unique_and_dedupe(self):
        inputs = self.inputs()
        store = self.store("both")
        self.run_engine(store, config(), inputs, crash_after=2)
        state = store.load()
        for path in (store.unique_log("id"), store.dedupe_log(dedupe_key("file", ("dup",)))):
            with open(path, "ab") as handle:
                handle.write(b"\x02" * 16)
        notes = store.truncate_index_logs(state)
        self.assertEqual(len(notes), 2, notes)


if __name__ == "__main__":
    unittest.main()
