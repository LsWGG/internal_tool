"""Markdown 回归报告的生成。

报告是这一整套东西的**交付物**：任务跑完，用户看的就是它。所以这里有几条硬规则：

1. **只输出纯 Markdown，不出现任何 HTML**。前端用 markdown-it 且 `html: false`（原始 HTML
   一律转义），写一个 `<details>` 或 `<br>` 出来会在页面上显示成字面文本。要折叠用围栏代码块，
   要换行用列表或分段。
2. **每个单元格都要转义**（`|`、换行、反引号）。表格破损是这类报告最常见的 bug，而且破损
   之后读者看到的是**错位的数字** —— 比报错危险得多。
3. **近似的数字必须自己说清楚**。Top-K 溢出、分位数来自 2000 个样本、续跑后的采样流不同，
   这些都要写在数字旁边或方法学一节里。宁可写「不做统计」，也不要印一个用户会相信的假数字。
4. **密钥不进报告**。任务配置本身不含密钥（见 LlmEndpoint 的 api_key 排除），但提示词、
   错误消息、用户函数源码里都可能带着密钥，所以所有进报告的文本都过一遍 `redact()`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .clean_models import CleanTaskConfig, canonical_json
from .clean_state import FALLBACK_SAMPLE_LIMIT
from .clean_stats import TYPE_NAMES, Counters, FieldStats, StatsSet

REPORT_FORMAT_VERSION = 1
"""报告版式版本。断点续跑与产物打包都要能说清「这份报告是哪一版生成的」。"""

MAX_CELL = 60
"""单元格最大显示长度。提示词、错误消息动辄上千字符，塞进表格会毁掉整张表。"""


# =========================================================================== 脱敏


_SECRET_PATTERNS = (
    # OpenAI 风格的 key
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    # Authorization: Bearer xxx
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    # key=value / "key": "value" 形态。只遮值，保留键名，否则读者不知道被遮的是什么
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd)"
        r"(\s*[:=]\s*[\"']?)([^\s\"',;]{4,})"
    ),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """遮蔽文本里的密钥。`secrets` 是已知的密钥值（端点池里的），逐个整串替换。

    两步都要：正则挡住「形状像密钥」的东西，已知值列表挡住「形状不像但确实是密钥」的东西
    （自定义网关的 key 可能就是一串普通字母数字）。
    """
    result = text if isinstance(text, str) else str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(_mask_match, result)
    return result


def _mask_match(match: re.Match) -> str:
    groups = match.groups()
    if len(groups) == 1:                      # Bearer xxx
        return f"{groups[0]} ***"
    if len(groups) == 3:                      # key = value
        return f"{groups[0]}{groups[1]}***"
    return "***"


# =========================================================================== 表格


def md_text(value: Any) -> str:
    """单元格文本：转义 `|`、折叠换行。**不做反引号包裹** —— 值里出现反引号时用代码样式
    在 Markdown 里没有可靠写法（代码段内不做转义），宁可纯文本。"""
    text = "" if value is None else str(value)
    if len(text) > MAX_CELL:
        text = text[: MAX_CELL - 1] + "…"
    return (
        text.replace("\r\n", "⏎").replace("\n", "⏎").replace("\r", "⏎").replace("|", "\\|")
    )


def md_code(value: Any) -> str:
    """代码样式的单元格。值里含反引号时退回纯文本转义。"""
    text = "" if value is None else str(value)
    if "`" in text:
        return md_text(text)
    return f"`{md_text(text)}`"


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "（无数据）\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_text(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return md_text(value)


def fmt_size(size: int | None) -> str:
    if size is None:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def fmt_float(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def fmt_pct(ratio: float | None) -> str:
    return "—" if ratio is None else f"{ratio * 100:.2f}%"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分"
    return f"{seconds / 3600:.1f} 小时"


def _arrow(before: str, after: str) -> str:
    return before if before == after else f"{before} → {after}"


# =========================================================================== 输入


@dataclass
class FileReport:
    """单个文件的处理结果。engine 边跑边填。"""

    name: str
    size: int = 0
    status: str = "completed"
    rows_in: int = 0
    rows_out: int = 0
    dropped: int = 0
    parse_errors: int = 0
    ragged_rows: int = 0
    replacement_chars: int = 0
    seconds: float = 0.0
    message: str = ""
    column_map: Mapping[str, str] = field(default_factory=dict)
    """源列 → 目的字段。多文件表头不一致时每个文件的映射都可能不同，所以映射按文件存。"""
    unmapped: tuple[str, ...] = ()
    """这个文件里没有映射进任何目的字段的源列。"哪些列没被映射" 是按文件才有意义的：
    union 布局下 A 文件缺的列可能在 B 文件里有。"""


@dataclass
class ReportInput:
    config: CleanTaskConfig
    task_id: str = ""
    status: str = "completed"
    files: list[FileReport] = field(default_factory=list)
    before: StatsSet | None = None
    after: StatsSet | None = None
    counters: Counters | None = None
    """正向清洗的计数。其中的 `violations` 是**清洗前**的违规 —— 即对**读入值**做的检查，
    `fallbacks` 是回退次数。这个分工要守住：一旦正向清洗也把回退后的值计进 `violations`，
    报告里的「清洗前/清洗后」就变成同一个数被复述两遍，看着像对比其实不是。"""
    after_counters: Counters | None = None
    """回归复检的计数（对**输出值**跑同一套约束的结果）。与 `counters` 分开是有意的：
    同一套约束求值两次、两个计数器各自累加，两个数字才能直接相减。两侧都按输出行序喂值
    —— 输入序列相同，结果必然相同。`None` 表示没复检，报告会写「未复检」而不是 0。"""
    ops_counters: Mapping[str, int] = field(default_factory=dict)
    notes: Sequence[str] = ()
    fallback_samples: Sequence[Mapping[str, Any]] = ()
    regression: Sequence[str] = ()
    llm: Mapping[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    resumed: bool = False
    secrets: Sequence[str] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


# =========================================================================== 报告


def build_report(data: ReportInput) -> str:
    """按 10 节的版式生成 Markdown。"""

    def g(key: str, default: Any = 0) -> Any:
        return data.extra.get(key, default)

    lines: list[str] = []
    title = data.config.name or data.task_id or "清洗任务"
    lines.append(f"# 文件清洗报告：{title}")
    lines.append("")
    lines.append(
        f"> 任务 `{md_text(data.task_id)}` · 状态 **{_status_label(data.status)}**"
        f" · 生成于 {md_text(_now())}"
        + (" · **本次为断点续跑**" if data.resumed else "")
    )
    lines.append("")
    for section in (
        _section_summary,
        _section_files,
        _section_mapping,
        _section_constraints,
        _section_fallback,
        _section_compare,
        _section_samples,
        _section_generated,
        _section_regression,
        _section_repro,
    ):
        lines.append(section(data))
    text = "\n".join(lines)
    # 最后再整体过一遍脱敏：任何一节里漏出的密钥都在这里被兜住
    return redact(text, data.secrets)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_STATUS_LABELS = {
    "completed": "已完成",
    "failed": "失败",
    "interrupted": "已中断（可从上次提交点续跑）",
    "running": "运行中",
    "queued": "排队中",
    "paused": "已暂停",
    "cancelled": "已取消",
}


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


# --------------------------------------------------------------------------- 1


def _section_summary(data: ReportInput) -> str:
    counters = data.counters or Counters()
    rows_in = sum(f.rows_in for f in data.files)
    rows_out = sum(f.rows_out for f in data.files)
    dropped = sum(f.dropped for f in data.files)
    errors = sum(f.parse_errors for f in data.files)
    before_total = counters.total_violations()
    after_total = _after(data).total_violations() if data.after_counters else None
    lines = ["## 1. 结论", ""]
    bullets = [
        f"处理文件 **{len(data.files)}** 个，读入 **{fmt_int(rows_in)}** 行，"
        f"输出 **{fmt_int(rows_out)}** 行"
        + (f"，清洗过程丢弃 **{fmt_int(dropped)}** 行" if dropped else ""),
        "约束违规：清洗前 **{}** 处 → 清洗后 {}".format(
            fmt_int(before_total),
            f"**{fmt_int(after_total)}** 处"
            if after_total is not None
            else "**未复检**（本次没有对输出值复查）",
        ),
        f"触发回退 **{fmt_int(counters.total_fallbacks())}** 次"
        + (f"，其中 **{fmt_int(counters.degraded)}** 次降级（回退无法完成，保持了原值）"
           if counters.degraded else ""),
    ]
    if counters.llm_calls:
        bullets.append(
            f"大模型调用 **{fmt_int(counters.llm_calls)}** 次"
            f"（缓存命中 {fmt_int(counters.llm_cache_hits)} 次，"
            f"失败 {fmt_int(counters.llm_failures)} 次）"
        )
    bullets.append(
        "处理解析错误 "
        + (f"**{fmt_int(errors)}** 行（已跳过并计数，见第 2 节）" if errors else "0 行")
    )
    if data.notes:
        bullets.append(f"运行期告警/提示 **{len(data.notes)}** 条（见第 5 节）")
    for bullet in bullets:
        lines.append(f"- {bullet}")
    lines.append("")
    lines.append(
        "> 判读提示：「清洗后违规」是拿**同一套约束**对输出值再复查一遍的结果，"
        "包含回退产生的新值。回退产生的值**不会**再对其它约束复检（否则一次回退可能引出"
        "无限次重试），所以某个字段的清洗后违规不为 0 时，先看第 4 节的回退策略列。"
    )
    lines.append("")
    return "\n".join(lines)


def _after(data: ReportInput) -> Counters:
    """清洗后的违规计数。没做回归复检时返回空计数器 —— 报告会显示 0，但第 9 节会说明
    复检是否真的跑过，避免把「没查」读成「通过」。"""
    return data.after_counters or Counters()


# --------------------------------------------------------------------------- 2


def _section_files(data: ReportInput) -> str:
    lines = ["## 2. 文件明细", ""]
    rows = []
    for item in data.files:
        rows.append(
            [
                item.name,
                fmt_size(item.size),
                fmt_int(item.rows_in),
                fmt_int(item.rows_out),
                fmt_int(item.dropped) if item.dropped else "0",
                fmt_int(item.parse_errors) if item.parse_errors else "0",
                fmt_int(item.ragged_rows) if item.ragged_rows else "0",
                fmt_int(item.replacement_chars) if item.replacement_chars else "0",
                fmt_duration(item.seconds),
                _status_label(item.status),
                item.message,
            ]
        )
    lines.append(
        md_table(
            [
                "文件", "大小", "读入行", "输出行", "丢弃", "解析错误",
                "参差行", "替换字符", "耗时", "状态", "备注",
            ],
            rows,
        )
    )
    lines.append("")
    lines.append(
        "**解析错误**是记录读不出来（CSV 坏行、xlsx 参差行），计数后跳过；"
        "**参差行**是字段数与表头不一致（按表头宽度补齐/截断，精确计数）；"
        "**替换字符**是解码时无法还原的字节数（U+FFFD）—— 非零通常意味着编码猜错了，"
        "建议显式指定编码后重跑。"
    )
    lines.append("")
    if any(item.unmapped for item in data.files):
        lines.append("未映射进输出的源列（按文件）：")
        lines.append("")
        for item in data.files:
            if item.unmapped:
                lines.append(
                    f"- `{md_text(item.name)}`：{md_text('、'.join(item.unmapped))}"
                )
        lines.append("")
        lines.append(
            "这些列被读进来了但没有进输出 —— 确认是有意丢掉的，还是第 3 节的映射漏配了。"
        )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 3


def _section_mapping(data: ReportInput) -> str:
    lines = ["## 3. 字段映射与清洗规则", ""]
    config = data.config
    rows = []
    for spec in config.fields:
        generate = ""
        if spec.generate:
            rule = spec.generate
            if rule.kind == "llm":
                generate = f"大模型（batch={rule.batch_size}）"
            elif rule.kind == "random":
                generate = f"随机（{rule.generator}）"
            elif rule.kind == "sequence":
                generate = f"序列（{rule.start} 起，步长 {rule.step}）"
            else:
                generate = f"表达式（{md_text(rule.expression)}）"
        elif spec.source:
            generate = "直接映射"
        else:
            generate = "新列（无来源）"
        constraints = "、".join(_constraint_label(c) for c in spec.constraints) or "—"
        fallback = "—"
        if spec.fallback:
            policy = {"keep": "保留原值", "retry": "重新生成", "truncate": "截断/修正"}[
                spec.fallback.on_violation
            ]
            fallback = policy
            if spec.fallback.on_violation == "retry":
                fallback += f"（最多 {spec.fallback.max_retries} 次）"
        rows.append(
            [
                spec.dest,
                spec.source or "（新列）",
                generate,
                constraints,
                fallback,
            ]
        )
    lines.append(md_table(["目的字段", "来源列", "取值方式", "约束", "回退策略"], rows))
    lines.append("")

    if config.ops:
        lines.append("### 清洗规则（按顺序执行）")
        lines.append("")
        op_rows = []
        for slot, op in enumerate(config.ops):
            # ffill 把「填了几个格」记在 `filled` 而不是 `changed`（它填的确实是空值，
            # 但填完值就变了）。报告只关心「这条规则动了多少格」，所以两者相加 ——
            # 否则 ffill 会永远显示 0，看着像没生效。
            changed = _op_counter(data.ops_counters, slot, "changed", "filled")
            dropped = _op_counter(data.ops_counters, slot, "dropped")
            failed = _op_counter(data.ops_counters, slot, "failed")
            skipped = _op_counter(data.ops_counters, slot, "skipped", "template_error")
            missing = _op_counter(data.ops_counters, slot, "field_missing")
            op_rows.append(
                [
                    slot + 1,
                    _op_label(op),
                    _op_target(op),
                    fmt_int(changed),
                    fmt_int(dropped) if dropped else "—",
                    fmt_int(failed) if failed else "—",
                    fmt_int(skipped) if skipped else "—",
                    fmt_int(missing) if missing else "—",
                ]
            )
        lines.append(
            md_table(
                ["#", "规则", "目标", "改动格数", "丢弃行", "转换失败", "未执行", "缺列跳过"],
                op_rows,
            )
        )
        lines.append("")
        lines.append(
            "「改动格数」是值真的变了的格子数 —— 规则写对了但计数为 0，说明数据本来就干净"
            "（或者规则没生效，看后三列）。「未执行」是正则替换模板写错、值被整格跳过的次数；"
            "「丢弃行」只有去重与删空值行会产生，第 2 节的输出行数应当与它对应得上。"
        )
        lines.append("")
    return "\n".join(lines)


_CONSTRAINT_LABELS = {
    "non_null": "非空",
    "enum": "枚举",
    "regex": "正则",
    "length": "长度",
    "range": "数值区间",
    "format": "格式",
    "type": "类型",
    "unique": "唯一",
}


def _constraint_label(constraint: Any) -> str:
    kind = constraint.kind
    detail = ""
    if kind == "enum":
        detail = "/".join(constraint.values[:4])
        if len(constraint.values) > 4:
            detail += "…"
    elif kind == "regex":
        # 用 /…/ 包住模式，避免和这一层的括号套成一串读不断句的嵌套
        detail = f"/{constraint.pattern}/ {constraint.mode}"
    elif kind == "length":
        parts = []
        if constraint.min_length is not None:
            parts.append(f"≥{constraint.min_length}")
        if constraint.max_length is not None:
            parts.append(f"≤{constraint.max_length}")
        detail = " ".join(parts)
    elif kind == "range":
        parts = []
        if constraint.min_value is not None:
            parts.append(f"≥{constraint.min_value}")
        if constraint.max_value is not None:
            parts.append(f"≤{constraint.max_value}")
        detail = " ".join(parts)
    elif kind in ("format", "type"):
        detail = constraint.value_kind or ""
    elif kind == "unique":
        detail = "任务内" if constraint.unique_scope == "task" else "文件内"
    label = f"{_CONSTRAINT_LABELS.get(kind, kind)}{f'（{detail}）' if detail else ''}"
    # warn 级只计数不拦数据，报告里必须一眼能分辨，否则会被当成已经处理过的违规
    if constraint.severity == "warn":
        label = "⚠ " + label + "（仅告警）"
    return label


_OP_LABELS = {
    "trim": "去首尾空白",
    "lower": "转小写",
    "upper": "转大写",
    "nfkc": "NFKC 规范化",
    "replace": "字面替换",
    "regex_replace": "正则替换",
    "fill_null": "填空值",
    "ffill": "向下填充",
    "cast": "类型转换",
    "date_format": "日期格式化",
    "dedupe": "去重",
    "drop_null": "删除空值行",
    "filter": "按列过滤",
    "concat": "拼接列",
    "slice": "截取子串",
    "number_format": "数字格式化",
}


def _op_label(op: Any) -> str:
    return _OP_LABELS.get(op.op, op.op)


def _op_target(op: Any) -> str:
    if op.op == "filter":
        names = {"equals":"等于", "contains":"包含", "starts_with":"开头是", "ends_with":"结尾是", "in":"属于列表", "is_empty":"为空", "not_empty":"不为空", "gt":"大于", "gte":"大于等于", "lt":"小于", "lte":"小于等于"}
        value = "、".join(op.filter_values) if op.condition == "in" else ("" if op.condition in ("is_empty", "not_empty") else str(op.value))
        return f"{op.field} · {'保留' if op.filter_action == 'keep' else '删除'}命中行 · {names[op.condition]} {value}" + (" · 忽略大小写" if op.ignore_case else "")
    if op.op == "concat":
        return f"{'+'.join(op.fields)} → {op.dest}"
    if op.op == "dedupe":
        subset = "+".join(op.subset) or "整行"
        return f"{subset}（{'任务内' if op.scope == 'task' else '文件内'}）"
    if op.op == "drop_null":
        return "+".join(op.fields or [op.field])
    return op.field or "—"


def _op_counter(counters: Mapping[str, int], slot: int, *kinds: str) -> int:
    """汇总某个规则槽位下若干种计数的总和。

    键的形态是 `"{槽位}:{算子}:{种类}"`（见 `clean_ops.OpContext.count`）。加 `startswith`
    前缀是为了不让槽位 2 把槽位 12 的计数吃进来 —— 这正是那种会安静算错的 bug。
    """
    prefix = f"{slot}:"
    suffixes = tuple(":" + kind for kind in kinds)
    total = 0
    for key, value in counters.items():
        if key.startswith(prefix) and key.endswith(suffixes):
            total += int(value)
    return total


# --------------------------------------------------------------------------- 4


def _section_constraints(data: ReportInput) -> str:
    lines = ["## 4. 约束检查与回退", ""]
    counters = data.counters or Counters()
    after = _after(data)
    rows = []
    for spec in data.config.fields:
        if not spec.constraints:
            continue
        # 「清洗前违规」来自正向清洗时对**读入值**的检查，不是统计 —— 统计里没有违规数。
        old_count = counters.field_violations(spec.dest)
        new_count = after.field_violations(spec.dest)
        fallbacks = counters.field_fallbacks(spec.dest)
        fallback_total = sum(fallbacks.values())
        policy = "—"
        if spec.fallback:
            policy = spec.fallback.on_violation
        example = _example_for(data, spec.dest)
        rows.append(
            [
                spec.dest,
                "、".join(_constraint_label(c) for c in spec.constraints),
                # 「没有违规」和「没查过」在表格里长得一样，但含义天差地别：前者是结论，
                # 后者是缺数据。分开写。
                "未统计" if data.counters is None else fmt_int(sum(old_count.values())),
                "未复检" if data.after_counters is None else fmt_int(sum(new_count.values())),
                fmt_int(fallback_total) if fallback_total else "0",
                policy,
                example,
            ]
        )
    lines.append(
        md_table(
            ["字段", "约束", "清洗前违规", "清洗后违规", "回退次数", "回退策略", "示例"],
            rows,
        )
    )
    lines.append("")
    if not any(spec.constraints for spec in data.config.fields):
        lines.append("本次没有配置字段约束。")
    elif data.after_counters is None:
        lines.append(
            "> ⚠ **本次没有做回归复检**，所以「清洗后违规」一列显示「未复检」而不是 0。"
            "任务可能是被中断的，或者复检被显式关闭了。"
        )
    else:
        lines.append(
            "> 「清洗前」统计的是**读入值**，与「清洗后」用同一套约束、同一个求值器、"
            "同一个喂值顺序，所以两个数字可以直接相减。`清洗前 - 清洗后` 是这套规则"
            "实际解决掉的违规数。"
        )
    lines.append("")
    return "\n".join(lines)


def _example_for(data: ReportInput, field_name: str) -> str:
    for sample in data.fallback_samples:
        if sample.get("field") == field_name:
            return f"{md_code(sample.get('value'))} → {md_code(sample.get('result'))}"
    return "—"


# --------------------------------------------------------------------------- 5


def _section_fallback(data: ReportInput) -> str:
    lines = ["## 5. 回退告警明细", ""]
    counters = data.counters or Counters()
    if not counters.fallbacks:
        lines.append("没有触发任何回退。")
        lines.append("")
        if data.notes:
            lines.extend(_notes_block(data.notes))
        return "\n".join(lines)

    rows = []
    for (field_name, kind, policy), count in sorted(
        counters.fallbacks.items(), key=lambda item: (-item[1], item[0])
    ):
        rows.append(
            [field_name, _CONSTRAINT_LABELS.get(kind, kind), policy, fmt_int(count)]
        )
    lines.append(md_table(["字段", "违规类型", "策略", "次数"], rows))
    lines.append("")
    lines.append(
        "**计数是精确的，明细是有界的**：GB 级数据上「每次回退打一行」等于给一条配错的规则"
        "写几百万行日志，日志本身就废了。所以日志只在每组（文件, 字段, 违规类型, 策略）"
        "首次出现、以及数量每上一个数量级时打一行。"
    )
    lines.append("")
    # 明细条数由配置决定（`output.top_k`，界面上是「报告明细条数」）。上面那张按组聚合的
    # 计数表**不受影响** —— 无论列不列样本，每一组的次数都是精确的。
    top_k = data.config.output.top_k
    total = sum(counters.fallbacks.values())
    # 逐条明细的唯一完整来源是下载包里的文件流；报告里这几行只是「看得见的那一截」。
    where = (
        "逐条明细（每触发一次回退一行，写到 64 MB 为止）在下载包的 `fallback.jsonl` 里。"
    )
    if top_k <= 0:
        lines.append(
            f"本次共触发回退 **{fmt_int(total)}** 次（上面的计数是精确的），"
            f"按设置未列样本明细（报告明细条数 = 0）。{where}"
        )
        lines.append("")
    elif data.fallback_samples:
        shown = list(data.fallback_samples)[:top_k]
        lines.append("### 回退样本")
        lines.append("")
        sample_rows = [
            [
                item.get("file", ""),
                item.get("row", ""),
                item.get("field", ""),
                _CONSTRAINT_LABELS.get(str(item.get("kind", "")), item.get("kind", "")),
                item.get("policy", ""),
                item.get("detail", ""),
                item.get("value", ""),
                item.get("result", ""),
                "是" if item.get("degraded") else "否",
            ]
            for item in shown
        ]
        lines.append(
            md_table(
                ["文件", "行", "字段", "违规", "策略", "说明", "原值", "结果", "降级"],
                sample_rows,
            )
        )
        lines.append("")
        # 总次数第 1 节的摘要里已经报过，这里只交代表里列了几条、为什么只有这些。
        lines.append(
            f"内存里留下 {fmt_int(len(data.fallback_samples))} 条样本（上限 "
            f"{fmt_int(FALLBACK_SAMPLE_LIMIT)} 条），这里按要求列前 "
            f"{fmt_int(len(shown))} 条（报告明细条数 = {fmt_int(top_k)}）"
            # 续跑时样本是从中断点前那份 state.json 恢复的，条数照旧有界，但「最早的
            # 几条」是中断前那一段的事件 —— 报告里不说这一句，读的人会以为样本是连续的。
            + ("；本次是续跑，样本可能来自中断前那一段。" if data.resumed else "；")
            + where
        )
        lines.append("")
    else:
        # 只有两种可能：状态是旧版本写的（那时快照里还没有这个键），或这次运行确实
        # 没留下。上面明明有回退次数、却没有样本表，不解释就是一处静默遗漏。
        lines.append(
            f"本次共触发回退 **{fmt_int(total)}** 次（上面的计数是精确的），但这次运行的"
            f"记录里没有样本可列（任务状态由旧版本写的不带样本，重跑一次就有了）。{where}"
        )
        lines.append("")
    if data.notes:
        lines.extend(_notes_block(data.notes))
    return "\n".join(lines)


def _notes_block(notes: Sequence[str]) -> list[str]:
    lines = ["### 运行期告警与提示", ""]
    for text in notes:
        lines.append(f"- {md_text(text)}")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- 6


def _section_compare(data: ReportInput) -> str:
    lines = ["## 6. 前后统计对比", ""]
    if data.before is None or data.after is None:
        lines.append("（本次运行没有采集到两侧统计）")
        lines.append("")
        return "\n".join(lines)
    names = _paired_fields(data)
    generated = set(data.config.llm_fields()) | {
        spec.dest for spec in data.config.fields if spec.generate
    }
    rows = []
    for name in names:
        old, new = data.before.field(name), data.after.field(name)
        rows.append(
            [
                name,
                # 一行全是「—」看不出是「整列都空」还是「这个字段根本没数据」。
                # 生成字段没有「清洗前」是设计使然，说清楚比留一列破折号好。
                _emptiness(old, new, generated=name in generated),
                _arrow(_length_range(old), _length_range(new)),
                _arrow(
                    fmt_float(old.length_mean if old else None),
                    fmt_float(new.length_mean if new else None),
                ),
                _percentiles(old, new),
                _arrow(_numeric_mean(old), _numeric_mean(new)),
                _arrow(_numeric_std(old), _numeric_std(new)),
                _types(old, new),
            ]
        )
    lines.append(
        md_table(
            ["字段", "空值率", "长度区间", "长度均值", "分位 p50 / p95", "数值均值",
             "标准差", "类型分布（前 → 后）"],
            rows,
        )
    )
    lines.append("")
    lines.append(_methodology_notes(data))
    return "\n".join(lines)


def _paired_fields(data: ReportInput) -> list[str]:
    """统计对比按**目的字段**列，因为那是用户配置里的名字。

    两侧统计都按目的字段名喂值（引擎先把源列映射成目的字段再统计），所以「清洗前」与
    「清洗后」在同一行指的是同一个字段，可以直接对比。用源列名做键会让这个对应关系
    在表头重命名后就断了。
    """
    names: list[str] = []
    for spec in data.config.fields:
        if spec.dest not in names:
            names.append(spec.dest)
    return names


def _emptiness(old: FieldStats | None, new: FieldStats | None, *, generated: bool) -> str:
    if old is None and new is None:
        return "无数据"
    if old is None:
        return "生成字段" if generated else "无清洗前数据"
    if new is None:
        return f"{fmt_pct(old.empty_rate)} → 无输出"
    return _arrow(fmt_pct(old.empty_rate), fmt_pct(new.empty_rate))


def _length_range(stat: FieldStats | None) -> str:
    if stat is None or stat.length_min is None:
        return "—"
    return f"{stat.length_min}~{stat.length_max}"


def _percentiles(old: FieldStats | None, new: FieldStats | None) -> str:
    """分位数一行。两边的「分位对象」可能不同（一边是长度、一边是数值）—— 那就分别标注，
    否则读者会把长度 200 和数值 200 当成同一个东西。"""
    sides: list[str] = []
    labels: set[str] = set()
    for stat in (old, new):
        low = stat.percentile(0.5) if stat is not None else None
        if low is None:
            sides.append("—")
            continue
        labels.add("值" if stat.percentile_of() == "value" else "长度")
        suffix = "*" if stat.percentiles_approximate() else ""
        sides.append(
            f"{fmt_float(low)}{suffix} / {fmt_float(stat.percentile(0.95))}{suffix}"
        )
    prefix = f"{labels.pop()} " if len(labels) == 1 else ""
    if "—" in sides:
        # 只有一侧有数据时画箭头会读成「— → 3.00」这种半句话
        available = sides[1] if sides[0] == "—" else sides[0]
        return "—" if available == "—" else prefix + available
    return prefix + _arrow(sides[0], sides[1])


def _numeric_mean(stat: FieldStats | None) -> str:
    if stat is None or not stat.numeric:
        return "—"
    return fmt_float(stat.num_mean, 3)


def _numeric_std(stat: FieldStats | None) -> str:
    if stat is None or not stat.numeric or stat.num_count < 2:
        return "—"
    return fmt_float(stat.numeric_stddev, 3)


def _types(old: FieldStats | None, new: FieldStats | None) -> str:
    def render(stat: FieldStats | None) -> str:
        if stat is None or not stat.types:
            return "—"
        # 按固定顺序渲染：类型分布的顺序会变的话，两次运行的报告就不可比了
        order = {name: i for i, name in enumerate(TYPE_NAMES)}
        items = sorted(stat.types.items(), key=lambda item: order.get(item[0], 99))
        return " ".join(f"{name}:{fmt_int(count)}" for name, count in items if count)

    return _arrow(render(old), render(new))


def _methodology_notes(data: ReportInput) -> str:
    lines = [
        "**统计口径**（一次有界扫描能算什么、不能算什么）：",
        "",
        "| 统计量 | 精度 |",
        "|---|---|",
        "| 行数、空值率、长度 min/max/mean | 精确 |",
        "| 数值均值/标准差（Welford 合并） | 精确（限被判定为数值列的字段） |",
        "| 类型分布 | 精确（按值的 Python 类型推断） |",
        "| Top-K 频次 | 基数不超上限时精确；超限后 Space-Saving，标注误差上界 |",
        "| p50 / p95 | **近似**：定长蓄水池采样，样本量见下 |",
        "| 中位数、p99、精确去重计数 | **不做**。没有精确的单遍有界算法，装不进内存的宁可不算 |",
        "",
    ]
    sizes = sorted(
        {
            stat.sample_size
            for stat in (data.before.fields.values() if data.before else [])
        }
        | {
            stat.sample_size
            for stat in (data.after.fields.values() if data.after else [])
        }
    )
    if sizes:
        lines.append(
            f"分位数样本量：{fmt_int(max(sizes))}（带 `*` 的格子表示累计值多于样本量，即来自采样）。"
        )
        lines.append("")
    lines.append("最近秩法取分位；样本不足时不做插值，直接取最接近的样本值。")
    lines.append("")
    if data.resumed:
        lines.append(
            "> 本次是续跑：**分位数与 Top-K 可能与不中断运行不同** —— 采样流与频次候选集"
            "依赖批次划分与提交点的位置。产物文件不受影响（续跑按提交点截断后重放，"
            "输出与不中断运行逐字节一致），受影响的只有这份报告里的近似统计。"
        )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 7


def _section_samples(data: ReportInput) -> str:
    lines = ["## 7. 字段样本（高频值）", ""]
    if data.before is None and data.after is None:
        lines.append("（没有采集到统计）")
        lines.append("")
        return "\n".join(lines)
    top_k = data.config.output.top_k
    # 0 = 不列明细。这张表原本是报告里最占篇幅的一块（每个字段最多 50 行原始值），
    # 几百万行的文件上它既读不完也不说明问题 —— 留标题、留一句实话，聚合统计在上一节。
    if top_k <= 0:
        lines.append(
            "按设置未列值明细（报告明细条数 = 0）。每个字段的计数、空值率、长度分位数见"
            "上一节「前后统计对比」。"
        )
        lines.append("")
        return "\n".join(lines)
    for name in _paired_fields(data):
        old = data.before.field(name) if data.before else None
        new = data.after.field(name) if data.after else None
        if old is None and new is None:
            continue
        lines.append(f"### `{md_text(name)}`")
        lines.append("")
        rows = []
        for value, count in (old.freq.top(top_k) if old else []):
            rows.append([value, fmt_int(count), "—"])
        for value, count in (new.freq.top(top_k) if new else []):
            if old and value in old.freq.counts:
                for row in rows:
                    if row[0] == value:
                        row[2] = fmt_int(count)
                        break
            else:
                rows.append([value, "—", fmt_int(count)])
        lines.append(md_table(["值", "清洗前次数", "清洗后次数"], rows))
        lines.append("")
        approximate = [
            side
            for side, stat in (("清洗前", old), ("清洗后", new))
            if stat is not None and not stat.freq.exact
        ]
        if approximate:
            bounds = [
                stat.freq.error_bound for stat in (old, new) if stat is not None
            ]
            lines.append(
                f"⚠ {'、'.join(approximate)}的频次为近似值：基数超过了逐值上限，"
                f"改用 Space-Saving，计数最多高估 **{max(bounds):,}**。"
                "相对大小仍然可用，绝对数字不要当精确值引用。"
            )
            lines.append("")
    lines.append(
        f"每个字段最多列 {fmt_int(2 * top_k)} 行：清洗前后各取出现次数最多的前 "
        f"{fmt_int(top_k)} 个值再合并（只在某一侧出现的值会带「—」，它们往往正是被清洗"
        "改动的那批，不能因为另一侧没进前几名就藏起来）。报告明细条数可调，0 = 不列。"
        "逐值全量在这类数据上不可行：GB 级数据的基数远大于内存，频次统计本身是有界的近似算法。"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 8


def _section_generated(data: ReportInput) -> str:
    lines = ["## 8. 生成字段说明", ""]
    generated = [spec for spec in data.config.fields if spec.generate]
    if not generated:
        lines.append("本次没有配置字段生成规则。")
        lines.append("")
        return "\n".join(lines)
    usage = data.llm or {}
    per_field = usage.get("per_field") or {}
    rows = []
    for spec in generated:
        rule = spec.generate
        if rule.kind == "llm":
            params = f"模型批次 {rule.batch_size}"
            if rule.temperature is not None:
                params += f"，temperature {rule.temperature}"
            if rule.max_tokens is not None:
                params += f"，max_tokens {rule.max_tokens}"
        elif rule.kind == "random":
            params = canonical_json(rule.params) if rule.params else ""
        elif rule.kind == "sequence":
            params = f"start={rule.start} step={rule.step} width={rule.width}"
        else:
            params = md_text(rule.expression)
        stat = per_field.get(spec.dest, {})
        rows.append(
            [
                spec.dest,
                rule.kind,
                params,
                fmt_int(stat.get("calls", 0)) if stat else "—",
                fmt_int(stat.get("cache_hits", 0)) if stat else "—",
                fmt_int(stat.get("failures", 0)) if stat else "—",
            ]
        )
    lines.append(md_table(["目的字段", "方式", "参数", "大模型调用", "缓存命中", "失败"], rows))
    lines.append("")
    if usage:
        lines.append("### 大模型使用情况")
        lines.append("")
        lines.append(f"- 调用 **{fmt_int(usage.get('calls', 0))}** 次，"
                     f"缓存命中 **{fmt_int(usage.get('cache_hits', 0))}** 次，"
                     f"失败 **{fmt_int(usage.get('failures', 0))}** 次")
        if usage.get("endpoints"):
            lines.append("- 端点：")
            for endpoint in usage["endpoints"]:
                lines.append(
                    f"  - `{md_text(endpoint.get('id'))}` "
                    f"{md_text(endpoint.get('name') or '')} "
                    f"模型 `{md_text(endpoint.get('model'))}`："
                    f"成功 {fmt_int(endpoint.get('ok', 0))}，失败 {fmt_int(endpoint.get('failed', 0))}，"
                    f"平均延迟 {fmt_float(endpoint.get('latency_ms'), 0)} ms"
                )
        if usage.get("exhausted"):
            lines.append(
                "- ⚠ **调用次数已达上限**，之后的字段生成全部跳过（按回退策略处理）。"
                "要处理完请提高上限或改用只处理前 N 行。"
            )
        if usage.get("aborted"):
            lines.append(
                f"- ⚠ 错误率超过阈值（{usage.get('abort_error_rate')}）已中止大模型生成。"
                "一个「成功」但全是回退值的任务比诚实失败更糟。"
            )
        lines.append("")
    for spec in generated:
        if spec.generate.kind == "llm" and spec.generate.prompt:
            lines.append(f"**`{md_text(spec.dest)}` 的提示词模板**")
            lines.append("")
            lines.append("```text")
            lines.append(redact(spec.generate.prompt, data.secrets))
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 9


def _section_regression(data: ReportInput) -> str:
    lines = ["## 9. 复检结论", ""]
    lines.append("### 规则符合性")
    lines.append("")
    if data.after_counters is None:
        lines.append("⚠ **本次没有做回归复检**，这一节给不出结论（不是「通过」）。")
    else:
        after = _after(data)
        total_after = after.total_violations()
        if total_after == 0:
            lines.append("对输出值用同一套约束复检：**全部通过**。")
        else:
            lines.append(
                f"对输出值用同一套约束复检：仍有 **{fmt_int(total_after)}** 处违规。"
                "这些是回退也没能修好的值 —— 逐条看第 5 节的样本，通常意味着约束与数据"
                "根本不匹配（比如把「留空也可以」的字段配成了非空）。"
            )
            lines.append("")
            rows = [
                [field_name, _CONSTRAINT_LABELS.get(kind, kind), fmt_int(count)]
                for (field_name, kind), count in sorted(
                    after.violations.items(), key=lambda item: (-item[1], item[0])
                )
            ]
            lines.append(md_table(["字段", "违规类型", "数量"], rows))
    lines.append("")
    if data.regression:
        lines.append("### 前后对比与对拍")
        lines.append("")
        lines.extend(data.regression)
        lines.append("")
    lines.append(
        "「规则符合性」与第 4 节的「清洗后违规」是同一个数：正向清洗与回归复检共用同一套"
        "约束求值器，且都按输出行序喂值 —— 输入序列相同，结果必然相同。这条等价性是报告"
        "可信度的根，所以打开 `verify_every` 时还会用逐行参考实现抽查列式快路径。"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 10


def _section_repro(data: ReportInput) -> str:
    lines = ["## 10. 复现信息与方法学", ""]
    config = data.config
    info = [
        ("报告版式版本", str(REPORT_FORMAT_VERSION)),
        ("配置指纹", config.config_hash()),
        ("任务 ID", data.task_id),
        ("开始时间", data.started_at.strftime("%Y-%m-%d %H:%M:%S") if data.started_at else "—"),
        ("结束时间", data.finished_at.strftime("%Y-%m-%d %H:%M:%S") if data.finished_at else "—"),
        ("总耗时", fmt_duration(data.duration())),
        ("断点续跑", "是" if data.resumed else "否"),
        ("数据来源", "上传目录" if config.source.mode == "upload" else "服务器路径"),
        ("输入编码", config.input.encoding or "自动探测"),
        ("输入分隔符", config.input.delimiter or "自动探测"),
        ("输出格式", config.output.format),
        ("输出表头布局", "并集（union）" if config.output.layout == "union" else "严格（strict）"),
        ("缺失列处理", config.output.on_missing),
    ]
    lines.append(md_table(["项", "值"], [[k, md_text(v)] for k, v in info]))
    lines.append("")
    lines.append("### 配置摘要")
    lines.append("")
    lines.append("```json")
    lines.append(redact(config.model_dump_json(indent=2), data.secrets))
    lines.append("```")
    lines.append("")
    lines.append(
        "> 密钥不在这份配置里：大模型密钥只存在端点池文件与任务级 `secret.json`（权限 0600，"
        "位于已 gitignore 的数据目录），任务配置只记录端点 ID。报告生成时仍会对所有文本做一次"
        "脱敏，兜住提示词或错误消息里意外夹带的密钥。"
    )
    lines.append("")
    return "\n".join(lines)


def report_filename(task_id: str) -> str:
    """报告文件名。任务 ID 已经限定为安全字符，这里再挡一次，避免标题里带路径分隔符。"""
    safe = "".join(ch for ch in str(task_id) if ch.isalnum() or ch in "-_")
    return f"report-{safe or 'task'}.md"


def dump_json(value: Any) -> str:
    """稳定的 JSON 文本（报告以外的场合也用，键顺序固定便于比对）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
