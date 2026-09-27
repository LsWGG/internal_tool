"""约束求值、回退实施、唯一性索引 —— 整个清洗功能的地基。

这个模块有三个不变量，破坏了任何一个，回归报告就会撒谎：

1. **标量/列式等价**：`check_value`（逐行标量，慢但显然正确）与 `eval_column`（列式快路径）
   对同一份数据必须给出相同的违规。等价性由测试保证（tests/test_clean_schema.py），并在
   运行时可选抽查（OutputOptions.verify_every）。列式优化不被允许偏离参考实现。
2. **回归可复现**：回归复检是「把写出的文件读回来，再跑一遍同一套约束」，它必须对同一份
   文件给出相同结果（断点续跑会重跑它）。所以它不依赖任何运行期状态，索引也从输出值
   现场重建。
   注意正向与回归的违规数**本就应该不同** —— 回归跑的是清洗后的值，报告里的「清洗前 vs
   清洗后」两列就是靠这个差值说话的。这里要求的是一致性，不是相等。
3. **违规类型确定**：类型优先级硬编码（_PRIORITY），与用户在 UI 里排列约束的顺序无关。
   空值必须报 non_null 而不是「正则不匹配」，否则重试提示与报告都变成噪音。

关于唯一性索引有两个刻意的语义选择，都会体现在报告里：

- **空值不参与唯一性**（既不进索引，也不算冲突）。否则一个允许空值的列里，第二个空值
  就会开始报重复。要禁止空值请配 non_null。
- **索引喂的是回退之后的值，按输出行序逐个喂**。检测阶段（check_batch，不改索引）与
  解决阶段（逐行 add）严格分开，理由见 check_batch 的 docstring。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import numpy as np

from .clean_models import Constraint, Fallback

try:  # RE2 是可选加速项：装不上时全部回落到 re + 长度上限。
    import re2 as _re2
except ImportError:  # pragma: no cover - 取决于环境
    _re2 = None

# 违规类型优先级。硬编码是刻意的：用户在界面上拖动约束顺序不该改变「先报哪一种违规」，
# 否则同一份数据在两遍之间、或在不同用户之间会给出不同的违规类型。
_PRIORITY = {
    "non_null": 0,
    "format": 1,
    "type": 1,
    "enum": 2,
    "regex": 3,
    "length": 4,
    "range": 5,
    "unique": 6,
    "llm_error": 7,
}

# 回落到 re 时的文本长度上限。Python 的 re 没有超时，一个 (a+)+$ 能永久钉死工作线程。
_RE_FALLBACK_MAX_LEN = 10000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_BOOL_TRUE = {"true", "1", "yes", "y", "t", "是", "真"}
_BOOL_FALSE = {"false", "0", "no", "n", "f", "否", "假"}


@dataclass(slots=True)
class Violation:
    field: str
    kind: str
    value: Any
    detail: str = ""


@dataclass(slots=True)
class FallbackResult:
    value: Any
    changed: bool
    # truncate 对这类违规无法实施（枚举不可截断、format 无法修补），降级为 keep。
    # 引擎据此打一条告警 —— 「静默降级」和「实施成功」必须在日志里分得清。
    degraded: bool = False
    note: str = ""


def is_empty(value: Any) -> bool:
    """空值的定义。刻意不 strip：strip 会改动数据，而「值里有没有空格」是数据事实，
    该由 trim 算子显式处理，不该藏在约束判空里。"""
    return value is None or value == ""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def cell_text(value: Any) -> str:
    """单元格的文本形式 —— **全工具唯一一份**（I/O 写文件、算子取文本、统计与预览都用它）。

    放在这一层是因为它属于「值的语义」，而不是「某一种输出格式的细节」：算子用
    `str(v)`、写出用另一个函数的话，报告与产出文件就会对不上，而这正是最难查的一类 bug。

    浮点整数化（`1.0` → `1`）是刻意的：xlsx 读回来的数值列是真 float，用户函数与生成器
    产出的也是真数值，而 `1.0` 与 `1` 在 CSV 里是同一个数。保留 `.0` 只会让「这列到底是
    整数还是浮点」在两遍之间看起来像变了。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # bool 要在 int 之前判 —— 它是 int 的子类，否则 True 会写成 "1"
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


