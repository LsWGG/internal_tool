import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clean_models import CleanOp
from app.clean_ops import (
    OpContext,
    Table,
    apply_ops,
    dedupe_key,
    split_dedupe_key,
    validate_ops,
)
from app.clean_schema import _RE_FALLBACK_MAX_LEN


def op(name, **kwargs):
    # 绝大多数算子需要一个目标列；concat/dedupe/drop_null 会忽略它。放在默认值里，
    # 测试才只需要写真正在测的那个参数。
    kwargs.setdefault("field", "f")
    return CleanOp(op=name, **kwargs)


def run(ops, rows, columns=("f",), ctx=None):
    """应用算子，返回 (行列表, 上下文)。"""
    ctx = ctx if ctx is not None else OpContext()
    table = apply_ops(Table.from_rows(list(columns), rows), ops, ctx)
    return table.to_rows(), ctx


def column(ops, values, name="f", ctx=None):
    rows, _ = run(ops, [[v] for v in values], columns=(name,), ctx=ctx)
    return [row[0] for row in rows]


def counts(ctx, suffix):
    return {k: v for k, v in ctx.counters.items() if k.endswith(suffix)}


class TableTests(unittest.TestCase):
    def test_from_rows_regular(self):
        table = Table.from_rows(["a", "b"], [[1, 2], [3, 4]])
        self.assertEqual(table.columns, ["a", "b"])
        self.assertEqual(table.data, [[1, 3], [2, 4]])
        self.assertEqual(table.nrows(), 2)

    def test_from_rows_pads_and_truncates(self):
        # 缺失补空串、多余截掉：跑到一半因为一行短了两个字段而抛异常是不可接受的，
        # 而「哪一行不规整」由读取层精确计数（reader.ragged_rows），这里只修好。
        table = Table.from_rows(["a", "b", "c"], [[1], [1, 2, 3, 4]])
        self.assertEqual(table.data, [[1, 1], ["", 2], ["", 3]])

    def test_from_rows_empty(self):
        table = Table.from_rows(["a", "b"], [])
        self.assertEqual((table.nrows(), table.ncols()), (0, 2))
        self.assertEqual(table.to_rows(), [])

    def test_duplicate_names_resolve_to_first(self):
        table = Table.from_rows(["id", "id"], [["x", "y"]])
        self.assertEqual(table.index("id"), 0)
        # 覆盖的是首次出现的那一列：下游按名取值只会拿到第一列，第二列是幽灵数据
        table = table.with_column("id", ["z"])
        self.assertEqual(table.columns, ["id", "id"])
        self.assertEqual(table.data[1], ["y"])

    def test_with_column_appends_when_new(self):
        table = Table.from_rows(["a"], [["1"]]).with_column("b", ["2"])
        self.assertEqual(table.columns, ["a", "b"])
        self.assertEqual(table.data, [["1"], ["2"]])

    def test_drop_rows_rejects_mask_length_mismatch(self):
        table = Table.from_rows(["a"], [["1"], ["2"]])
        with self.assertRaises(ValueError):
            table.drop_rows([True])

    def test_drop_rows_keeps_identity_when_nothing_dropped(self):
        table = Table.from_rows(["a"], [["1"], ["2"]])
        self.assertIs(table.drop_rows([True, True]), table)


