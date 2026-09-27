"""声明式清洗算子。

每个算子都必须**单遍、有界、可流式** —— 这是 GB 级数据的硬约束，不是风格偏好。因此这里
刻意不提供需要「看到未来」或「记住整列」的语义：去重只有 keep=first，没有 bfill，没有全局
drop_duplicates。算子之间唯一的跨块状态是 OpContext 里的两样东西（ffill 的携带值、去重索引），
它们各自有界，所以整条流水线能在恒定内存里跑完一个 GB 文件。

批内用**列主序**（Table）表示：算子天然是「扫一列」，约束求值也是列式的；行主序只在这一层
的边界出现两次（读入、写出）。转换点少，出错的地方就少。

两条贯穿全模块的规则，改代码前先读：

1. **文本算子只作用于字符串值**（trim/lower/upper/nfkc/replace/regex_replace/slice）。
   对数值列做 trim 是无意义的，而「把数值悄悄转成文本」是数据改动 —— 后者该由用户显式配
   一个 cast 算子，出现在配置里、出现在报告里。
2. **失败不静默**。cast 转不动、日期解析不出来、替换模板写错了，都保持原值并计数。计数进报告，
   第一次出现时打一条日志。任务不会因为一个字段的笔误而失败，但报告必须说清楚有多少格没做成。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from .clean_models import CleanOp
from .clean_schema import (
    _RE_FALLBACK_MAX_LEN,
    UniqueIndex,
    cell_text,
    compiled,
    is_empty,
    parse_datetime,
    to_float,
)

# 字段值的连接符：UNIT SEPARATOR。单元格里出现它的概率可以忽略，而用 "," 之类的可打印字符
# 会让 ("a,b", "c") 与 ("a", "b,c") 拼成同一个键 —— 那是静默漏掉一行去重。
_DEDUPE_SEP = "\x1f"


class CleanOpsError(ValueError):
    """算子层的不可能状态（未知算子名、列宽不自洽）。配置错误走 validate_ops 返回文本。"""


# =========================================================================== 表


@dataclass(eq=False)
class Table:
    """一批数据的列主序视图。

    `columns` 允许重名（I/O 层刻意不去重表头，`normalize_headers` 只填空名），所以按名字
    定位一律是**首次出现**，与字段映射的匹配规则一致。想清洗重名列里的第二列，先用
    concat 生成一个新列名绕开。

    用 `eq=False`：dataclass 默认的逐元素比较在大表上是 O(单元格数) 的隐藏地雷，
    而这里任何时候都不需要值语义的相等。
    """

    columns: list[str]
    data: list[list[Any]]

    # -- 构造与转换 --------------------------------------------------------

    @classmethod
    def from_rows(cls, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> "Table":
        width = len(columns)
        if not rows:
            return cls(list(columns), [[] for _ in range(width)])
        if all(len(row) == width for row in rows):
            # 规整是最常见的情形：走 zip 转置（C 层），比逐格 append 快一个数量级。
            return cls(list(columns), [list(column) for column in zip(*rows)])
        return cls(list(columns), _ragged_to_columns(rows, width))

    def to_rows(self) -> list[list[Any]]:
        if not self.data:
            return []
        return [list(row) for row in zip(*self.data)]

    def nrows(self) -> int:
        return len(self.data[0]) if self.data else 0

    def ncols(self) -> int:
        return len(self.columns)

    # -- 定位与替换 --------------------------------------------------------

    def find(self, name: str) -> int | None:
        try:
            return self.columns.index(name)
        except ValueError:
            return None

    def index(self, name: str) -> int:
        found = self.find(name)
        if found is None:
            raise CleanOpsError(f"字段不存在：{name}")
        return found

    def column(self, name: str) -> list[Any]:
        return self.data[self.index(name)]

    def with_column(self, name: str, values: list[Any]) -> "Table":
        """写入一列：已存在则**就地覆盖**（首次出现的那一列），否则追加到末尾。

        覆盖而不是追加一个新列，是因为 dest 指向的通常是用户想替换掉的列；
        追加会让同名两列同时在输出里，而下游按名取值只会拿到第一列，
        第二列变成谁也看不见的幽灵数据。
        """
        found = self.find(name)
        if found is None:
            return Table(self.columns + [name], self.data + [values])
        data = list(self.data)
        data[found] = values
        return Table(list(self.columns), data)

    def drop_rows(self, keep: Sequence[bool]) -> "Table":
        if len(keep) != self.nrows():
            raise CleanOpsError(f"删除掩码长度 {len(keep)} 与行数 {self.nrows()} 不一致")
        if all(keep):
            return self
        index = [i for i, ok in enumerate(keep) if ok]
        return Table(self.columns, [[column[i] for i in index] for column in self.data])


def _ragged_to_columns(rows: Sequence[Sequence[Any]], width: int) -> list[list[Any]]:
    """参差行补齐/截断到表头宽度。

    这是**最后一道保险**，不是权威计数：读取层（clean_io 的 CsvStream）已经按表头宽度补齐
    并精确计入 `ragged_rows`，报告用的是那个数。这里只保证「宁可替调用方修好，也不要在
    GB 级任务跑到一半时因为一行短了两个字段而抛异常」。
    """
    data: list[list[Any]] = [[] for _ in range(width)]
    for row in rows:
        if len(row) != width:
            row = list(row[:width]) + [""] * (width - len(row))
        for i, value in enumerate(row):
            data[i].append(value)
    return data


# =========================================================================== 上下文


@dataclass
class _DedupeState:
    """一路去重索引的运行时状态。日志坐标（file_start/count）是断点续跑的接口。"""

    index: UniqueIndex
    scope: str
    subset: tuple[str, ...]
    file_start: int
    file_id: str = ""


class OpContext:
    """算子之间唯一的跨块状态。engine 每处理一批就复用它一次。

    - `carry`：ffill 每字段一个携带值（有界：字段数）
    - `_dedupes`：去重索引，每个键一个摘要数组（有界：值个数 × 8 字节）
    - `counters` / `notes`：进报告的计数与一次性日志

    计数器的键是 `"{序号}:{算子名}:{种类}"`，例如 `"2:trim:changed"`。带序号是刻意的：
    同一批数据上「第 3 条规则把 1234 格改成什么样」是用户真正想看的信息，而只按算子名聚合
    会把两条 trim 混成一个数。序号对应配置里 ops 的下标，报告据此还原规则。
    """

    def __init__(self, log_path_for: Callable[[str], Path | None] | None = None):
        """`log_path_for` 把去重键映射到摘要日志的路径（None = 纯内存，用于测试与干跑）。

        传路径而不是传一个已打开的 UniqueIndex：恢复时要从同一个日志**重建**一个索引对象，
        若工厂返回的是句柄，那个句柄就白开一次并且没人关（ResourceWarning 就是这么来的）。
        """
        self.counters: dict[str, int] = {}
        self.notes: dict[str, str] = {}
        self.carry: dict[str, Any] = {}
        self._log_path_for = log_path_for
        self._dedupes: dict[str, _DedupeState] = {}
        self._file_id = ""

    def _new_index(self, key: str) -> UniqueIndex:
        path = self._log_path_for(key) if self._log_path_for is not None else None
        return UniqueIndex(path)

    # -- 计数与日志 --------------------------------------------------------

    def count(self, key: str, amount: int = 1) -> None:
        if amount:
            self.counters[key] = self.counters.get(key, 0) + amount

    def note_once(self, key: str, text: str) -> None:
        """按 key 只记一次。告警日志的有界化：**计数始终精确**，日志行按原因去重。

        「每次触发回退都打一行」在 GB 级等于给一个配错的规则写几百万行，日志本身就废了。
        """
        if key not in self.notes:
            self.notes[key] = text

    # -- 去重索引 ----------------------------------------------------------

    def dedupe_index(self, scope: str, subset: Sequence[str]) -> _DedupeState:
        """按 (作用域, 字段集合) 取索引。键由内容派生，所以它在重启前后是同一个键 ——
        这是断点续跑能把索引接回去的前提（不能靠「配置里的第几条规则」当身份）。"""
        key = dedupe_key(scope, subset)
        state = self._dedupes.get(key)
        if state is None:
            index = self._new_index(key)
            # file_id 取上下文当前的（而不是留空）：索引是惰性创建的，一个算子可能在本文件
            # 的第一批才第一次用到它，那时 reset_file 早就过去了。留空会让续跑时判不出
            # 「这个索引属于哪个文件」，从而要么重放整个日志（错误地丢掉新文件的行），
            # 要么全部丢弃（错误地放行重复行）。
            state = self._dedupes[key] = _DedupeState(
                index=index,
                scope=scope,
                subset=tuple(subset),
                file_start=index.count,
                file_id=self._file_id,
            )
        return state

    def reset_file(self, file_id: str = "") -> None:
        """文件边界。engine 在开始处理每个文件时调用一次。

        - **清 carry**：把上一个文件的最后一个值填进下一个文件的第一行，是从另一个表里
          凭空造数据。ffill 的语义只在单个表内有意义。
        - **file 作用域的去重索引**用 reset_visible()：丢掉内存成员但继续追加同一个日志，
          因为日志长度是续跑的唯一坐标（见 UniqueIndex.reset_visible）。
          task 作用域的索引不动 —— 那正是它的定义。
        """
        self.carry.clear()
        self._file_id = file_id
        for state in self._dedupes.values():
            if state.scope == "file":
                state.index.reset_visible()
                state.file_start = state.index.count
                state.file_id = file_id

    # -- 断点续跑的接口 ----------------------------------------------------

    def dedupe_snapshot(self) -> dict[str, dict[str, Any]]:
        """给 state.json 的索引坐标。**只记计数，摘要本身在各自 append-only 日志里** ——
        这也是当初选「有序摘要数组 + 日志」而不是内存哈希表的直接收益。"""
        snapshot: dict[str, dict[str, Any]] = {}
        for key, state in self._dedupes.items():
            entry: dict[str, Any] = {"count": state.index.count}
            if state.scope == "file":
                entry["file_start"] = state.file_start
                entry["file"] = state.file_id
            snapshot[key] = entry
        return snapshot

    def restore_indexes(self, snapshot: dict[str, dict[str, Any]], current_file: str = "") -> None:
        """按 state.json 重建去重索引。

        file 作用域的关键一步：快照里的文件**就是**正在续跑的那个文件时，只重放
        `[file_start, count)`；否则说明那个文件已经完成，索引必须从空开始 —— 把上一个文件
        的值当成「已见过」会错误地丢掉下一个文件的行，而那种错误在产物里完全看不出来。
        """
        if self._log_path_for is None:
            # 纯内存上下文（报告生成、干跑）：没有日志可重放，索引成员也不是报告要的东西
            # （报告只读 counters / notes）。这里必须早退，否则会拿 path=None 去读文件。
            return
        for key, entry in snapshot.items():
            scope, subset = split_dedupe_key(key)
            path = self._log_path_for(key) if self._log_path_for is not None else None
            file_start = int(entry.get("file_start", 0))
            count = int(entry.get("count", 0))
            if scope == "file" and entry.get("file") != current_file:
                # 文件已完成：可见区间为空，但日志坐标要往前推，否则下次截断会砍错位置。
                index = UniqueIndex.rebuild(path, count, count)
                file_start = count
            else:
                index = UniqueIndex.rebuild(path, count, file_start)
            self._dedupes[key] = _DedupeState(
                index=index,
                scope=scope,
                subset=subset,
                file_start=file_start,
                file_id=str(entry.get("file", "")),
            )

    def flush(self) -> None:
        for state in self._dedupes.values():
            state.index.flush()

    def truncate_indexes(self, snapshot: dict[str, dict[str, Any]]) -> None:
        """把日志砍回快照里的坐标。崩溃时尾部可能写了半条，重放必须是严格幂等的。"""
        for key, state in self._dedupes.items():
            entry = snapshot.get(key)
            if entry is not None:
                state.index.truncate_to(int(entry.get("count", 0)))

    def close(self) -> None:
        for state in self._dedupes.values():
            state.index.close()


def dedupe_key(scope: str, subset: Sequence[str]) -> str:
    return f"dedupe|{scope}|{','.join(subset)}"


def split_dedupe_key(key: str) -> tuple[str, tuple[str, ...]]:
    """dedupe_key 的逆。字段名里带 "," 的组合（`("a,b",)` vs `("a","b")`）会解析成同一个
    键 —— 两者本来就是同一份索引语义（都按这两个值的连接判重），所以不需要更复杂的编码。"""
    _, scope, fields = key.split("|", 2)
    return scope, tuple(fields.split(",")) if fields else ()


# =========================================================================== 单元格级工具

_FAILED = object()
"""转换失败的哨兵。用哨兵而不是 None：None 是一个合法的单元格值（虽然 is_empty 认它），
用 None 表示失败会让「空值」与「转不动」混成一件事。"""

_NO_CARRY = object()


def _text_only(fn: Callable[[str], str]) -> Callable[[Any], Any]:
    def apply(value: Any) -> Any:
        return fn(value) if isinstance(value, str) else value

    return apply


_TRIM = _text_only(str.strip)
_LOWER = _text_only(str.lower)
_UPPER = _text_only(str.upper)
_NFKC = _text_only(lambda value: unicodedata.normalize("NFKC", value))


def _map_cells(column: list[Any], fn: Callable[[Any], Any]) -> tuple[list[Any], int]:
    """逐格变换 + 变化计数。

    两趟而不是一趟：两趟都是 C 层的（列表推导 + genexp 求和），比带分支的 Python 循环快，
    而 changed 计数是报告里「这条规则到底改了什么」的唯一来源 —— 规则没生效时用户看到的
    是 0，而不是一个需要自己判断的空白。
    """
    new = [fn(value) for value in column]
    return new, sum(1 for before, after in zip(column, new) if before != after)


def _map_counted(column: list[Any], fn: Callable[[Any], Any]) -> tuple[list[Any], int, int]:
    """带失败计数的逐格变换：fn 返回 `_FAILED` 表示这格没做成，原值保留。

    这条路必须是显式循环（要在中途分流），所以只给真的会失败的算子用。
    """
    out: list[Any] = []
    changed = failed = 0
    for value in column:
        new = fn(value)
        if new is _FAILED:
            failed += 1
            out.append(value)
            continue
        if new != value:
            changed += 1
        out.append(new)
    return out, changed, failed


# =========================================================================== 入口


def apply_ops(table: Table, ops: Sequence[CleanOp], ctx: OpContext) -> Table:
    """按配置顺序应用算子。返回的 Table 可能被替换（删行的算子会重建它）。"""
    for slot, op in enumerate(ops):
        handler = _HANDLERS.get(op.op)
        if handler is None:  # 模型层已限定枚举，这里只是拒绝静默
            raise CleanOpsError(f"未知算子：{op.op}")
        table = handler(table, op, ctx, f"{slot}:{op.op}")
    return table


# =========================================================================== 算子


def _resolve(table: Table, op: CleanOp, ctx: OpContext, slot: str, name: str) -> int | None:
    """定位算子的目标列。找不到时**跳过这个算子并计数**，不抛异常。

    目录来源 + 表头不齐是常态（一个文件多一列、少一列），一个文件缺列不该让跑了半小时的
    任务整体失败。但也绝不能静默：计数进报告、日志打一条，「这条规则没生效」必须是可见的。
    """
    found = table.find(name)
    if found is None:
        ctx.count(f"{slot}:field_missing")
        ctx.note_once(
            f"missing:{name}",
            f"字段 {name!r} 在部分数据中不存在，用到它的算子在这些批次被跳过"
            "（多文件表头不一致时属正常，报告里可按文件核对）",
        )
    return found


def _text_op(
    table: Table, op: CleanOp, ctx: OpContext, slot: str, fn: Callable[[Any], Any]
) -> Table:
    """文本算子的公共骨架：定位列 → 逐格变换 → 写回并计数。"""
    found = _resolve(table, op, ctx, slot, op.field)
    if found is None:
        return table
    column, changed = _map_cells(table.data[found], fn)
    ctx.count(f"{slot}:changed", changed)
    data = list(table.data)
    data[found] = column
    return Table(list(table.columns), data)


def _op_trim(table, op, ctx, slot):
    return _text_op(table, op, ctx, slot, _TRIM)


def _op_lower(table, op, ctx, slot):
    return _text_op(table, op, ctx, slot, _LOWER)


def _op_upper(table, op, ctx, slot):
    return _text_op(table, op, ctx, slot, _UPPER)


def _op_nfkc(table, op, ctx, slot):
    return _text_op(table, op, ctx, slot, _NFKC)


def _op_replace(table, op, ctx, slot):
    """字面量替换（不是正则）。要按模式替换请用 regex_replace —— 两者的转义规则不同，
    混在一起会让「为什么我的 `\\d` 没生效」变成一个必须读源码才能回答的问题。"""
    source, target = op.pattern, op.replacement
    return _text_op(table, op, ctx, slot, _text_only(lambda v: v.replace(source, target)))


def _op_regex_replace(table, op, ctx, slot):
    pattern = compiled(op.pattern, "search")  # 替换天然是 search 语义（与 mode 无关）
    replacement = op.replacement
    skipped_template = f"{slot}:template_error"

    def convert(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            new = pattern.sub(replacement, value)
        except Exception as exc:
            # 模板非法（`\9` 但模式里没第 9 组）或引擎自身的问题。一个字段的笔误不该炸掉
            # 整个 GB 级任务，但也绝不能装作替换成功了。
            ctx.count(skipped_template)
            ctx.note_once(
                skipped_template,
                f"正则替换模板有误，该算子未生效：pattern={op.pattern!r} "
                f"replacement={replacement!r}（{exc}）",
            )
            return value
        if new is None:
            # 回落引擎 + 超长文本：不做替换。计数意味着「这些格没被替换」，与 matches()
            # 判为不匹配是同一个取舍：宁可不做，也不基于残缺文本产出一个看起来正常的结果。
            ctx.count(f"{slot}:skipped")
            ctx.note_once(
                f"{slot}:skipped_why",
                f"文本超过 {_RE_FALLBACK_MAX_LEN} 字符且该模式回落到 Python re，"
                "正则替换被跳过",
            )
            return value
        return new

    return _text_op(table, op, ctx, slot, convert)


def _op_fill_null(table, op, ctx, slot):
    fill = op.value
    # 判空用 is_empty（None / 空串），与约束层同一定义：空白串是数据事实，
    # 该由 trim 显式处理。这里若顺手 strip 一下，填空与不填空的边界就变得不可预测了。
    return _text_op(
        table, op, ctx, slot, lambda value: fill if is_empty(value) else value
    )


def _op_ffill(table, op, ctx, slot):
    """前向填充。携带值跨块保留在 ctx 里（每字段一个，有界），文件边界清空。"""
    found = _resolve(table, op, ctx, slot, op.field)
    if found is None:
        return table
    current = ctx.carry.get(op.field, _NO_CARRY)
    out: list[Any] = []
    filled = 0
    for value in table.data[found]:
        if not is_empty(value):
            current = value
        elif current is _NO_CARRY:
            # 这一列开头就是空的：没有可携带的值。保持空 —— 用空串「填」一个空串是假装做了事。
            pass
        else:
            value = current
            filled += 1
        out.append(value)
    if current is not _NO_CARRY:
        ctx.carry[op.field] = current
    ctx.count(f"{slot}:filled", filled)
    data = list(table.data)
    data[found] = out
    return Table(list(table.columns), data)


def _cast_value(value: Any, kind: str, date_formats: Sequence[str]) -> Any:
    """转不动的返回 `_FAILED`（保持原值并计数），不返回 None。空值原样通过。"""
    if is_empty(value):
        return value
    if kind == "str":
        return cell_text(value)
    if isinstance(value, bool):
        # bool → int/float 在 Python 里是合法的（True == 1），但对清洗而言那是把标记值
        # 悄悄变成一个数。用户真要这么做，先 cast 成 str 再转，让意图出现在配置里。
        return _FAILED
    text = cell_text(value).strip()
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            pass
        # "3.0" → 3 是清洗场景的真实需求；但 "3.7" → 3 是静默丢数据，拒绝。
        number = to_float(text)
        if number is None or not float(number).is_integer():
            return _FAILED
        # 走 float 这条路只为了接受 "3.0"/"1e3" 这类写法。超过 2^53 的整数字面量在上面
        # 那句 int(text) 里已经精确处理过了，不会走到这里丢精度。
        return int(number)
    if kind == "float":
        number = to_float(text)
        return _FAILED if number is None else number
    if kind in ("date", "datetime"):
        parsed = parse_datetime(value, tuple(date_formats))
        if parsed is None:
            return _FAILED
        return parsed.date() if kind == "date" else parsed
    return _FAILED


def _op_cast(table, op, ctx, slot):
    found = _resolve(table, op, ctx, slot, op.field)
    if found is None:
        return table
    kind = op.value_kind
    formats = op.date_formats
    column, changed, failed = _map_counted(
        table.data[found], lambda value: _cast_value(value, kind, formats)
    )
    ctx.count(f"{slot}:changed", changed)
    ctx.count(f"{slot}:failed", failed)
    if failed:
        ctx.note_once(
            f"{slot}:failed_why",
            f"字段 {op.field!r} 有 {failed} 个值无法转成 {kind}，已保持原值"
            "（约束与回退会照常对它们生效，报告里的违规数包含了这些格）",
        )
    data = list(table.data)
    data[found] = column
    return Table(list(table.columns), data)


def _op_date_format(table, op, ctx, slot):
    out_format = op.date_format
    formats = op.date_formats
    fmt_ok = _strftime_works(out_format)

    def convert(value: Any) -> Any:
        if is_empty(value):
            return value
        if not fmt_ok:
            return _FAILED
        if isinstance(value, datetime):
            parsed: datetime | None = value
        elif isinstance(value, date):
            parsed = datetime(value.year, value.month, value.day)
        else:
            parsed = parse_datetime(value, tuple(formats))
        if parsed is None:
            return _FAILED
        try:
            return parsed.strftime(out_format)
        except ValueError:
            return _FAILED

    found = _resolve(table, op, ctx, slot, op.field)
    if found is None:
        return table
    if not fmt_ok:
        ctx.note_once(
            f"{slot}:bad_format",
            f"date_format 的输出格式 {out_format!r} 无法用于 strftime，该算子未生效",
        )
    column, changed, failed = _map_counted(table.data[found], convert)
    ctx.count(f"{slot}:changed", changed)
    ctx.count(f"{slot}:failed", failed)
    if failed:
        ctx.note_once(
            f"{slot}:failed_why",
            f"字段 {op.field!r} 有 {failed} 个值不是可识别的日期（已配置输入格式 "
            f"{formats or ['ISO8601']}），已保持原值",
        )
    data = list(table.data)
    data[found] = column
    return Table(list(table.columns), data)


def _strftime_works(fmt: str) -> bool:
    """strftime 的错误格式（如 %Q）在 Linux 上是「原样输出」而不是抛异常，所以两种都试一下：
    一个不会展开的格式串等于静默不改数据，那必须提前拒绝。"""
    if not fmt:
        return False
    try:
        probe = datetime(2001, 2, 3, 4, 5, 6).strftime(fmt)
    except (ValueError, TypeError):
        return False
    return probe != fmt


def _op_number_format(table, op, ctx, slot):
    kind = op.value_kind
    decimals = op.decimals
    thousands = op.thousands

    def convert(value: Any) -> Any:
        text = cell_text(value)
        if not text:
            return value  # 空值原样：格式化不该把空格变成一个 0
        try:
            number = Decimal(text.strip())
            if not number.is_finite():
                return _FAILED
            if decimals is None:
                # int 默认取整；float 保持自己的小数位，只把 1E+3 这类科学计数法摊开。
                quantized = (
                    number.quantize(Decimal(1), rounding=ROUND_HALF_UP)
                    if kind == "int"
                    else number
                )
            else:
                quantized = number.quantize(
                    Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP
                )
            return format(quantized, ",f" if thousands else "f")
        except (InvalidOperation, ValueError, ArithmeticError):
            return _FAILED

    found = _resolve(table, op, ctx, slot, op.field)
    if found is None:
        return table
    column, changed, failed = _map_counted(table.data[found], convert)
    ctx.count(f"{slot}:changed", changed)
    ctx.count(f"{slot}:failed", failed)
    if failed:
        ctx.note_once(
            f"{slot}:failed_why",
            f"字段 {op.field!r} 有 {failed} 个值不是数字，已保持原值"
            "（千分位逗号之类的写法要先 replace 掉再格式化）",
        )
    data = list(table.data)
    data[found] = column
    return Table(list(table.columns), data)


def _op_slice(table, op, ctx, slot):
    start, end = op.start, op.end
    return _text_op(table, op, ctx, slot, _text_only(lambda v: v[start:end]))


def _op_concat(table, op, ctx, slot):
    found = [table.find(name) for name in op.fields]
    missing = [name for name, index in zip(op.fields, found) if index is None]
    if missing:
        ctx.count(f"{slot}:field_missing")
        ctx.note_once(
            f"missing:{missing[0]}",
            f"字段 {missing[0]!r} 在部分数据中不存在，用到它的算子在这些批次被跳过"
            "（多文件表头不一致时属正常，报告里可按文件核对）",
        )
        return table
    indexes = [index for index in found if index is not None]
    separator = op.separator
    # 空值保留位置（不跳过）：`省+市+区` 里区为空时产出 "粤A-" 而不是 "粤A"，
    # 「第几段对应哪个字段」始终可预测。要跳过空格得用生成规则，那是另一个算子的事。
    values = [
        separator.join(cell_text(value) for value in row)
        for row in zip(*(table.data[index] for index in indexes))
    ]
    existing = table.find(op.dest)
    if existing is None:
        changed = len(values)
    else:
        changed = sum(
            1 for before, after in zip(table.data[existing], values) if before != after
        )
    ctx.count(f"{slot}:changed", changed)
    return table.with_column(op.dest, values)


def _op_drop_null(table, op, ctx, slot):
    names = list(op.fields) or ([op.field] if op.field else [])
    indexes = [table.find(name) for name in names]
    if any(index is None for index in indexes):
        missing = [name for name, index in zip(names, indexes) if index is None]
        ctx.count(f"{slot}:field_missing")
        ctx.note_once(
            f"missing:{missing[0]}",
            f"字段 {missing[0]!r} 在部分数据中不存在，用到它的算子在这些批次被跳过"
            "（多文件表头不一致时属正常，报告里可按文件核对）",
        )
        return table
    columns = [table.data[index] for index in indexes if index is not None]
    if not columns:
        return table
    # 多字段时是「任一为空即删」：这是数据完整性过滤的常规语义（缺了任一个关键字段的行
    # 都不完整），也让单字段的情形与多字段保持一致。
    keep = [
        not any(is_empty(value) for value in row) for row in zip(*columns)
    ]
    dropped = keep.count(False)
    ctx.count(f"{slot}:dropped", dropped)
    return table.drop_rows(keep)


def _op_dedupe(table, op, ctx, slot):
    """按 `subset`（默认整行）去重，保留首次出现。

    两阶段契约与 unique 约束一致：先 check_batch 判定（**不改索引**），再接受留下的行的摘要。
    dedupe 与 unique 的差别在于「解决」这一步：去重没有回退会生成新值，所以解决就等于
    「把留下的行记进索引」，不需要逐行 probe→add 的即时性（那个契约是为了防止回退生成的值
    与自己冲突才必须的）。
    """
    subset = list(op.subset) or list(table.columns)
    indexes = [table.find(name) for name in subset]
    if any(index is None for index in indexes):
        missing = [name for name, index in zip(subset, indexes) if index is None]
        ctx.count(f"{slot}:field_missing")
        ctx.note_once(
            f"missing:{missing[0]}",
            f"字段 {missing[0]!r} 在部分数据中不存在，用到它的算子在这些批次被跳过"
            "（多文件表头不一致时属正常，报告里可按文件核对）",
        )
        return table
    index_columns = [index for index in indexes if index is not None]
    keys = [
        _dedupe_key(row) for row in zip(*(table.data[index] for index in index_columns))
    ]
    state = ctx.dedupe_index(op.scope, subset)
    duplicates = state.index.check_batch(keys)
    if not duplicates:
        return table
    digests, _ = UniqueIndex.digest_many(keys)
    keep_mask = np.fromiter(
        (not flag for flag in duplicates), dtype=bool, count=len(duplicates)
    )
    state.index.add_digests(digests[keep_mask])
    ctx.count(f"{slot}:dropped", duplicates.count(True))
    return table.drop_rows(keep_mask.tolist())


def _dedupe_key(row: Sequence[Any]) -> str:
    """一行在比较字段上的连接键。加前缀是为了**保证非空**：UniqueIndex 把空值视为「不参与
    唯一性」，而全空的行之间的重复是真实存在的重复，必须能被判出来。"""
    return _DEDUPE_SEP + _DEDUPE_SEP.join(cell_text(value) for value in row)


_HANDLERS: dict[str, Callable[[Table, CleanOp, OpContext, str], Table]] = {
    "trim": _op_trim,
    "lower": _op_lower,
    "upper": _op_upper,
    "nfkc": _op_nfkc,
    "replace": _op_replace,
    "regex_replace": _op_regex_replace,
    "fill_null": _op_fill_null,
    "ffill": _op_ffill,
    "cast": _op_cast,
    "date_format": _op_date_format,
    "dedupe": _op_dedupe,
    "drop_null": _op_drop_null,
    "concat": _op_concat,
    "slice": _op_slice,
    "number_format": _op_number_format,
}


# =========================================================================== 配置期校验


def validate_ops(ops: Sequence[CleanOp], columns: Sequence[str] | None = None):
    """配置期检查，返回 `(errors, warnings)`。

    errors 必须让任务创建失败（模式编译不了、格式串不展开）；warnings 只提示
    （字段在当前表头里没有、正则回落到 Python re）。区分两者是有用的：目录来源下
    「某个文件没有这个字段」是正常现象，而「正则写错了」不是。

    这里**只做能静态判定的检查**。真正的保障是 /validate 拿前 N 行跑一遍完整流水线 ——
    那个会看到实际数据，比如替换模板的组引用是否越界、多少格转成了数字。
    """
    errors: list[str] = []
    warnings: list[str] = []
    for slot, op in enumerate(ops):
        where = f"第 {slot + 1} 条规则（{op.op}）"
        if columns is not None:
            names = list(op.fields)
            if op.field:
                names.append(op.field)
            known = set(columns)
            for name in names:
                if name and name not in known:
                    warnings.append(f"{where}：字段 {name!r} 不在当前表头里，该规则会被跳过")
        if op.op in ("regex_replace",):
            if not op.pattern:
                errors.append(f"{where}：正则表达式为空")
                continue
            try:
                pattern = compiled(op.pattern, "search")
            except Exception as exc:  # re.error / re2 的异常类型不同，这里只关心「编译不了」
                errors.append(f"{where}：正则表达式无法编译（{exc}）")
                continue
            if pattern.engine == "re":
                warnings.append(
                    f"{where}：RE2 不支持该模式，已回落到 Python re —— 有 "
                    f"{_RE_FALLBACK_MAX_LEN} 字符的长度上限，且不做灾难性回溯防护"
                )
        if op.op == "date_format" and not _strftime_works(op.date_format):
            errors.append(
                f"{where}：输出格式 {op.date_format!r} 无法用于 strftime"
                "（在 Linux 上无效的格式会原样输出，等于不改数据）"
            )
    return errors, warnings
