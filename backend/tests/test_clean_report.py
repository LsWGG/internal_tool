"""clean_report 的测试。

这个文件里最重要的不是「数字对不对」，而是**表格有没有被值撑破**：报告的核心是一堆
Markdown 表格，而表格里的值来自用户数据（含 `|`、换行）与用户提示词（含反引号、代码）。
一旦某个值里的 `|` 没转义，那一行的所有列都会右移一格 —— 报告仍然渲染得出来，只是
**每个数字都错位了**，比直接报错危险得多。所以下面用 `parse_tables()` 把渲染结果
反解回单元格矩阵，断言每个表格都是规整的矩形。
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime
from typing import get_args

from app.clean_models import CleanOp, CleanTaskConfig, ConstraintKind
from app.clean_report import (
    _CONSTRAINT_LABELS,
    _OP_LABELS,
    REPORT_FORMAT_VERSION,
    FileReport,
    ReportInput,
    build_report,
    fmt_int,
    fmt_pct,
    fmt_size,
    md_code,
    md_table,
    md_text,
    redact,
    report_filename,
)
from app.clean_stats import Counters, StatsSet


# =========================================================================== 工具


def config(**overrides) -> CleanTaskConfig:
    """一个最小可用配置：两个字段，一个带约束，一个由大模型生成。"""
    base = {
        "name": "测试任务",
        "source": {"mode": "upload", "upload_id": "u1"},
        "fields": [
            {"dest": "姓名", "source": "name"},
            {
                "dest": "性别",
                "source": "gender",
                "constraints": [{"kind": "enum", "values": ["男", "女"]}],
                "fallback": {"on_violation": "keep"},
            },
        ],
        "ops": [{"op": "trim", "field": "姓名"}],
    }
    base.update(overrides)
    return CleanTaskConfig(**base)


def input_with(**overrides) -> ReportInput:
    data = {
        "config": config(),
        "task_id": "t-1",
        "status": "completed",
        "started_at": datetime(2026, 9, 26, 10, 0, 0),
        "finished_at": datetime(2026, 9, 26, 10, 1, 30),
    }
    data.update(overrides)
    return ReportInput(**data)


_TABLE_ROW = re.compile(r"^\|.*\|\s*$")


def parse_tables(text: str) -> list[list[list[str]]]:
    """把 Markdown 里的表格反解成 [表][行][单元格]。

    按 `\\|` 转义切分 —— 这正是被测的行为：如果转义写错了，这里就会多出一个单元格，
    断言随之失败。围栏代码块里的内容跳过（提示词里可能有 `|`）。
    """
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    in_fence = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(line):
            body = line.strip()[1:-1]
            cells = re.split(r"(?<!\\)\|", body)
            current.append([c.strip() for c in cells])
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def assert_tables_rectangular(case: unittest.TestCase, text: str) -> None:
    for index, table in enumerate(parse_tables(text)):
        widths = {len(row) for row in table}
        case.assertEqual(
            len(widths),
            1,
            f"第 {index + 1} 张表的列数不一致：{sorted(widths)}\n"
            + "\n".join(" | ".join(r) for r in table[:6]),
        )


def stats(seed: int = 7) -> StatsSet:
    return StatsSet(limit=10, reservoir_size=200, seed=seed)


class ReportTestCase(unittest.TestCase):
    """报告很长，断言失败时默认会打印整篇 —— 截断到能定位问题即可。"""

    maxDiff = 1500


def find_row(text: str, headers: list[str], key_header: str, key: str) -> list[str]:
    """按表头定位一张表，再按关键列取到那一行。找不到就返回空列表。

    断言表格内容要用它，而不是 `assertIn("| 8 |", text)` —— 后者会被别的表里的
    同一个数字蒙混过关。
    """
    def is_separator(row: list[str]) -> bool:
        return all(set(cell) == {"-"} for cell in row)

    for table in parse_tables(text):
        if not table or table[0] != headers:
            continue
        index = headers.index(key_header)
        for row in table[1:]:
            if is_separator(row):
                continue
            if len(row) == len(headers) and row[index] == key:
                return row
    return []


def section(text: str, number: int) -> str:
    """第 N 节的正文。同一个词在别的章节里也会出现（例如第 10 节那张配置摘要表里
    永远有一行「断点续跑」），整篇 `assertIn` / `assertNotIn` 分不清是谁说的。"""
    for part in text.split("\n## "):
        if part.startswith(f"{number}. "):
            return part
    raise AssertionError(f"报告里没有第 {number} 节")


def table_with_headers(text: str, headers: list[str]) -> list[list[str]]:
    """按表头取出整张表的数据行（去掉分隔行）。找不到就返回空列表。"""
    for table in parse_tables(text):
        if table and table[0] == headers:
            return [
                row
                for row in table[1:]
                if not all(set(cell) == {"-"} for cell in row)
            ]
    return []


# =========================================================================== 转义


class EscapingTests(ReportTestCase):
    def test_pipe_is_escaped(self):
        self.assertEqual(md_text("a|b"), "a\\|b")

    def test_newlines_become_marker(self):
        self.assertEqual(md_text("a\r\nb"), "a⏎b")
        self.assertEqual(md_text("a\nb"), "a⏎b")
        self.assertEqual(md_text("a\rb"), "a⏎b")

    def test_none_and_backtick(self):
        self.assertEqual(md_text(None), "")
        # 反引号不做代码段包裹：在代码段里无法转义，反而会吞掉后面的内容
        self.assertEqual(md_code("a`b"), "a`b")
        self.assertEqual(md_code("ab"), "`ab`")

    def test_long_value_is_clamped(self):
        out = md_text("字" * 200)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.endswith("…"))

    def test_table_with_hostile_values_stays_rectangular(self):
        text = md_table(
            ["字段", "值", "说明"],
            [
                ["a", "x|y", "含竖线"],
                ["b", "多\n行", "含换行"],
                ["c", "` 反引号 `", "含反引号"],
                ["d", "|", "只有一个竖线"],
            ],
        )
        tables = parse_tables(text)
        self.assertEqual(len(tables), 1)
        for row in tables[0]:
            self.assertEqual(len(row), 3, row)

    def test_hostile_values_survive_the_full_report(self):
        data = input_with(
            files=[
                FileReport(
                    name="a|b.csv",
                    rows_in=3,
                    rows_out=3,
                    unmapped=("列|一", "列\n二"),
                )
            ],
            notes=["可疑值：a|b 与 c`d"],
        )
        text = build_report(data)
        assert_tables_rectangular(self, text)

    def test_no_block_level_html_is_emitted(self):
        """我们自己**不生产**任何 HTML 块。

        注意这里查的是「有没有以标签开头的独立行」，而不是「全文里有没有 `<`」：用户数据
        里带 `<br>` 是数据，markdown-it（html:false）会把它转义成文本，那是正确行为。
        真正的坑是在正文里写 `<details>` 想让内容折叠 —— 那会在页面上显示成一行字面文本。
        """
        text = build_report(rich_report_input())
        offenders = [
            line
            for line in text.split("\n")
            if re.match(r"\s*<[a-zA-Z/!]", line)
        ]
        self.assertEqual(offenders, [], f"报告里出现了 HTML 块：{offenders}")

    def test_html_in_user_data_is_passed_through_as_data(self):
        # 值是数据不是指令：它的尖括号要原样保留，由渲染层去转义
        text = build_report(
            input_with(
                files=[FileReport(name="<b>x</b>.csv", message="<br>换行")],
                notes=["提示 <details> 不能用"],
            )
        )
        self.assertIn("<b>x</b>.csv", text)
        self.assertIn("<details>", text)     # 在列表项里作为文本，不是块
        assert_tables_rectangular(self, text)


# =========================================================================== 脱敏


class RedactTests(ReportTestCase):
    def test_openai_style_key(self):
        self.assertEqual(redact("key is sk-abcdefghijklmn"), "key is ***")

    def test_bearer_token(self):
        self.assertEqual(redact("Authorization: Bearer abcdefghijkl"), "Authorization: Bearer ***")

    def test_assignment_forms_keep_the_key_name(self):
        # 保留键名，否则读者不知道被遮的是什么
        self.assertIn("api_key", redact('api_key="supersecretvalue"'))
        self.assertNotIn("supersecretvalue", redact('api_key="supersecretvalue"'))

    def test_known_secret_is_masked_even_without_a_key_shape(self):
        # 自建网关的 key 可能就是一串普通字母
        self.assertNotIn("plainvalue", redact("token=plainvalue", ["plainvalue"]))

    def test_short_secrets_are_ignored(self):
        # 短串替换会误伤正常文本（比如把 "1" 全换掉）
        self.assertEqual(redact("abc 1 def", ["1"]), "abc 1 def")

    def test_secret_in_prompt_never_reaches_the_report(self):
        data = input_with(
            config=config(
                fields=[
                    {"dest": "摘要", "source": "raw"},
                    {
                        "dest": "标题",
                        "generate": {
                            "kind": "llm",
                            "prompt": "用 sk-live-abcdefghijklmn 这个 key 处理 ${摘要}",
                        },
                    },
                ],
                llm={"endpoint_ids": ["ep1"]},
            ),
            secrets=["sk-live-abcdefghijklmn"],
        )
        text = build_report(data)
        self.assertNotIn("sk-live-abcdefghijklmn", text)
        self.assertIn("${摘要}", text)     # 其余内容照常保留


# =========================================================================== 结构


class StructureTests(ReportTestCase):
    def test_ten_sections_in_order(self):
        text = build_report(input_with())
        heads = re.findall(r"^## (\d+)\. ", text, re.M)
        self.assertEqual(heads, [str(i) for i in range(1, 11)])

    def test_report_is_markdown_and_mentions_format_version(self):
        text = build_report(input_with())
        self.assertEqual(text.count("# 文件清洗报告"), 1)
        self.assertIn(str(REPORT_FORMAT_VERSION), text)

    def test_empty_everything_still_renders(self):
        # 空任务也要出一份结构完整的报告：渲染器崩掉比报告难看糟糕得多
        minimal = CleanTaskConfig(
            source={"mode": "server_path", "paths": ["/data"]},
            fields=[{"dest": "a", "source": "a"}],
        )
        text = build_report(ReportInput(config=minimal))
        assert_tables_rectangular(self, text)
        self.assertIn("## 10. 复现信息与方法学", text)
        self.assertIn("没有配置字段约束", text)
        self.assertIn("没有配置字段生成规则", text)

    def test_all_tables_in_a_rich_report_are_rectangular(self):
        data = rich_report_input()
        assert_tables_rectangular(self, build_report(data))

    def test_status_and_resume_are_visible(self):
        text = build_report(input_with(status="interrupted", resumed=True))
        self.assertIn("已中断", text)
        self.assertIn("断点续跑", text)
        self.assertIn("续跑", text)


# =========================================================================== 内容


def rich_report_input() -> ReportInput:
    """一份「什么都有一点」的输入：违规、回退、降级、近似频次、未映射列、告警。"""
    before, after = stats(), stats(seed=11)
    # StatsSet.update 收的是**列**（列式存储）：data[i] 是 columns[i] 那一整列。
    # 传成「一行一个 list」会静默取到前两行当两列 —— 数字看着正常，其实是错位的。
    before.update(
        ["姓名", "性别"],
        [
            [" 张三", "李四 ", "", "王|五", "赵六"],
            ["男", "男", "未知", "女", "男"],
        ],
    )
    after.update(
        ["姓名", "性别", "摘要"],
        [
            ["张三", "李四", "", "王|五", "赵六"],
            ["男", "男", "男", "女", "男"],
            ["摘要一", "摘要二", "摘要三", "摘要四", "摘要五"],
        ],
    )
    counters = Counters()
    counters.note_violation("姓名", "non_null")
    counters.note_violation("性别", "enum")
    counters.note_violation("性别", "enum")
    counters.note_fallback("性别", "enum", "keep", degraded=True)
    counters.note_fallback("性别", "enum", "keep", degraded=True)
    counters.note_fallback("姓名", "non_null", "truncate", degraded=False)
    counters.llm_calls, counters.llm_failures, counters.llm_cache_hits = 12, 1, 3

    # 复检结果是 姓名 还剩一个非空违规（输出里那个空串）—— 与上面的数据对得上
    after_counters = Counters()
    after_counters.note_violation("姓名", "non_null")

    return input_with(
        config=config(
            fields=[
                {"dest": "姓名", "source": "name"},
                {
                    "dest": "性别",
                    "source": "gender",
                    "constraints": [{"kind": "enum", "values": ["男", "女"]}],
                    "fallback": {"on_violation": "keep"},
                },
                {
                    "dest": "摘要",
                    "source": "raw",
                    "constraints": [
                        {"kind": "length", "max_length": 200},
                        {"kind": "regex", "pattern": "^.{5,}$", "severity": "warn"},
                    ],
                    "generate": {"kind": "llm", "prompt": "总结 ${姓名}", "batch_size": 5},
                },
            ],
            llm={"endpoint_ids": ["ep1"]},
        ),
        files=[
            FileReport(
                name="客户.csv",
                size=2_500_000,
                rows_in=5,
                rows_out=5,
                parse_errors=1,
                ragged_rows=2,
                replacement_chars=7,
                seconds=12.5,
                column_map={"name": "姓名", "gender": "性别"},
                unmapped=("备注",),
            )
        ],
        before=before,
        after=after,
        counters=counters,
        after_counters=after_counters,
        ops_counters={"0:trim:changed": 2, "0:trim:failed": 1},
        notes=["文件 b.csv 的列 #3 未映射"],
        fallback_samples=[
            {
                "file": "客户.csv",
                "row": 3,
                "field": "性别",
                "kind": "enum",
                "policy": "keep",
                "detail": "不在枚举内",
                "value": "未知",
                "result": "男",
                "degraded": True,
            }
        ],
        regression=["- 姓名列空值率由 20.00% 降至 20.00%（合法空值）"],
        llm={
            "calls": 12,
            "cache_hits": 3,
            "failures": 1,
            "per_field": {"摘要": {"calls": 12, "cache_hits": 3, "failures": 1}},
            "endpoints": [
                {"id": "ep1", "name": "主", "model": "gpt-x", "ok": 11, "failed": 1,
                 "latency_ms": 812.4}
            ],
        },
        secrets=["sk-should-not-appear"],
    )


class ContentTests(ReportTestCase):
    def test_summary_reports_before_and_after_counts(self):
        text = build_report(rich_report_input())
        self.assertIn("约束违规：清洗前 **3** 处 → 清洗后 **1** 处", text)
        self.assertIn("触发回退 **3** 次", text)
        self.assertIn("**2** 次降级", text)
        self.assertIn("大模型调用 **12** 次", text)

    def test_missing_recheck_is_not_reported_as_pass(self):
        # 这是最容易骗人的一处：没复检却显示 0 会被读成「全部通过」
        data = input_with(notes=[])
        data.after_counters = None
        text = build_report(data)
        self.assertIn("未复检", text)
        self.assertIn("不是「通过」", text)
        self.assertNotIn("**全部通过**", text)

    def test_passing_recheck_says_so(self):
        data = input_with()
        data.after_counters = Counters()
        text = build_report(data)
        self.assertIn("**全部通过**", text)

    def test_constraint_table_has_the_core_columns(self):
        text = build_report(rich_report_input())
        self.assertIn("清洗前违规", text)
        self.assertIn("清洗后违规", text)
        self.assertIn("回退次数", text)
        self.assertIn("回退策略", text)
        self.assertIn("示例", text)
        # enum 约束在报告里要是中文标签，且带枚举值
        self.assertIn("枚举（男/女）", text)
        # warn 级必须能一眼分辨
        self.assertIn("仅告警", text)

    def test_unmapped_columns_are_listed_per_file(self):
        text = build_report(rich_report_input())
        self.assertIn("未映射进输出的源列", text)
        self.assertIn("备注", text)

    def test_parse_errors_and_encoding_damage_are_explained(self):
        text = build_report(rich_report_input())
        self.assertIn("解析错误", text)
        self.assertIn("替换字符", text)
        self.assertIn("编码猜错了", text)

    def test_op_counters_are_reported_per_rule(self):
        text = build_report(rich_report_input())
        self.assertIn("去首尾空白", text)
        self.assertIn("清洗规则（按顺序执行）", text)

    def test_llm_exhaustion_and_abort_are_surfaced(self):
        data = rich_report_input()
        data.llm = {
            "calls": 50000,
            "exhausted": True,
            "aborted": True,
            "abort_error_rate": 0.5,
            "endpoints": [],
        }
        text = build_report(data)
        self.assertIn("调用次数已达上限", text)
        self.assertIn("错误率超过阈值", text)

    def test_notes_are_listed(self):
        text = build_report(rich_report_input())
        self.assertIn("文件 b.csv 的列 #3 未映射", text)


class MethodologyTests(ReportTestCase):
    def test_not_computed_statistics_are_declared(self):
        text = build_report(rich_report_input())
        self.assertIn("中位数、p99、精确去重计数", text)
        self.assertIn("不做", text)

    def test_approximate_frequency_is_flagged_with_a_bound(self):
        data = input_with()
        data.before = stats()
        data.after = stats()
        # 注意 update 收的是**列**（列式存储），一个列表就是一整列
        data.before.update(["姓名"], [[f"v{i}" for i in range(60)]])   # 基数 60 > limit 10
        data.after.update(["姓名"], [["x"]])
        text = build_report(data)
        self.assertIn("的频次为近似值", text)
        self.assertIn("高估", text)
        # 上界 = N/limit + 1 = 60/10 + 1
        self.assertIn("7", text)

    def test_exact_frequency_is_not_flagged(self):
        data = input_with()
        data.before = stats()
        data.after = stats()
        data.before.update(["姓名"], [["x", "y", "x"]])
        data.after.update(["姓名"], [["x"]])
        text = build_report(data)
        # 用告警句而不是 "Space-Saving" 判断：方法学表里本来就解释了 Space-Saving 是什么
        self.assertNotIn("的频次为近似值", text)

    def test_resume_is_disclosed_as_changing_approximate_stats(self):
        data = rich_report_input()
        data.resumed = True
        text = build_report(data)
        self.assertIn("分位数与 Top-K 可能与不中断运行不同", text)
        # 同时要说明产物本身不受影响，否则用户会以为续跑不可信
        self.assertIn("逐字节一致", text)

    def test_resume_note_only_appears_for_resumed_runs(self):
        text = build_report(rich_report_input())
        self.assertNotIn("分位数与 Top-K 可能与不中断运行不同", text)

    def test_reservoir_sample_size_is_declared(self):
        text = build_report(rich_report_input())
        self.assertIn("分位数样本量", text)

    def test_compare_table_labels_fields_with_no_before_side(self):
        # 生成字段的「清洗前」本来就不存在；一行破折号会被读成「统计失败了」
        data = rich_report_input()
        text = build_report(data)
        compare = ["字段", "空值率", "长度区间", "长度均值", "分位 p50 / p95", "数值均值",
                   "标准差", "类型分布（前 → 后）"]
        self.assertEqual(find_row(text, compare, "字段", "摘要")[1], "生成字段")

    def test_compare_table_marks_a_field_with_no_data_at_all(self):
        data = input_with(
            config=config(fields=[{"dest": "空字段", "source": "missing"}])
        )
        data.before = stats()
        data.after = stats()
        text = build_report(data)
        self.assertIn("无数据", find_row(
            text,
            ["字段", "空值率", "长度区间", "长度均值", "分位 p50 / p95", "数值均值",
             "标准差", "类型分布（前 → 后）"],
            "字段",
            "空字段",
        )[1])

    def test_compare_table_covers_every_configured_field_in_config_order(self):
        data = rich_report_input()
        text = build_report(data)
        compare = ["字段", "空值率", "长度区间", "长度均值", "分位 p50 / p95", "数值均值",
                   "标准差", "类型分布（前 → 后）"]
        ordered = [
            row[0]
            for table in parse_tables(text)
            if table and table[0] == compare
            for row in table[2:]
        ]
        self.assertEqual(ordered, ["姓名", "性别", "摘要"])


class CounterAggregationTests(ReportTestCase):
    OP_HEADERS = ["#", "规则", "目标", "改动格数", "丢弃行", "转换失败", "未执行", "缺列跳过"]

    def test_each_slot_shows_its_own_counters(self):
        data = input_with(
            config=config(
                ops=[
                    {"op": "trim", "field": "姓名"},
                    {"op": "lower", "field": "姓名"},
                    {"op": "trim", "field": "性别"},
                ]
            ),
            ops_counters={
                "0:trim:changed": 1,
                "1:lower:changed": 3,
                "2:trim:changed": 5,
                "2:trim:failed": 2,
            },
        )
        text = build_report(data)
        self.assertEqual(find_row(text, self.OP_HEADERS, "#", "1")[3], "1")
        self.assertEqual(find_row(text, self.OP_HEADERS, "#", "2")[3], "3")
        row = find_row(text, self.OP_HEADERS, "#", "3")
        self.assertEqual(row[3], "5")
        self.assertEqual(row[5], "2")

    def test_slot_prefix_does_not_swallow_two_digit_slots(self):
        # 槽位 1 的计数不能被槽位 12 吃进来（这就是 startswith 而不是 contains 的理由）
        data = input_with(
            config=config(
                ops=[{"op": "trim", "field": "姓名"}] + [{"op": "trim", "field": "姓名"}]
                * 11
            ),
            ops_counters={"1:trim:changed": 7, "12:trim:changed": 999},
        )
        text = build_report(data)
        self.assertEqual(find_row(text, self.OP_HEADERS, "#", "2")[3], "7")
        self.assertNotIn("999", text)

    def test_counters_for_a_slot_that_is_not_configured_are_ignored(self):
        # 配置里只有 1 条规则，却来了槽位 9 的计数（多半是配置被改过而 state 是旧的）：
        # 不能凭空多出一行，也不能把别人的数字算到第 1 条上
        data = input_with(ops_counters={"9:trim:changed": 4})
        text = build_report(data)
        self.assertEqual(len(parse_tables(text)[1]), 3)   # 表头 + 分隔行 + 1 条规则
        self.assertEqual(find_row(text, self.OP_HEADERS, "#", "1")[3], "0")

    def test_zero_changes_still_shows_zero_not_a_dash(self):
        # 0 和「没有这一项」在表里长得一样，所以非改动计数用 — 表示「不存在」，
        # 改动格数用 0（那个 0 有信息量：规则跑了，但没改到任何东西）
        data = input_with(ops_counters={"0:trim:changed": 0})
        text = build_report(data)
        row = find_row(text, self.OP_HEADERS, "#", "1")
        self.assertEqual(row[3], "0")
        self.assertEqual(row[4:], ["—", "—", "—", "—"])

    def test_ffill_filled_count_is_reported_as_changes(self):
        # ffill 把「填了几个格」记在 filled 上；不并进来的话它会永远显示 0，
        # 看起来像规则没生效
        data = input_with(
            config=config(ops=[{"op": "ffill", "field": "姓名"}]),
            ops_counters={"0:ffill:filled": 12},
        )
        text = build_report(data)
        self.assertEqual(find_row(text, self.OP_HEADERS, "#", "1")[3], "12")

    def test_deduped_rows_are_visible_in_the_rule_table(self):
        # 去重是唯一会静默少行的规则；只看第 2 节的行数差会不知道是谁干的
        data = input_with(
            config=config(ops=[{"op": "dedupe", "subset": ["性别"]}]),
            ops_counters={"0:dedupe:dropped": 31},
        )
        text = build_report(data)
        row = find_row(text, self.OP_HEADERS, "#", "1")
        self.assertEqual(row[2], "性别（文件内）")
        self.assertEqual(row[4], "31")

    def test_regex_template_errors_count_as_not_executed(self):
        data = input_with(
            config=config(
                ops=[
                    {"op": "regex_replace", "field": "姓名", "pattern": r"\d+",
                     "replacement": "N"}
                ]
            ),
            ops_counters={"0:regex_replace:template_error": 2, "0:regex_replace:skipped": 3},
        )
        text = build_report(data)
        self.assertEqual(find_row(text, self.OP_HEADERS, "#", "1")[6], "5")

    def test_unknown_op_name_falls_back_to_its_slug(self):
        data = input_with(ops_counters={})
        data.config = config(ops=[{"op": "slice", "field": "姓名", "start": 0, "end": 1}])
        text = build_report(data)
        self.assertIn("截取子串", text)
        self.assertIn("姓名", text)

    def test_dedupe_target_shows_scope_and_subset(self):
        data = input_with(
            config=config(ops=[{"op": "dedupe", "subset": ["姓名"], "scope": "task"}])
        )
        text = build_report(data)
        self.assertIn("姓名（任务内）", text)

    def test_dedupe_without_subset_says_whole_row(self):
        data = input_with(config=config(ops=[{"op": "dedupe", "scope": "task"}]))
        self.assertIn("整行（任务内）", build_report(data))

    def test_every_op_and_constraint_kind_has_a_chinese_label(self):
        """新增算子/约束时忘了加标签，报告里就会出现 `number_format` 这样的英文 slug。

        这不会报错、也不会让表格破损，只会让报告读起来像半成品 —— 所以用一条测试把
        「标签表必须覆盖模型枚举」这件事钉住。
        """
        ops = set(get_args(CleanOp.model_fields["op"].annotation))
        self.assertEqual(sorted(ops - set(_OP_LABELS)), [], "算子缺中文标签")

        kinds = set(get_args(ConstraintKind))
        self.assertEqual(sorted(kinds - set(_CONSTRAINT_LABELS)), [], "约束缺中文标签")

    def test_constraint_labels_are_chinese_in_the_table(self):
        data = input_with(
            config=config(
                fields=[
                    {
                        "dest": "年龄",
                        "source": "age",
                        "constraints": [
                            {"kind": "non_null"},
                            {"kind": "range", "min_value": 0, "max_value": 150},
                            {"kind": "unique", "unique_scope": "file"},
                            {"kind": "type", "value_kind": "int", "severity": "warn"},
                        ],
                    }
                ]
            )
        )
        text = build_report(data)
        for label in ("非空", "数值区间（≥0.0 ≤150.0）", "唯一（文件内）", "⚠ 类型（int）"):
            self.assertIn(label, text)


# ======================================================================= 报告明细条数

_FALLBACK_HEADERS = ["文件", "行", "字段", "违规", "策略", "说明", "原值", "结果", "降级"]
_SAMPLE_HEADERS = ["值", "清洗前次数", "清洗后次数"]


def fallback_samples(count: int) -> list[dict]:
    """条数可控的回退样本，字段与 `rich_report_input()` 里那条同形。"""
    return [
        {
            "file": "客户.csv",
            "row": index + 1,
            "field": "性别",
            "kind": "enum",
            "policy": "keep",
            "detail": "不在枚举内",
            "value": f"未知{index}",
            "result": "男",
            "degraded": False,
        }
        for index in range(count)
    ]


class DetailLevelTests(ReportTestCase):
    """「报告明细条数」（`output.top_k`）。

    几百万行的文件上，逐条说明会把报告本身淹掉。但这个旋钮必须**只影响报告里列多少行**：
    第 5 节的按组计数、第 7 节的统计口径、10 节的骨架都不受它影响 —— 否则它就成了
    「调小 = 少查」的假开关，而这正是不能给用户的东西。
    """

    def rich(self, top_k: int) -> ReportInput:
        data = rich_report_input()
        data.config.output.top_k = top_k
        return data

    def test_default_detail_count_is_eight(self):
        text = build_report(rich_report_input())
        self.assertIn("前 8 个值", text)

    def test_zero_detail_keeps_the_ten_sections(self):
        text = build_report(self.rich(0))
        heads = re.findall(r"^## (\d+)\. ", text, re.M)
        self.assertEqual(heads, [str(i) for i in range(1, 11)])
        assert_tables_rectangular(self, text)

    def test_zero_detail_drops_both_detail_tables(self):
        text = build_report(self.rich(0))
        self.assertEqual(table_with_headers(text, _FALLBACK_HEADERS), [])
        self.assertEqual(table_with_headers(text, _SAMPLE_HEADERS), [])
        self.assertIn("报告明细条数 = 0", text)

    def test_zero_detail_keeps_the_exact_counts(self):
        # 不列明细 ≠ 不算明细：按组计数仍然精确，摘要里的总数也不变
        text = build_report(self.rich(0))
        row = find_row(text, ["字段", "违规类型", "策略", "次数"], "字段", "性别")
        self.assertEqual(row[-1], "2")
        self.assertIn("触发回退 **3** 次", text)

    def test_zero_detail_points_at_the_aggregate_section(self):
        text = build_report(self.rich(0))
        self.assertIn("前后统计对比", text)

    def test_detail_count_limits_fallback_rows(self):
        data = self.rich(3)
        data.fallback_samples = fallback_samples(5)
        text = build_report(data)
        rows = table_with_headers(text, _FALLBACK_HEADERS)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[6] for r in rows], ["未知0", "未知1", "未知2"])
        # 账要交清楚：内存里留了几条、这里列几条、剩下的在哪
        self.assertIn("内存里留下 5 条样本", text)
        self.assertIn("上限 200 条", text)
        self.assertIn("列前 3 条（报告明细条数 = 3）", text)
        self.assertIn("`fallback.jsonl`", text)

    def test_the_report_never_claims_the_download_is_complete(self):
        """`fallback.jsonl` 写到 64 MB 就不再追加（丢弃计数连 state 都没进），所以
        「完整明细在下载包里」是一句谎话 —— 说它能说的那句：写到多少为止。"""
        data = self.rich(3)
        data.fallback_samples = fallback_samples(5)
        text = build_report(data)
        self.assertNotIn("完整明细", text)
        self.assertIn("写到 64 MB 为止", text)

    def test_zero_detail_still_says_where_the_detail_is(self):
        # 不列明细 ≠ 不给去处：这句话正是「0」这个设置能不能用的前提
        text = build_report(self.rich(0))
        self.assertIn("`fallback.jsonl`", text)

    def test_a_state_without_samples_still_explains_itself(self):
        """没有样本可列时（状态是旧版本写的，或这一份确实没留下），「有回退次数、
        却没有样本表」必须有一句话解释，否则就是一处静默遗漏 —— 而且不能把锅甩给
        「断点续跑」：续跑是**会**恢复样本的（`TaskProgress.snapshot` 里带着它）。"""
        data = self.rich(8)
        data.fallback_samples = []
        text = build_report(data)
        self.assertEqual(table_with_headers(text, _FALLBACK_HEADERS), [])
        row = find_row(text, ["字段", "违规类型", "策略", "次数"], "字段", "性别")
        self.assertEqual(row[-1], "2", "没有样本表，但计数表必须还在")
        self.assertIn("没有样本可列", section(text, 5))
        self.assertNotIn("断点续跑", section(text, 5))
        self.assertIn("`fallback.jsonl`", section(text, 5))

    def test_a_resumed_run_says_the_samples_may_predate_the_interruption(self):
        # 续跑恢复的是中断点前那份状态：样本条数照旧有界，但最早那几条属于中断前那一段。
        # 报告里不说这一句，读的人会以为这 8 条是连续采到的。
        data = self.rich(8)
        data.resumed = True
        data.fallback_samples = fallback_samples(3)
        text = build_report(data)
        self.assertEqual(len(table_with_headers(text, _FALLBACK_HEADERS)), 3)
        self.assertIn("本次是续跑，样本可能来自中断前那一段", section(text, 5))

    def test_detail_count_limits_values_per_field(self):
        # 每张值表是「清洗前后各取前 K 个」的并集 → 上限是 2K，不是 K
        data = self.rich(3)
        text = build_report(data)
        tables = [
            t for t in parse_tables(text) if t and t[0] == _SAMPLE_HEADERS
        ]
        self.assertTrue(tables, "第 7 节应该至少有一张值表")
        widest = max(len(t) - 1 for t in tables)
        self.assertLessEqual(widest, 6)
        self.assertEqual(widest, 6, "上限应该真的生效（并集达到 2K）")
        self.assertFalse(
            any(row[1] == "—" and row[2] == "—" for t in tables for row in t[1:]),
            "并集里不该出现两侧都没计数的值",
        )

    def test_detail_count_above_what_exists_is_not_a_lie(self):
        # 要 8 条但内存里只有 2 条：账按实际条数报，不按设置值报
        data = self.rich(8)
        data.fallback_samples = fallback_samples(2)
        text = build_report(data)
        self.assertEqual(len(table_with_headers(text, _FALLBACK_HEADERS)), 2)
        self.assertIn("列前 2 条（报告明细条数 = 8）", text)


# =========================================================================== 小工具


class FormatTests(ReportTestCase):
    def test_md_table_without_rows(self):
        self.assertIn("（无数据）", md_table(["a"], []))

    def test_fmt_size(self):
        self.assertEqual(fmt_size(None), "—")
        self.assertEqual(fmt_size(512), "512 B")
        self.assertEqual(fmt_size(2048), "2.0 KB")
        self.assertEqual(fmt_size(5 * 1024**3), "5.0 GB")

    def test_fmt_int(self):
        self.assertEqual(fmt_int(1234567), "1,234,567")
        self.assertEqual(fmt_int(None), "—")

    def test_fmt_pct(self):
        self.assertEqual(fmt_pct(0.1234), "12.34%")
        self.assertEqual(fmt_pct(None), "—")

    def test_report_filename_is_safe(self):
        self.assertEqual(report_filename("t-1"), "report-t-1.md")
        self.assertEqual(report_filename("../../etc/passwd"), "report-etcpasswd.md")
        self.assertEqual(report_filename(""), "report-task.md")


if __name__ == "__main__":
    unittest.main()