class TextOpTests(unittest.TestCase):
    def test_trim_counts_changed_cells(self):
        self.assertEqual(column([op("trim")], ["  a  ", "b", " a"]), ["a", "b", "a"])

    def test_lower_upper_are_unicode_aware(self):
        self.assertEqual(column([op("lower")], ["ABC"]), ["abc"])
        self.assertEqual(column([op("upper")], ["abc"]), ["ABC"])

    def test_nfkc_normalizes_full_width(self):
        self.assertEqual(column([op("nfkc")], ["ＡＢ１２"]), ["AB12"])

    def test_text_ops_leave_non_strings_alone(self):
        # 把数值悄悄转成文本是数据改动，该由用户显式配 cast。这里必须是原样通过。
        rows, ctx = run([op("trim")], [[7], [3.5], [None]], columns=("f",))
        self.assertEqual([r[0] for r in rows], [7, 3.5, None])
        self.assertEqual(counts(ctx, ":changed"), {})

    def test_replace_is_literal_not_regex(self):
        # "." 在正则里匹配任意字符，字面量替换必须原样替换
        got = column([op("replace", pattern=".", replacement="/")], ["a.b"])
        self.assertEqual(got, ["a/b"])

    def test_regex_replace_replaces_every_match(self):
        got = column(
            [op("regex_replace", pattern=r"(\d+)", replacement=r"<\1>")], ["a12b345"]
        )
        self.assertEqual(got, ["a<12>b<345>"])

    def test_regex_replace_uses_group_references(self):
        got = column(
            [op("regex_replace", pattern=r"(\w+)@(\w+)", replacement=r"\2.\1")],
            ["user@host"],
        )
        self.assertEqual(got, ["host.user"])

    def test_regex_replace_bad_template_keeps_value_and_counts(self):
        # 一个字段的笔误不该炸掉整个 GB 级任务，但也绝不能装作替换成功了
        got = column(
            [op("regex_replace", pattern=r"(\d+)", replacement=r"\9")], ["a12"]
        )
        self.assertEqual(got, ["a12"])
        _, ctx = run(
            [op("regex_replace", pattern=r"(\d+)", replacement=r"\9")], [["a12"]]
        )
        self.assertEqual(counts(ctx, ":template_error"), {"0:regex_replace:template_error": 1})
        self.assertTrue(any("模板有误" in text for text in ctx.notes.values()))

    def test_regex_replace_over_limit_text_is_skipped_not_truncated(self):
        # 回落引擎 + 超长文本：不做替换，且必须计数（不能基于残缺文本产出结果）
        target = "ab" * (_RE_FALLBACK_MAX_LEN // 2 + 10)
        ops = [op("regex_replace", pattern=r"(ab)\1", replacement="X")]
        got = column(ops, [target])
        self.assertEqual(got, [target])
        _, ctx = run(ops, [[target]])
        self.assertEqual(counts(ctx, ":skipped"), {"0:regex_replace:skipped": 1})

    def test_slice_with_open_ends(self):
        self.assertEqual(column([op("slice", start=0, end=3)], ["abcdef"]), ["abc"])
        self.assertEqual(column([op("slice", start=3)], ["abcdef"]), ["def"])
        self.assertEqual(column([op("slice", end=2)], ["abcdef"]), ["ab"])
        # 负下标沿用 Python 语义
        self.assertEqual(column([op("slice", start=-2)], ["abcdef"]), ["ef"])


class NullOpTests(unittest.TestCase):
    def test_fill_null_uses_is_empty_not_strip(self):
        # 空白串是数据事实，该由 trim 显式处理；在这里顺手 strip 会让边界不可预测
        got = column([op("fill_null", value="X")], ["", None, " ", "a"])
        self.assertEqual(got, ["X", "X", " ", "a"])

    def test_fill_null_counts_only_filled(self):
        _, ctx = run([op("fill_null", value="X")], [[""], [""], ["a"]])
        self.assertEqual(counts(ctx, ":changed"), {"0:fill_null:changed": 2})

    def test_ffill_within_and_across_batches(self):
        ops = [op("ffill")]
        first, ctx = run(ops, [[""], ["a"], [""], ["b"]])
        # 开头就是空的：没有可携带的值，保持空 —— 不能凭空造
        self.assertEqual([r[0] for r in first], ["", "a", "a", "b"])
        second, ctx = run(ops, [[""], [""], ["c"], [""]], ctx=ctx)
        self.assertEqual([r[0] for r in second], ["b", "b", "c", "c"])
        self.assertEqual(ctx.carry["f"], "c")

    def test_ffill_does_not_carry_across_files(self):
        # 把上一个文件的最后一行填进下一个文件，是从另一个表里凭空造数据
        ops = [op("ffill")]
        _, ctx = run(ops, [["a"], [""]])
        ctx.reset_file("b.csv")
        got, ctx = run(ops, [[""], ["d"]], ctx=ctx)
        self.assertEqual([r[0] for r in got], ["", "d"])

    def test_drop_null_drops_any_empty(self):
        rows, ctx = run(
            [op("drop_null", fields=["a", "b"])],
            [["1", "x"], ["1", ""], ["", "y"], ["2", "z"]],
            columns=("a", "b"),
        )
        self.assertEqual(rows, [["1", "x"], ["2", "z"]])
        self.assertEqual(counts(ctx, ":dropped"), {"0:drop_null:dropped": 2})

    def test_drop_null_single_field(self):
        got = column([op("drop_null", field="f")], ["a", "", None])
        self.assertEqual(got, ["a"])

    def test_missing_field_skips_op_and_counts(self):
        rows, ctx = run([op("trim", field="nope")], [[" a "]])
        self.assertEqual(rows, [[" a "]])
        self.assertEqual(ctx.counters, {"0:trim:field_missing": 1})
        self.assertEqual(len(ctx.notes), 1)
        # 同一个缺失字段再出现时不重复打日志
        run([op("trim", field="nope")], [[" a "]], ctx=ctx)
        self.assertEqual(len(ctx.notes), 1)


class CastTests(unittest.TestCase):
    def test_cast_int_accepts_integral_floats(self):
        self.assertEqual(column([op("cast", value_kind="int")], ["28", "3.0", " 42 "]), [28, 3, 42])

    def test_cast_int_rejects_lossy_values(self):
        # "3.7" → 3 是静默丢数据；保持原值并计数，让用户看见
        got = column([op("cast", value_kind="int")], ["3.7", "abc", "1e3"])
        self.assertEqual(got, ["3.7", "abc", 1000])

    def test_cast_int_keeps_long_literals_exact(self):
        digits = "9" * 25
        got = column([op("cast", value_kind="int")], [digits])
        self.assertEqual(got, [int(digits)])

    def test_cast_int_counters(self):
        _, ctx = run([op("cast", value_kind="int")], [["3"], ["3.7"], ["x"]])
        self.assertEqual(counts(ctx, ":failed"), {"0:cast:failed": 2})
        self.assertEqual(counts(ctx, ":changed"), {"0:cast:changed": 1})

    def test_cast_float_is_strict(self):
        got = column([op("cast", value_kind="float")], ["3.5", "1,234", "1e3"])
        self.assertEqual(got, [3.5, "1,234", 1000.0])

    def test_cast_str_uses_cell_text(self):
        self.assertEqual(column([op("cast", value_kind="str")], [3.0, True]), ["3", "true"])

    def test_cast_refuses_bool_to_int(self):
        # bool → int 在 Python 里合法（True == 1），但那是把标记值悄悄变成一个数
        got = column([op("cast", value_kind="int")], [True, False])
        self.assertEqual(got, [True, False])
        _, ctx = run([op("cast", value_kind="int")], [[True]])
        self.assertEqual(counts(ctx, ":failed"), {"0:cast:failed": 1})

    def test_cast_empty_passes_through_without_failing(self):
        got, ctx = run([op("cast", value_kind="int")], [[""], [None]])
        self.assertEqual([r[0] for r in got], ["", None])
        self.assertEqual(ctx.counters, {})

    def test_cast_date_and_datetime_types(self):
        got = column([op("cast", value_kind="date")], ["2026-09-26"])
        self.assertEqual(got[0].isoformat(), "2026-09-26")
        self.assertNotIn(" ", str(got[0]))

    def test_cast_date_honours_input_formats(self):
        ops = [op("cast", value_kind="date", date_formats=["%Y%m%d"])]
        self.assertEqual(column(ops, ["20260926"])[0].isoformat(), "2026-09-26")
        # 格式不匹配就报失败，而不是猜
        self.assertEqual(column(ops, ["2026-09-26"]), ["2026-09-26"])


class DateFormatTests(unittest.TestCase):
    def test_iso_input(self):
        got = column([op("date_format", date_format="%Y/%m/%d")], ["2026-09-26"])
        self.assertEqual(got, ["2026/09/26"])

    def test_input_formats(self):
        ops = [op("date_format", date_format="%Y-%m-%d", date_formats=["%Y%m%d"])]
        self.assertEqual(column(ops, ["20260926"]), ["2026-09-26"])

    def test_accepts_datetime_from_previous_cast(self):
        ops = [
            op("cast", value_kind="datetime"),
            op("date_format", date_format="%Y-%m"),
        ]
        self.assertEqual(column(ops, ["2026-09-26 08:30:00"]), ["2026-09"])

    def test_unparseable_is_counted_not_dropped(self):
        _, ctx = run([op("date_format", date_format="%Y-%m-%d")], [["nope"], ["2026-09-26"]])
        self.assertEqual(counts(ctx, ":failed"), {"0:date_format:failed": 1})

    def test_bad_output_format_is_noop_with_note(self):
        # Linux 上无效格式（%Q）会原样输出，等于不改数据 —— 必须当作失败而不是成功
        got, ctx = run([op("date_format", date_format="%Q")], [["2026-09-26"]])
        self.assertEqual(got, [["2026-09-26"]])
        self.assertEqual(counts(ctx, ":failed"), {"0:date_format:failed": 1})
        self.assertTrue(any("无法用于 strftime" in text for text in ctx.notes.values()))


class NumberFormatTests(unittest.TestCase):
    def test_decimals_round_half_up(self):
        # 银行家舍入会让 2.5 → 2，而用户对「保留 0 位」的预期是 3
        ops = [op("number_format", value_kind="float", decimals=0)]
        self.assertEqual(column(ops, ["2.5", "3.5"]), ["3", "4"])

    def test_decimals_are_decimal_not_binary(self):
        ops = [op("number_format", value_kind="float", decimals=2)]
        self.assertEqual(column(ops, ["0.145"]), ["0.15"])

    def test_thousands_separator(self):
        ops = [op("number_format", value_kind="float", decimals=2, thousands=True)]
        self.assertEqual(column(ops, ["1234567.891"]), ["1,234,567.89"])

    def test_int_default_quantizes_float_scale(self):
        ops = [op("number_format", value_kind="int")]
        self.assertEqual(column(ops, ["1000.00"]), ["1000"])

    def test_float_default_expands_scientific_notation(self):
        ops = [op("number_format", value_kind="float")]
        self.assertEqual(column(ops, ["1e3"]), ["1000"])

    def test_thousands_comma_input_is_not_accepted(self):
        # "1,234" 不是数字。静默去掉逗号会让「格式不对」变成「格式对了」
        got = column([op("number_format", value_kind="float", thousands=True)], ["1,234"])
        self.assertEqual(got, ["1,234"])
        _, ctx = run([op("number_format", value_kind="float")], [["1,234"]])
        self.assertEqual(counts(ctx, ":failed"), {"0:number_format:failed": 1})

    def test_empty_stays_empty(self):
        got, ctx = run([op("number_format", value_kind="int")], [[""], [None]])
        self.assertEqual([r[0] for r in got], ["", None])
        self.assertEqual(ctx.counters, {})

    def test_non_finite_is_rejected(self):
        _, ctx = run([op("number_format", value_kind="float")], [["NaN"], ["Infinity"]])
        self.assertEqual(counts(ctx, ":failed"), {"0:number_format:failed": 2})


class DedupeTests(unittest.TestCase):
    def test_keeps_first_occurrence(self):
        rows, ctx = run(
            [op("dedupe", subset=["a"])],
            [["x", "1"], ["x", "2"], ["y", "3"]],
            columns=("a", "b"),
        )
        self.assertEqual(rows, [["x", "1"], ["y", "3"]])
        self.assertEqual(counts(ctx, ":dropped"), {"0:dedupe:dropped": 1})

    def test_default_subset_is_the_whole_row(self):
        rows, _ = run(
            [op("dedupe")], [["x", "1"], ["x", "2"]], columns=("a", "b")
        )
        self.assertEqual(rows, [["x", "1"], ["x", "2"]])

    def test_deduplicates_across_batches(self):
        ops = [op("dedupe", subset=["f"], scope="task")]
        first, ctx = run(ops, [["a"], ["b"], ["a"]])
        self.assertEqual([r[0] for r in first], ["a", "b"])
        second, ctx = run(ops, [["a"], ["c"]], ctx=ctx)
        self.assertEqual([r[0] for r in second], ["c"])
        self.assertEqual(counts(ctx, ":dropped"), {"0:dedupe:dropped": 2})

    def test_file_scope_resets_and_task_scope_does_not(self):
        file_ops = [op("dedupe", subset=["f"], scope="file")]
        task_ops = [op("dedupe", subset=["f"], scope="task")]
        ctx = OpContext()
        run(file_ops, [["a"]], ctx=ctx)
        run(task_ops, [["a"]], ctx=ctx)
        # 新文件：file 作用域从头开始（a 又能通过），task 作用域仍然记得 a
        ctx.reset_file("b.csv")
        got_file, _ = run(file_ops, [["a"]], ctx=ctx)
        self.assertEqual([r[0] for r in got_file], ["a"])
        got_task, _ = run(task_ops, [["a"]], ctx=ctx)
        self.assertEqual(got_task, [])

    def test_empty_rows_still_dedupe(self):
        # 全空的两行是真实的重复。UniqueIndex 把空值视为不参与唯一性，所以连接键必须保证非空
        rows, ctx = run([op("dedupe")], [["", ""], ["", ""]], columns=("a", "b"))
        self.assertEqual(rows, [["", ""]])
        self.assertEqual(counts(ctx, ":dropped"), {"0:dedupe:dropped": 1})

    def test_missing_subset_field_skips_op(self):
        rows, ctx = run([op("dedupe", subset=["nope"])], [["a"], ["a"]])
        self.assertEqual(len(rows), 2)
        self.assertEqual(ctx.counters, {"0:dedupe:field_missing": 1})


class ConcatTests(unittest.TestCase):
    def test_joins_fields_with_separator(self):
        rows, _ = run(
            [op("concat", fields=["a", "b"], dest="c", separator="-")],
            [["x", "y"]],
            columns=("a", "b"),
        )
        self.assertEqual(rows, [["x", "y", "x-y"]])

    def test_empty_values_keep_their_position(self):
        # 产出 "粤A-" 而不是 "粤A"：「第几段对应哪个字段」始终可预测
        rows, _ = run(
            [op("concat", fields=["a", "b"], dest="c", separator="-")],
            [["x", ""]],
            columns=("a", "b"),
        )
        self.assertEqual(rows, [["x", "", "x-"]])

    def test_overwrites_existing_dest_in_place(self):
        rows, _ = run(
            [op("concat", fields=["a", "b"], dest="a")],
            [["x", "y"]],
            columns=("a", "b"),
        )
        self.assertEqual(rows, [["xy", "y"]])

    def test_dest_does_not_shadow_source(self):
        rows, _ = run(
            [op("concat", fields=["a", "b"], dest="c")],
            [["x", "y"]],
            columns=("a", "b"),
        )
        self.assertEqual(rows[0], ["x", "y", "xy"])


class DedupeIndexResumeTests(unittest.TestCase):
    """去重索引的续跑接口。产物逐字节一致全靠这里。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def factory(self, key):
        return self.root / (key.replace("|", "_").replace(",", "+") + ".log")

    def context(self):
        """每个带日志的上下文都要关掉，否则每个测试都会留一个未关闭的句柄。"""
        ctx = OpContext(self.factory)
        self.addCleanup(ctx.close)
        return ctx

    def test_snapshot_and_restore_reproduce_the_same_drops(self):
        rows = [["a"], ["b"], ["a"], ["c"], ["b"], ["d"]]
        ops = [op("dedupe", subset=["f"], scope="task")]

        whole, _ = run(ops, rows, ctx=self.context())

        ctx = self.context()
        run(ops, rows[:3], ctx=ctx)
        snapshot = ctx.dedupe_snapshot()
        ctx.flush()

        resumed = self.context()
        resumed.restore_indexes(snapshot)
        second, _ = run(ops, rows[3:], ctx=resumed)
        self.assertEqual([r[0] for r in whole], ["a", "b", "c", "d"])
        self.assertEqual([r[0] for r in second], ["c", "d"])

    def test_file_scoped_restore_only_replays_the_current_file(self):
        rows = [["a"], ["b"], ["a"]]
        ops = [op("dedupe", subset=["f"], scope="file")]

        ctx = self.context()
        ctx.reset_file("a.csv")
        run(ops, rows, ctx=ctx)
        snapshot = ctx.dedupe_snapshot()
        ctx.flush()
        key = dedupe_key("file", ["f"])
        self.assertEqual(snapshot[key]["count"], 2)
        self.assertEqual(snapshot[key]["file_start"], 0)

        # 续跑到同一个文件：a 应该已经算过（日志里的 2 条可见）
        resumed = self.context()
        resumed.restore_indexes(snapshot, current_file="a.csv")
        got, _ = run(ops, [["a"], ["z"]], ctx=resumed)
        self.assertEqual([r[0] for r in got], ["z"])

        # 续跑时那个文件已经完成：可见区间必须为空，否则新文件的第一行会被错误丢掉
        finished = self.context()
        finished.restore_indexes(snapshot, current_file="b.csv")
        got, _ = run(ops, [["a"], ["a"]], ctx=finished)
        self.assertEqual([r[0] for r in got], ["a"])

    def test_reset_file_moves_the_visibility_window_without_a_new_log(self):
        ops = [op("dedupe", subset=["f"], scope="file")]
        ctx = self.context()
        ctx.reset_file("a.csv")
        run(ops, [["a"], ["b"]], ctx=ctx)
        ctx.reset_file("b.csv")
        # 同一个日志文件继续追加：换文件会让长度坐标失效，恢复时就没法切出可见区间
        self.assertTrue(list(self.root.glob("*.log")))
        run(ops, [["a"]], ctx=ctx)
        snapshot = ctx.dedupe_snapshot()
        entry = snapshot[dedupe_key("file", ["f"])]
        self.assertEqual(entry["count"], 3)
        self.assertEqual(entry["file_start"], 2)
        self.assertEqual(entry["file"], "b.csv")

    def test_key_roundtrip(self):
        scope, subset = split_dedupe_key(dedupe_key("task", ["a", "b"]))
        self.assertEqual(scope, "task")
        self.assertEqual(list(subset), ["a", "b"])


class ContextTests(unittest.TestCase):
    def test_counters_are_keyed_by_rule_slot(self):
        # 两条 trim 混成一个数，报告就没法说清「第几条规则做了什么」
        ops = [op("trim", field="f"), op("trim", field="g")]
        _, ctx = run(ops, [[" a ", " b "]], columns=("f", "g"))
        self.assertEqual(ctx.counters["0:trim:changed"], 1)
        self.assertEqual(ctx.counters["1:trim:changed"], 1)

    def test_note_once_is_deduplicated(self):
        ctx = OpContext()
        ctx.note_once("k", "first")
        ctx.note_once("k", "second")
        self.assertEqual(ctx.notes, {"k": "first"})

    def test_zero_counters_are_not_recorded(self):
        _, ctx = run([op("trim")], [["a"]])
        self.assertEqual(ctx.counters, {})


class ValidateOpsTests(unittest.TestCase):
    def test_missing_field_is_a_warning_not_an_error(self):
        # 目录来源下某个文件没有这个字段是正常现象
        errors, warnings = validate_ops([op("trim", field="nope")], ["f"])
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("nope", warnings[0])

    def test_uncompilable_regex_is_an_error(self):
        errors, _ = validate_ops([op("regex_replace", pattern="([", replacement="x")])
        self.assertTrue(errors)
        self.assertIn("无法编译", errors[0])

    def test_re2_fallback_is_a_warning(self):
        # 回落引擎没有线性时间保证，用户该知道
        errors, warnings = validate_ops(
            [op("regex_replace", pattern=r"(ab)\1", replacement="x")]
        )
        self.assertEqual(errors, [])
        self.assertTrue(any("回落" in text for text in warnings))

    def test_bad_strftime_is_an_error(self):
        errors, _ = validate_ops([op("date_format", date_format="%Q")])
        self.assertTrue(errors)

    def test_clean_config_has_no_findings(self):
        ops = [
            op("trim", field="f"),
            op("cast", field="f", value_kind="int"),
            op("dedupe", subset=["f"], scope="task"),
        ]
        self.assertEqual(validate_ops(ops, ["f"]), ([], []))


if __name__ == "__main__":
    unittest.main()

class ColumnFilterTests(unittest.TestCase):
    def test_text_conditions_and_whole_row_removal(self):
        rows = [['Alpha', 1], ['alphabet', 2], ['Beta', 3], [None, 4], ['', 5]]
        for condition, value, expected in [('equals','alpha',[1]), ('contains','PH',[1,2]), ('starts_with','al',[1,2]), ('ends_with','TA',[3])]:
            with self.subTest(condition=condition):
                got, ctx = run([op('filter', condition=condition, value=value, ignore_case=True)], rows, ('f','id'))
                self.assertEqual([row[1] for row in got], expected)
                self.assertEqual(sum(counts(ctx, ':dropped').values()), 5-len(expected))
                removed, _ = run([op('filter', condition=condition, value=value, ignore_case=True, filter_action='drop')], rows, ('f','id'))
                self.assertEqual(len(removed), 5-len(expected))

    def test_empty_enum_numeric_and_precision(self):
        self.assertEqual(column([op('filter',condition='is_empty')],[None,'',' ',0]),[None,''])
        self.assertEqual(column([op('filter',condition='not_empty')],[None,'',' ',0]),[' ',0])
        self.assertEqual(column([op('filter',condition='in',filter_values=['A','2'],ignore_case=True)],['a',2,'b']),['a',2])
        for condition, expected in [('gt',['9007199254740993']),('gte',['9007199254740992','9007199254740993']),('lt',['2']),('lte',['2','9007199254740992'])]:
            self.assertEqual(column([op('filter',condition=condition,value='9007199254740992')],['2','9007199254740992','9007199254740993','bad',None,True,'NaN']),expected)

    def test_order_missing_field_and_config_validation(self):
        got, _ = run([op('trim'),op('filter',value='a'),op('filter',condition='gt',field='n',value='2')],[[' a ',3],['a',1],['b',4]],('f','n'))
        self.assertEqual(got,[['a',3]])
        got, ctx = run([op('filter',field='missing',value='a')],[['b']])
        self.assertEqual(got,[['b']])
        self.assertTrue(counts(ctx,':field_missing'))
        for kwargs in [dict(field=''),dict(value=''),dict(condition='in'),dict(condition='gt',value='bad'),dict(condition='gt',value='Infinity')]:
            with self.assertRaises(ValueError):
                op('filter',**kwargs)