def _ordered(constraints: Sequence[Constraint]) -> list[Constraint]:
    # sorted 是稳定排序：同优先级的约束保持用户配置的顺序。
    return sorted(constraints, key=lambda c: _PRIORITY.get(c.kind, 99))


# --------------------------------------------------------------------------- 正则引擎


class CompiledPattern:
    """用户正则的执行引擎。

    优先 RE2：它是线性时间的，从根本上消除灾难性回溯。RE2 只支持正则的一个子集
    （无反向引用、无环视），不在子集内的模式回落到 Python 的 re，并启用长度上限。

    引擎选择对同一个 pattern 是**确定**的（先试 RE2，再回落 re），所以正向清洗与回归
    复检必然用同一个引擎 —— 两遍一致性不受这里影响。这一点很重要：换引擎不会破坏
    报告的可信度，只有「同一遍里用不同引擎」才会。
    """

    __slots__ = ("raw", "mode", "engine", "_rx")

    def __init__(self, pattern: str, mode: str = "fullmatch"):
        self.raw = pattern
        self.mode = mode
        self.engine = "re2" if _re2 is not None else "re"
        if _re2 is not None:
            try:
                self._rx = _re2.compile(pattern)
            except Exception:
                # RE2 不支持的语法（反向引用、环视等）→ 回落。这是唯一会落到 re 的原因。
                self.engine = "re"
                self._rx = re.compile(pattern)
        else:
            self._rx = re.compile(pattern)

    def matches(self, value: Any) -> bool:
        text = _text(value)
        if self.engine == "re" and len(text) > _RE_FALLBACK_MAX_LEN:
            # 回落引擎下的长度闸。选择「判为不匹配」而不是「截断后再匹配」：截断会产生
            # 一个看起来正常、实际基于残缺文本的结果，那比明确报违规更危险。违规详情里
            # 会写明原因，用户看到的是「未做匹配」而不是一个假的失败。
            return False
        return bool(self._rx.fullmatch(text)) if self.mode == "fullmatch" else bool(self._rx.search(text))

    def find(self, value: Any) -> str | None:
        """search 模式下取出匹配到的子串，供 truncate 回退使用。"""
        if self.mode != "search":
            return None
        match = self._rx.search(_text(value))
        return match.group(0) if match else None

    def over_limit(self, value: Any) -> bool:
        return self.engine == "re" and len(_text(value)) > _RE_FALLBACK_MAX_LEN

    def sub(self, replacement: str, value: Any) -> str | None:
        """替换**全部**匹配（与 mode 无关 —— 替换语义天然是 search 的）。

        **返回 None 只表示一件事**：回落引擎遇到超长文本，这次替换没做。与 matches() 同理，
        宁可不动也不产出一个基于残缺文本的结果。

        替换模板非法（`\\9` 但模式里没有第 9 组）会**照原样抛出**，调用方（clean_ops 的
        regex_replace）负责把它计数并降级 —— 在这里吞掉异常会让「模板写错了」和「文本太长」
        混成同一个计数器，而这两件事对用户的含义完全不同：前者要改配置，后者要改数据。
        """
        text = _text(value)
        if self.engine == "re" and len(text) > _RE_FALLBACK_MAX_LEN:
            return None
        return self._rx.sub(replacement, text)


_PATTERN_CACHE: dict[tuple[str, str], CompiledPattern] = {}


def compiled(pattern: str, mode: str = "fullmatch") -> CompiledPattern:
    key = (pattern, mode)
    got = _PATTERN_CACHE.get(key)
    if got is None:
        got = _PATTERN_CACHE[key] = CompiledPattern(pattern, mode)
    return got


# --------------------------------------------------------------------------- 数值 / 日期


def to_float(value: Any) -> float | None:
    """宽松但不猜：只接受 float() 能吃下的东西。不处理千分位逗号、不处理全角数字 ——
    那些是数据问题，该由清洗算子显式修掉，而不是在约束求值里悄悄接受。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_datetime(value: Any, formats: Sequence[str]) -> datetime | None:
    text = _text(value).strip()
    if not text:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    if not formats:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def cast_check(value: Any, kind: str | None) -> bool:
    """type / cast 共用的可转换性判断。"""
    if is_empty(value):
        return False
    text = _text(value).strip()
    if kind == "str":
        return True
    if kind == "int":
        try:
            int(text)
            return True
        except ValueError:
            # "1.0" 不是 int，但 "1e3" 也不是 —— int() 已经覆盖了正确的语义边界
            return False
    if kind == "float":
        return to_float(value) is not None
    if kind == "bool":
        return text.lower() in _BOOL_TRUE or text.lower() in _BOOL_FALSE
    if kind in ("date", "datetime"):
        return parse_datetime(value, ()) is not None
    return False


# --------------------------------------------------------------------------- 唯一性索引


class UniqueIndex:
    """有界的唯一性索引：有序的 8 字节摘要数组 + 待合并缓冲 + 只追加的日志。

    只存摘要不存原值：1000 万个 12 字符的字符串用 Python set 要约 1.2GB，摘要数组是
    80MB 定长。代价是摘要碰撞 —— 8 字节、1000 万值时的碰撞概率约 2.7e-6，也就是说
    极端情况下可能把一个全新的值误判为重复。这个取舍是有意的：误判会走回退策略并被
    计数进报告，而内存爆炸会让任务直接跑不完。

    为什么是「有序数组 + 缓冲」而不是哈希表：这个结构恢复起来只要一步 np.sort，
    而哈希表的容量与负载因子状态在断点续跑时是个麻烦。可续跑是需求，所以结构跟着它选。
    """

    PENDING_LIMIT = 65536

    def __init__(self, log_path=None):
        self.log_path = log_path
        self._sorted = np.empty(0, dtype=np.uint64)
        self._pending: list[int] = []
        self._pending_set: set[int] = set()
        self._count = 0          # 已接受的值的总数（= 日志里的条目数）
        self._log = None
        if log_path is not None:
            self._log = open(log_path, "ab", buffering=1024 * 256)

    # -- 摘要 --------------------------------------------------------------

    @staticmethod
    def digest(value: Any) -> int | None:
        """值的确定性的 8 字节摘要。空值返回 None（不参与唯一性）。"""
        if is_empty(value):
            return None
        text = value if isinstance(value, str) else str(value)
        return int.from_bytes(
            hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "little"
        )

    @staticmethod
    def digest_many(values: Sequence[Any]) -> tuple[np.ndarray, np.ndarray]:
        """返回 (摘要数组, 空值掩码)。摘要数组里空值位置填 0（配合空值掩码忽略）。"""
        n = len(values)
        out = np.zeros(n, dtype=np.uint64)
        empty = np.zeros(n, dtype=bool)
        for i, value in enumerate(values):
            d = UniqueIndex.digest(value)
            if d is None:
                empty[i] = True
            else:
                out[i] = d
        return out, empty

    # -- 查询与接受 --------------------------------------------------------

    def probe(self, value: Any) -> bool:
        """True = 该值已经出现过。标量参考实现用。"""
        d = self.digest(value)
        if d is None:
            return False
        if d in self._pending_set:
            return True
        if self._sorted.size:
            pos = int(np.searchsorted(self._sorted, np.uint64(d)))
            if pos < self._sorted.size and int(self._sorted[pos]) == d:
                return True
        return False

    def check_batch(self, values: Sequence[Any]) -> list[bool]:
        """批量判重：True = 与「索引中已有 + 本批更早的值」重复。**不修改索引。**

        这是「检测」与「解决」两阶段契约里的检测阶段，契约必须被调用方遵守：

        - **检测**（本方法）：一次性对整个分块判定，此时索引里只有更早分块的值，
          批内重复靠 np.unique 的 return_index 找出来。
        - **解决**：随后逐行处理回退，**每解决一行就立刻 add**。不能攒到最后批量 add ——
          考虑 [X_1, X, X]：row3 是 X 的重复，回退会去试 X_1，而 X_1 正是 row1 写出的值。
          如果 row1 的 X_1 还没进索引，row3 就会拿到重复的 X_1 并写进输出。
        """
        n = len(values)
        result = [False] * n
        if not n:
            return result
        digests, empty = self.digest_many(values)
        # np.unique 的 return_index 给的是「第一次出现」的下标集合，正好就是
        # 「本批内首次出现的值」—— 其余位置都是批内重复。
        _, first_idx = np.unique(digests, return_index=True)
        is_first = np.zeros(n, dtype=bool)
        is_first[first_idx] = True
        for i in range(n):
            if empty[i]:
                continue
            if not is_first[i]:
                result[i] = True     # 批内重复
        if self._sorted.size or self._pending_set:
            candidates = np.flatnonzero(is_first & ~empty)
            if candidates.size:
                hits = self.probe_many_digests(digests[candidates])
                for pos, hit in zip(candidates.tolist(), hits.tolist()):
                    if hit:
                        result[pos] = True
        return result

    def probe_many_digests(self, digests: np.ndarray) -> np.ndarray:
        """对一批已是摘要的值做成员查询。一次 searchsorted 而非 N 次，这是向量化真正省下来的。"""
        n = digests.size
        if not n:
            return np.zeros(0, dtype=bool)
        out = np.zeros(n, dtype=bool)
        if self._sorted.size:
            pos = np.searchsorted(self._sorted, digests)
            pos_clipped = np.minimum(pos, self._sorted.size - 1)
            out = self._sorted[pos_clipped] == digests
        if self._pending_set:
            pending = np.fromiter(self._pending_set, dtype=np.uint64, count=len(self._pending_set))
            if pending.size:
                ppos = np.searchsorted(np.sort(pending), digests)
                ppos_clipped = np.minimum(ppos, pending.size - 1)
                out |= np.sort(pending)[ppos_clipped] == digests
        return out

    def add(self, value: Any) -> bool:
        """接受一个值。返回 False 表示它是空值，被忽略。"""
        d = self.digest(value)
        if d is None:
            return False
        self._pending.append(d)
        self._pending_set.add(d)
        self._count += 1
        if self._log is not None:
            self._log.write(d.to_bytes(8, "little"))
        if len(self._pending) >= self.PENDING_LIMIT:
            self._compact()
        return True

    def add_digests(self, digests: np.ndarray) -> None:
        """批量接受一批摘要（check_batch 判定为不重复的那些）。"""
        for d in digests.tolist():
            self._pending.append(d)
            self._pending_set.add(int(d))
            self._count += 1
        if self._log is not None and digests.size:
            self._log.write(np.asarray(digests, dtype="<u8").tobytes())
        if len(self._pending) >= self.PENDING_LIMIT:
            self._compact()

    def _compact(self) -> None:
        if not self._pending:
            return
        pending = np.asarray(self._pending, dtype=np.uint64)
        self._sorted = (
            np.union1d(self._sorted, pending) if self._sorted.size else np.unique(pending)
        )
        self._pending = []
        self._pending_set = set()

    # -- 生命周期 ----------------------------------------------------------

    @property
    def count(self) -> int:
        return self._count

    def flush(self) -> None:
        """把日志刷到 OS。提交顺序要求它发生在 state.json 落盘之前。"""
        if self._log is not None:
            self._log.flush()

    def close(self) -> None:
        if self._log is not None:
            try:
                self._log.flush()
                self._log.close()
            finally:
                self._log = None

    def truncate_to(self, count: int) -> None:
        """把日志砍回 count 条。断点续跑的幂等性全靠这一步：崩溃时最后一段日志可能写了
        一半，而 state.json 还指向上一个提交点，于是多出来的条目必须被丢弃。"""
        self._count = count
        if self._log is not None:
            self._log.flush()
            self._log.truncate(count * 8)
            self._log.seek(0, 2)

    def clone(self) -> "UniqueIndex":
        """复制成员状态（**不复制日志句柄**，也不共享任何数组）。

        用于回归复检的对拍：列式快路径与逐行参考实现必须从同一个索引状态出发，
        否则比出来的差异全是假的。取一个成员快照而不是重放日志，是因为对拍要的是
        「现在这个状态」，而重放要读到当前日志长度、还要处理未提交的尾部。
        """
        other = UniqueIndex()
        other._sorted = self._sorted.copy()
        other._pending = list(self._pending)
        other._pending_set = set(self._pending_set)
        other._count = self._count
        return other

    def reset_visible(self) -> None:
        """丢掉内存里的成员，但**继续往同一个日志追加**。

        这是「文件作用域去重」在断点续跑下唯一站得住的做法：一个文件处理到一半崩溃，
        续跑时要从第 N 行接着跑，而此时索引里必须是「这个文件前 N 行接受过的值」。
        既然摘要按接受顺序写在日志里，那么只要记住「本文件开始时的日志长度」，
        重放这一段就精确还原了它 —— 但前提是重放与继续追加用的是同一个日志文件。
        换一个新日志文件会让长度坐标失去意义。
        """
        self._sorted = np.empty(0, dtype=np.uint64)
        self._pending = []
        self._pending_set = set()

    @classmethod
    def rebuild(cls, log_path, count: int, start: int = 0) -> "UniqueIndex":
        """从日志重建：读 [start, count) 这一段，然后 np.sort —— 恢复的全部代价就这一下。

        `start` 给的是「可见区间的起点」，用于文件作用域索引：同一份日志的前半段属于
        已经完成的文件，重放进来会把上一个文件的值当成已见过，从而错误地丢掉下一个
        文件的行。`count` 始终是**日志长度**，与 reset_visible 的坐标口径一致。
        """
        index = cls(log_path)
        if count <= 0:
            return index
        index._count = count
        if count > start:
            data = np.fromfile(log_path, dtype="<u8", count=count - start, offset=start * 8)
            index._sorted = np.unique(data)
        return index


# --------------------------------------------------------------------------- 违规判定


def _detail_for(constraint: Constraint, value: Any, unique_index=None) -> str:
    kind = constraint.kind
    if kind == "non_null":
        return "空值"
    if kind == "enum":
        return f"不在枚举内（{len(constraint.values)} 个允许值）"
    if kind == "regex":
        if constraint.pattern and compiled(constraint.pattern, constraint.mode).over_limit(value):
            return f"文本超过 {_RE_FALLBACK_MAX_LEN} 字符且该正则无法用 RE2 执行，未做匹配"
        return f"不匹配 /{constraint.pattern}/"
    if kind == "length":
        return f"长度 {len(_text(value))} 不在 [{constraint.min_length}, {constraint.max_length}] 内"
    if kind == "range":
        if to_float(value) is None:
            return "无法转为数值"
        return f"数值 {to_float(value)} 不在 [{constraint.min_value}, {constraint.max_value}] 内"
    if kind == "format":
        return f"不是合法的 {constraint.value_kind}"
    if kind == "type":
        return f"无法转为 {constraint.value_kind}"
    if kind == "unique":
        return "与已有值重复"
    return "违规"


def _check_one(value: Any, constraint: Constraint, field: str, unique_index) -> Violation | None:
    """单个约束的标量判定。参考实现的核心。"""
    kind = constraint.kind
    if kind == "non_null":
        return Violation(field, kind, value, "空值") if is_empty(value) else None
    if kind == "enum":
        text = _text(value)
        allowed = constraint.values
        if not constraint.case_sensitive:
            text = text.casefold()
            allowed = [v.casefold() for v in constraint.values]
        return None if text in allowed else Violation(field, kind, value, _detail_for(constraint, value))
    if kind == "regex":
        pattern = compiled(constraint.pattern, constraint.mode)
        return None if pattern.matches(value) else Violation(field, kind, value, _detail_for(constraint, value))
    if kind == "length":
        size = len(_text(value))
        low = constraint.min_length
        high = constraint.max_length
        if (low is not None and size < low) or (high is not None and size > high):
            return Violation(field, kind, value, _detail_for(constraint, value))
        return None
    if kind == "range":
        number = to_float(value)
        if number is None:
            return Violation(field, kind, value, "无法转为数值")
        low, high = constraint.min_value, constraint.max_value
        if (low is not None and number < low) or (high is not None and number > high):
            return Violation(field, kind, value, _detail_for(constraint, value))
        return None
    if kind == "format":
        return None if _format_ok(value, constraint) else Violation(field, kind, value, _detail_for(constraint, value))
    if kind == "type":
        return None if cast_check(value, constraint.value_kind) else Violation(field, kind, value, _detail_for(constraint, value))
    if kind == "unique":
        if unique_index is None or is_empty(value):
            return None
        return Violation(field, kind, value, "与已有值重复") if unique_index.probe(value) else None
    return None


def _format_ok(value: Any, constraint: Constraint) -> bool:
    kind = constraint.value_kind
    if kind in ("date", "datetime", "time"):
        return parse_datetime(value, constraint.date_formats) is not None
    text = _text(value).strip()
    if kind == "email":
        return bool(_EMAIL_RE.match(text))
    if kind == "url":
        return bool(_URL_RE.match(text))
    if kind == "ipv4":
        match = _IPV4_RE.match(text)
        return bool(match) and all(0 <= int(part) <= 255 for part in match.groups())
    return False


def check_value(
    value: Any, constraints: Sequence[Constraint], field: str, unique_index=None
) -> Violation | None:
    """逐行标量参考实现。慢，但显然正确。

    它有两个用途：等价性测试的基准，以及回归时对抽样块的对拍。**不要**在热路径上调用它 ——
    eval_column 才是执行路径，两者必须给出相同结果，由测试与可选抽查保证。
    """
    for constraint in _ordered(constraints):
        if constraint.severity != "error":
            continue
        violation = _check_one(value, constraint, field, unique_index)
        if violation is not None:
            return violation
    return None


def _fail_indices(
    values: Sequence[Any], pending: Sequence[int], constraint: Constraint, unique_index
) -> list[int]:
    """列式快路径：一次扫完这一列，返回违规的绝对下标。

    这里省下来的是「每行 × 每个约束」的重新分派与排序：不再逐行走一遍 _ordered，
    而是一个约束一趟地把整列扫过去。语义与 _check_one 逐行调用完全一致 —— 同一个
    正则引擎、同一套比较、同一个空值定义。
    """
    kind = constraint.kind
    if kind == "non_null":
        return [i for i in pending if is_empty(values[i])]
    if kind == "enum":
        allowed = set(constraint.values)
        if constraint.case_sensitive:
            return [i for i in pending if _text(values[i]) not in allowed]
        folded = {v.casefold() for v in constraint.values}
        return [i for i in pending if _text(values[i]).casefold() not in folded]
    if kind == "regex":
        pattern = compiled(constraint.pattern, constraint.mode)
        return [i for i in pending if not pattern.matches(values[i])]
    if kind == "length":
        low, high = constraint.min_length, constraint.max_length
        out = []
        for i in pending:
            size = len(_text(values[i]))
            if (low is not None and size < low) or (high is not None and size > high):
                out.append(i)
        return out
    if kind == "range":
        low, high = constraint.min_value, constraint.max_value
        out = []
        for i in pending:
            number = to_float(values[i])
            if number is None or (low is not None and number < low) or (high is not None and number > high):
                out.append(i)
        return out
    if kind == "format":
        return [i for i in pending if not _format_ok(values[i], constraint)]
    if kind == "type":
        return [i for i in pending if not cast_check(values[i], constraint.value_kind)]
    if kind == "unique":
        if unique_index is None:
            return []
        batch = [values[i] for i in pending]
        duplicates = unique_index.check_batch(batch)
        return [i for i, dup in zip(pending, duplicates) if dup]
    return []


def eval_column(
    values: Sequence[Any], constraints: Sequence[Constraint], field: str, unique_index=None
) -> list[Violation | None]:
    """列式执行路径。返回与 values 等长的违规列表（None = 通过，取优先级最高的那一种）。

    后面的约束只在前面已通过的行上求值（短路）。这既是提速，也是与参考实现一致的必要
    条件 —— 参考实现同样是遇到第一个 error 级违规就返回。
    """
    n = len(values)
    result: list[Violation | None] = [None] * n
    pending = list(range(n))
    for constraint in _ordered(constraints):
        if not pending:
            break
        if constraint.severity != "error":
            continue
        bad = _fail_indices(values, pending, constraint, unique_index)
        if not bad:
            continue
        for i in bad:
            result[i] = Violation(field, constraint.kind, values[i], _detail_for(constraint, values[i]))
        dropped = set(bad)
        pending = [i for i in pending if i not in dropped]
    return result


def count_warnings(
    values: Sequence[Any], constraints: Sequence[Constraint], field: str
) -> dict[str, int]:
    """warn 级约束的违规计数。只记账，不影响数据 —— 也不参与短路，所以每个 warn 约束
    都是独立扫一遍全列。warn 级约束不该配太多，这是它便宜的原因也是它的代价。"""
    counts: dict[str, int] = {}
    for constraint in _ordered(constraints):
        if constraint.severity != "warn":
            continue
        bad = _fail_indices(values, list(range(len(values))), constraint, None)
        if bad:
            counts[f"{field}:{constraint.kind}"] = len(bad)
    return counts


# --------------------------------------------------------------------------- 回退实施


def apply_fallback(
    value: Any,
    violation: Violation,
    fallback: Fallback,
    constraint: Constraint,
    unique_index=None,
) -> FallbackResult:
    """实施 keep / truncate 两种回退。retry 由引擎循环处理（只有它知道怎么重新生成值）。

    「truncate」对不同违规类型必须是不同动作，否则这个词是句空话：截断一个枚举值没有
    意义，钳制一个正则匹配也没有意义。无法实施时降级为 keep 并标 degraded —— 引擎据此
    打告警，这样「实施成功」与「悄悄放弃」在日志里分得清。

    constraint 是必需的入参而不是从 violation.detail 里反解：边界值（min_length、
    min_value、pad_char、正则的 mode）只存在于约束本身，靠解析自己刚生成的详情字符串
    取回来是脆的 —— 改一句文案就静默失效。
    """
    policy = fallback.on_violation
    kind = violation.kind
    if policy == "retry":
        raise ValueError("retry 策略由引擎循环处理，不应进入 apply_fallback")
    if policy == "keep":
        return FallbackResult(value, False)

    # ---- truncate ----
    if kind == "non_null":
        replacement = fallback.default if fallback.default is not None else ""
        return FallbackResult(replacement, replacement != value, note="填入默认值")

    if kind == "enum":
        return FallbackResult(value, False, degraded=True, note="枚举值无法截断，保留原值")

    if kind == "format":
        return FallbackResult(
            value, False, degraded=True, note=f"{violation.detail}，无法修补，保留原值"
        )

    if kind == "regex":
        if constraint.mode == "search":
            matched = compiled(constraint.pattern, constraint.mode).find(value)
            if matched:
                return FallbackResult(matched, matched != value, note="保留匹配到的子串")
        return FallbackResult(
            value, False, degraded=True, note="fullmatch 正则无法自动修复，保留原值"
        )

    if kind == "length":
        text = _text(value)
        low, high = constraint.min_length, constraint.max_length
        if high is not None and len(text) > high:
            return FallbackResult(text[:high], True, note=f"截断到 {high} 字符")
        if low is not None and len(text) < low:
            padded = text + constraint.pad_char * (low - len(text))
            return FallbackResult(padded, True, note=f"右侧补齐到 {low} 字符")
        return FallbackResult(value, False)

    if kind == "range":
        number = to_float(value)
        if number is None:
            return FallbackResult(value, False, degraded=True, note="非数值，无法钳制，保留原值")
        low, high = constraint.min_value, constraint.max_value
        clamped = number
        if low is not None and number < low:
            clamped = low
        if high is not None and number > high:
            clamped = high
        if clamped == number:
            return FallbackResult(value, False)
        # 整数列不要因为钳制变成 3.0：原值看起来是整数就还整数。
        text = _text(value).strip()
        out: Any = int(clamped) if text.lstrip("+-").isdigit() else clamped
        return FallbackResult(out, True, note=f"钳制到 [{low}, {high}]")

    if kind == "unique":
        if unique_index is None:
            return FallbackResult(
                value, False, degraded=True, note="没有唯一性索引，无法生成唯一后缀"
            )
        base = _text(value)
        for attempt in range(1, 10001):
            candidate = f"{base}{fallback.unique_suffix}{attempt}"
            if not unique_index.probe(candidate):
                return FallbackResult(
                    candidate, True, note=f"追加后缀 {fallback.unique_suffix}{attempt}"
                )
        return FallbackResult(value, False, degraded=True, note="后缀尝试 10000 次仍未唯一")

    return FallbackResult(value, False, degraded=True, note="未知违规类型")
