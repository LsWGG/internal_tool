"""统计与回归复检。

这一层只做**一次有界扫描能算出来的东西**。GB 级文件装不进内存，也没有精确的单遍有界算法，
所以每个数字都要么精确、要么**明确标注是近似**并且能说出误差来源。报告里宁可写「不做统计」，
也不要印一个用户会相信的假数字 —— 后者比空白危险得多。

| 统计量 | 精度 |
|---|---|
| 行数、空值率、长度 min/max/mean | 精确 |
| 数值 mean/stddev（Welford，跨批并行合并） | 精确（按「这列是数值列」的判定） |
| 类型分布 | 精确（按文档化的推断规则） |
| Top-K 频次 | 基数 ≤ limit 时精确；溢出后 Space-Saving，误差上界 N/limit |
| p50 / p95 | **近似**：蓄水池采样，报告里标注样本量与样本数 |
| 中位数、p99、精确去重计数 | **不算**。没有精确单遍有界算法 |

「这列是数值列」的判定：前 `_NUMERIC_PROBE` 个非空值里有 `_NUMERIC_RATIO` 以上能被
`to_float` 解析，就打开该字段的数值累加器，且**一旦打开就不再关闭**（黏性）。黏性是为了续跑：
判定依赖「看到过的前几个值」，而续跑看到的第一批不是原来那个第一批。黏性 + 持久化让判定
在重启后不会翻转，否则同一个任务的报告会因为崩溃重启而变一个样子。

成本账（实测口径，写在这里免得后来者以为是免费的）：全字符串列每 5 万格约 10–15ms，
其中长度一趟、频次一趟是 C 层，蓄水池与数值解析是 numpy。一个 20M 行 × 6 列的文件两次
统计（清洗前/后）约 40–60 秒，与整条流水线同量级。要省掉它就得砍掉报告的核心内容。
"""

from __future__ import annotations

import heapq
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .clean_schema import UniqueIndex, cell_text, check_value, eval_column, is_empty, to_float

# 类型分布的类别。顺序固定（状态里按这个顺序存），空值单列一类且不参与长度/数值统计。
TYPE_NAMES = ("empty", "str", "int", "float", "bool", "date", "datetime", "other")

_NUMERIC_PROBE = 200
_NUMERIC_RATIO = 0.8
_NUMERIC_MIN_PROBE = 4
"""判定「这列是数值列」至少要看几个非空值。太少（比如 1 个）会把「这一列恰好有个数字」
判成数值列，于是报告里出现一个基于两三个值的均值 —— 不如不报。"""


def _probe_sample(values: Sequence[Any]) -> list[Any]:
    sample: list[Any] = []
    for value in values:
        if is_empty(value):
            continue
        sample.append(value)
        if len(sample) >= _NUMERIC_PROBE:
            break
    return sample


class CleanStatsError(ValueError):
    pass


# =========================================================================== 频次


class FrequencyCounter:
    """Top-K 频次：基数不超上限时**精确**，超了走 Space-Saving。

    Space-Saving：槽位固定为 `limit` 个，新值顶掉当前计数最小的槽位并**继承它的计数**。
    于是任何一个值的报告计数满足 `真实 <= 报告 <= 真实 + N/limit`，这个上界进报告。

    最小值用惰性堆找：每次更新往堆里压一份 `(count, seq, key)`，取最小值时弹掉与字典
    不符的过期项。真去维护一个堆的 decrease-key 要引入额外结构，而惰性堆的代价只是
    堆比字典大几倍 —— 上限只有几千条，可以忽略。
    """

    def __init__(self, limit: int = 1000):
        if limit < 1:
            raise CleanStatsError("频次上限必须 >= 1")
        self.limit = limit
        self.counts: dict[str, int] = {}
        self.total = 0
        self.overflow = False
        self.warm = False
        self._heap: list[tuple[int, int, str]] = []
        self._seq = 0

    def add_many(self, batch: Mapping[str, int]) -> None:
        for key, count in batch.items():
            self.add(key, count)

    def add(self, key: str, count: int = 1) -> None:
        self.total += count
        current = self.counts.get(key)
        if current is not None:
            self.counts[key] = current + count
            self._push(key, current + count)
            return
        if len(self.counts) < self.limit:
            self.counts[key] = count
            self._push(key, count)
            return
        self.overflow = True
        victim, victim_count = self._evict_min()
        self.counts[key] = victim_count + count
        self._push(key, victim_count + count)
        del victim

    def _push(self, key: str, count: int) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (count, self._seq, key))

    def _evict_min(self) -> tuple[str, int]:
        while self._heap:
            count, _, key = heapq.heappop(self._heap)
            if self.counts.get(key) == count:
                del self.counts[key]
                return key, count
        raise CleanStatsError("频次堆已空但字典非空：惰性堆的过期判定出错了")

    @property
    def exact(self) -> bool:
        return not self.overflow and not self.warm

    @property
    def error_bound(self) -> int:
        """报告计数的最大高估量（Space-Saving 的经典上界）。"""
        return 0 if not self.overflow else int(self.total / self.limit) + 1

    def top(self, k: int) -> list[tuple[str, int]]:
        # 计数相同时按值排序：报告必须稳定，否则两次运行会看到顺序不同的同一个结果
        ordered = sorted(self.counts.items(), key=lambda item: (-item[1], item[0]))
        return ordered[:k]

    def snapshot(self, keep: int) -> dict[str, Any]:
        """只留前 keep 条进 state.json。候选集可能是几千条，全存进去会让每次提交都变大。"""
        return {
            "counts": dict(self.top(keep)),
            "total": self.total,
            "overflow": self.overflow,
            "warm": self.warm or self.overflow,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        counts = state.get("counts") or {}
        for key, count in counts.items():
            self.counts[str(key)] = int(count)
            self._push(str(key), int(count))
        self.total = int(state.get("total", 0))
        self.overflow = bool(state.get("overflow", False))
        # 热启动后**精确性已经丢了**：cut 点排在 keep 之外的值后来可能长进前 K。
        # 这时还声称「精确」就是撒谎，所以一律按近似报告。
        self.warm = bool(state.get("warm", True))


# =========================================================================== 蓄水池


class Reservoir:
    """定长蓄水池采样（Algorithm R），numpy 批量向量化。

    Algorithm R 的每一步只依赖「第 i 个元素」和一次 `randint(0, i)`，与池里已有的**值**
    无关，所以一批可以一次算完所有随机下标：对每个位置 i 取 j=randint(0,i)，j<k 的那个
    位置贡献一次写入，同一个槽位在本批里被写多次时最后一个赢 —— 与逐元素顺序执行等价，
    而随机数消耗量也完全一致。`seen` 是全局序号，续跑时要接着它走。
    """

    def __init__(self, size: int, rng: np.random.Generator):
        if size < 1:
            raise CleanStatsError("蓄水池容量必须 >= 1")
        self.size = size
        self.rng = rng
        self.seen = 0
        self.sample: list[float] = []

    def add_array(self, values: np.ndarray) -> None:
        n = int(values.size)
        if not n:
            return
        start = self.seen
        self.seen += n
        if len(self.sample) < self.size:
            room = self.size - len(self.sample)
            head = values[:room]
            self.sample.extend(float(v) for v in head)
            offset = head.size
        else:
            offset = 0
        remaining = values[offset:]
        if remaining.size:
            positions = np.arange(start + offset, start + offset + remaining.size)
            picks = self.rng.integers(0, positions + 1)
            hit = picks < self.size
            if hit.any():
                # 同一槽位在本批里被写多次时**最后一次赢**，与逐元素顺序执行等价
                reversed_hits = picks[hit][::-1]
                unique_slots, first_in_reversed = np.unique(
                    reversed_hits, return_index=True
                )
                positions_in_batch = hit.nonzero()[0][::-1][first_in_reversed]
                for slot, pos in zip(unique_slots, positions_in_batch):
                    self.sample[int(slot)] = float(remaining[int(pos)])

    def percentile(self, q: float) -> float | None:
        """最近秩法。样本为空返回 None —— 报告要能区分「0」和「没有数据」。"""
        if not self.sample:
            return None
        ordered = sorted(self.sample)
        rank = int(round(q * (len(ordered) - 1)))
        return ordered[max(0, min(rank, len(ordered) - 1))]

    @property
    def approximate(self) -> bool:
        return self.seen > len(self.sample)

    def snapshot(self) -> dict[str, Any]:
        return {"seen": self.seen, "sample": self.sample}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.seen = int(state.get("seen", 0))
        self.sample = [float(v) for v in (state.get("sample") or [])]


# =========================================================================== 单字段


class FieldStats:
    """一个字段（一侧）的有界统计。状态大小与行数无关。"""

    def __init__(self, name: str, *, limit: int = 1000, reservoir_size: int = 2000, rng=None):
        self.name = name
        self.freq = FrequencyCounter(limit)
        self.count = 0
        self.empty = 0
        self.types: Counter[str] = Counter()
        self.length_sum = 0
        self.length_min: int | None = None
        self.length_max: int | None = None
        self.numeric = False
        self.num_count = 0
        self.num_mean = 0.0
        self.num_m2 = 0.0
        self.lengths = Reservoir(
            reservoir_size, rng if rng is not None else np.random.default_rng(0)
        )
        self.values = Reservoir(
            reservoir_size, rng if rng is not None else np.random.default_rng(0)
        )

    # -- 喂数据 ------------------------------------------------------------

    def update(self, values: Sequence[Any]) -> None:
        n = len(values)
        if not n:
            return
        self.count += n
        if _all_str(values):
            self._update_text(values, n)
        else:
            self._update_mixed(values, n)

    def _update_text(self, values: Sequence[str], n: int) -> None:
        """全字符串列的快路径（CSV / xlsx 文本列，绝大多数情形）。

        每一趟都是 C 层的：`list.count` 数空串、列表推导求长度、`Counter` 直接吃列表。
        """
        empty = values.count("")
        self.empty += empty
        # 只在非零时加：Counter 上 += 0 会凭空造出一个值为 0 的键，让快慢两条路径的
        # 序列化结果不一致（一个有空类目一个没有），报告里的类型分布就会随路径变形
        if empty:
            self.types["empty"] += empty
        if n != empty:
            self.types["str"] += n - empty
        lengths = [len(value) for value in values]
        self._note_lengths(lengths)
        frequencies = Counter(values)
        # 空值不进频次分布：空值率已经单独报了，让它占据 Top-K 的一行只是噪音
        frequencies.pop("", None)
        self.freq.add_many(frequencies)
        numbers = self._maybe_numbers(values)
        if numbers is not None:
            self._accumulate(numbers=numbers)

    def _update_mixed(self, values: Sequence[Any], n: int) -> None:
        """混合类型列（用户函数/生成器产出的数值列就是这一类）。慢，但正确。"""
        lengths = []
        texts = []
        for value in values:
            code = _type_code(value)
            self.types[code] += 1
            if code == "empty":
                self.empty += 1
                lengths.append(0)
                continue
            text = cell_text(value)
            texts.append(text)
            lengths.append(len(text))
        self._note_lengths(lengths)
        self.freq.add_many(Counter(texts))
        numbers = self._maybe_numbers(values)
        if numbers is not None:
            self._accumulate(numbers=numbers)

    def _maybe_numbers(self, values: Sequence[Any]) -> np.ndarray | None:
        """数值列判定的唯一入口（两条路径共用），返回可进数值统计的值或 None。

        判定用**本批的前几个非空值**，而不是「攒够 N 个值再决定」：后者会让小文件
        （只有一批数据）永远拿不到数值统计，而那是最常见的一类输入。判定成立后就黏住
        （见模块 docstring）。纯文本列每批只为探针付几十次解析，可以忽略。
        """
        if not self.numeric:
            sample = _probe_sample(values)
            if not sample or len(sample) < _NUMERIC_MIN_PROBE:
                return None
            hits = sum(1 for value in sample if to_float(value) is not None)
            if hits < _NUMERIC_RATIO * len(sample):
                return None
            self.numeric = True
        return _parse_numbers(values)

    def _note_lengths(self, lengths: Sequence[int]) -> None:
        """长度统计。**空值不参与**（min/max/mean/分位口径一致）：

        空值记作长度 0，会让「长度区间」从 `1~20` 变成 `0~20`，读起来像是有零长度数据；
        而分位数如果算上这些 0，一列 40% 为空的数据就会被报成「中位长度 0」——
        那是个假象，不是事实。求和时加 0 不影响结果，分母用非空个数，两边一致。
        """
        if not lengths:
            return
        self.length_sum += sum(lengths)
        zeros = lengths.count(0)          # C 层，干净列（无空值）几乎不花时间
        if zeros == len(lengths):
            return                        # 整批都空：什么都不记
        filled = [length for length in lengths if length] if zeros else list(lengths)
        low, high = min(filled), max(filled)
        self.length_min = low if self.length_min is None else min(self.length_min, low)
        self.length_max = high if self.length_max is None else max(self.length_max, high)
        self.lengths.add_array(np.asarray(filled, dtype=np.float64))

    def _accumulate(self, *, numbers: np.ndarray) -> None:
        if numbers.size == 0:
            return
        self.values.add_array(numbers)
        # 跨批合并用并行 Welford 的合并公式（Chan 等），与逐元素 Welford 等价到浮点误差
        n_b = int(numbers.size)
        mean_b = float(numbers.mean())
        m2_b = float(((numbers - mean_b) ** 2).sum())
        n_a = self.num_count
        if n_a == 0:
            self.num_count, self.num_mean, self.num_m2 = n_b, mean_b, m2_b
            return
        delta = mean_b - self.num_mean
        total = n_a + n_b
        self.num_mean += delta * n_b / total
        self.num_m2 += m2_b + delta * delta * n_a * n_b / total
        self.num_count = total

    # -- 读取 --------------------------------------------------------------

    @property
    def empty_rate(self) -> float:
        return self.empty / self.count if self.count else 0.0

    @property
    def length_mean(self) -> float:
        filled = self.count - self.empty
        return self.length_sum / filled if filled else 0.0

    @property
    def numeric_stddev(self) -> float:
        if self.num_count < 2:
            return 0.0
        return (self.num_m2 / (self.num_count - 1)) ** 0.5

    def percentile_of(self) -> str:
        """分位数报的是「长度」还是「数值」—— 由这一列是不是数值列决定，报告里要写明。"""
        return "value" if self.numeric else "length"

    def percentile(self, q: float) -> float | None:
        return (self.values if self.numeric else self.lengths).percentile(q)

    def percentiles_approximate(self) -> bool:
        return (self.values if self.numeric else self.lengths).approximate

    @property
    def sample_size(self) -> int:
        return len((self.values if self.numeric else self.lengths).sample)

    # -- 状态 --------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "empty": self.empty,
            "types": dict(self.types),
            "length": [self.length_min, self.length_max, self.length_sum],
            "numeric": self.numeric,
            "welford": [self.num_count, self.num_mean, self.num_m2],
            "freq": self.freq.snapshot(_SNAPSHOT_TOP),
            "lengths": self.lengths.snapshot(),
            "values": self.values.snapshot(),
        }

    @classmethod
    def restore(cls, name: str, state: Mapping[str, Any], *, limit=1000, reservoir_size=2000, rng=None):
        stat = cls(name, limit=limit, reservoir_size=reservoir_size, rng=rng)
        stat.count = int(state.get("count", 0))
        stat.empty = int(state.get("empty", 0))
        stat.types = Counter({str(k): int(v) for k, v in (state.get("types") or {}).items()})
        low, high, total = state.get("length") or [None, None, 0]
        stat.length_min = None if low is None else int(low)
        stat.length_max = None if high is None else int(high)
        stat.length_sum = int(total)
        stat.numeric = bool(state.get("numeric", False))
        seen, mean, m2 = state.get("welford") or [0, 0.0, 0.0]
        stat.num_count, stat.num_mean, stat.num_m2 = int(seen), float(mean), float(m2)
        stat.freq.restore(state.get("freq") or {})
        stat.lengths.restore(state.get("lengths") or {})
        stat.values.restore(state.get("values") or {})
        return stat


_SNAPSHOT_TOP = 50
"""每个字段进 state.json 的频次条目上限。候选集可能有几千条，全存会让每次提交都变大，
而报告只会展示前 K 条 —— 存 top_k 就够，热启动后精确性由 freq.warm 标记出来。"""


def _all_str(values: Sequence[Any]) -> bool:
    return isinstance(values[0], str) and all(isinstance(value, str) for value in values)


def _type_code(value: Any) -> str:
    if is_empty(value):
        return "empty"
    if isinstance(value, bool):
        return "bool"       # 在 int 之前判：bool 是 int 的子类
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"
    return "other"


def _parse_numbers(values: Sequence[Any]) -> np.ndarray:
    """能被 to_float 解析的值。不吞掉解析不了的那些，它们只是不进数值统计（长度统计照旧）。"""
    parsed = [to_float(value) for value in values]
    numbers = [number for number in parsed if number is not None]
    if not numbers:
        return np.zeros(0, dtype=np.float64)
    array = np.asarray(numbers, dtype=np.float64)
    # NaN/inf 会让 mean/stddev 变成 NaN，一个数字都没有比一个 NaN 有用
    return array[np.isfinite(array)]


# =========================================================================== 字段集合


def stat_keys(columns: Sequence[str]) -> list[str]:
    """统计用的列名。重名表头（I/O 层刻意不去重）在这里加 `#2` 后缀区分 ——
    把两列合并统计会得到一个谁都不认识的数字。"""
    seen: dict[str, int] = {}
    keys = []
    for name in columns:
        seen[name] = seen.get(name, 0) + 1
        keys.append(name if seen[name] == 1 else f"{name}#{seen[name]}")
    return keys


class StatsSet:
    """一侧（清洗前或清洗后）的全部字段统计。"""

    def __init__(self, *, limit=1000, reservoir_size=2000, seed=20260926):
        self.limit = limit
        self.reservoir_size = reservoir_size
        self.rng = np.random.default_rng(seed)
        self.fields: dict[str, FieldStats] = {}
        self.rows = 0

    def update(self, columns: Sequence[str], data: Sequence[Sequence[Any]]) -> None:
        if data:
            self.rows += len(data[0])
        for key, column in zip(stat_keys(columns), data):
            stat = self.fields.get(key)
            if stat is None:
                stat = self.fields[key] = FieldStats(
                    key, limit=self.limit, reservoir_size=self.reservoir_size, rng=self.rng
                )
            stat.update(column)

    def field(self, name: str) -> FieldStats | None:
        return self.fields.get(name)

    def snapshot(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "fields": {k: v.snapshot() for k, v in self.fields.items()},
            # 随机数生成器的状态**必须**一起存：蓄水池抽样的结果是 p50/p95 的来源，
            # 续跑时若换一条新的随机流，同一个任务「中断后继续」与「一口气跑完」会给出
            # 不同的分位数 —— 报告是回归验证的产物，它自己却不稳定，那就没有意义了。
            "rng": _encode_ints(self.rng.bit_generator.state),
        }

    @classmethod
    def restore(cls, state: Mapping[str, Any], *, limit=1000, reservoir_size=2000, seed=20260926):
        stats = cls(limit=limit, reservoir_size=reservoir_size, seed=seed)
        stats.rows = int(state.get("rows", 0))
        saved = state.get("rng")
        if saved:
            # 生成器类型对不上时 numpy 自己会抛（状态里带着名字）—— 让它抛，别悄悄退回
            # 种子流的起点：那会让续跑后的统计静默地与不中断运行不一致。
            stats.rng.bit_generator.state = _decode_ints(saved)
        for name, field_state in (state.get("fields") or {}).items():
            stats.fields[name] = FieldStats.restore(
                name,
                field_state,
                limit=limit,
                reservoir_size=reservoir_size,
                rng=stats.rng,
            )
        return stats


def _encode_ints(value: Any) -> Any:
    """把嵌套结构里的整数转成十进制字符串（只用于随机数生成器的状态）。

    PCG64 的内部状态是两个 128 位整数，而 `state.json` 走 orjson，orjson 对超出 64 位的
    整数直接抛错。十进制字符串是唯一既无损又对任何序列化器都安全的表示 ——
    JSON 的数字在标准里没有精度上限，但实现有。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _encode_ints(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_ints(item) for item in value]
    return value


def _decode_ints(value: Any) -> Any:
    """`_encode_ints` 的逆。只用在生成器状态上 —— 那里的叶子只有整数和一个生成器名。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value          # 生成器名（"PCG64"）走这里
    if isinstance(value, Mapping):
        return {str(key): _decode_ints(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_ints(item) for item in value]
    return value


# =========================================================================== 违规与回退计数


class Counters:
    """违规与回退的计数。**计数永远精确**，与告警日志的有界化无关（见 clean_engine）。

    键的形态是元组，序列化时拼成 `"字段|类型"` —— 报告要按字段分组，用元组做键最直接，
    而 state.json 只存字符串。
    """

    def __init__(self):
        self.violations: Counter[tuple[str, str]] = Counter()
        self.fallbacks: Counter[tuple[str, str, str]] = Counter()
        self.degraded = 0
        self.retries = 0
        self.llm_calls = 0
        self.llm_failures = 0
        self.llm_cache_hits = 0

    # -- 违规 --------------------------------------------------------------

    def note_violation(self, field: str, kind: str) -> None:
        self.violations[(field, kind)] += 1

    def note_fallback(self, field: str, kind: str, policy: str, *, degraded: bool) -> None:
        self.fallbacks[(field, kind, policy)] += 1
        if degraded:
            self.degraded += 1

    def field_violations(self, field: str) -> dict[str, int]:
        return {kind: n for (name, kind), n in self.violations.items() if name == field}

    def field_fallbacks(self, field: str) -> dict[tuple[str, str], int]:
        return {
            (kind, policy): n
            for (name, kind, policy), n in self.fallbacks.items()
            if name == field
        }

    def total_violations(self) -> int:
        return sum(self.violations.values())

    def total_fallbacks(self) -> int:
        return sum(self.fallbacks.values())

    def snapshot(self) -> dict[str, Any]:
        return {
            "violations": {f"{f}|{k}": n for (f, k), n in self.violations.items()},
            "fallbacks": {f"{f}|{k}|{p}": n for (f, k, p), n in self.fallbacks.items()},
            "degraded": self.degraded,
            "retries": self.retries,
            "llm_calls": self.llm_calls,
            "llm_failures": self.llm_failures,
            "llm_cache_hits": self.llm_cache_hits,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        for key, count in (state.get("violations") or {}).items():
            field, kind = str(key).split("|", 1)
            self.violations[(field, kind)] = int(count)
        for key, count in (state.get("fallbacks") or {}).items():
            field, kind, policy = str(key).split("|", 2)
            self.fallbacks[(field, kind, policy)] = int(count)
        self.degraded = int(state.get("degraded", 0))
        self.retries = int(state.get("retries", 0))
        self.llm_calls = int(state.get("llm_calls", 0))
        self.llm_failures = int(state.get("llm_failures", 0))
        self.llm_cache_hits = int(state.get("llm_cache_hits", 0))


# =========================================================================== 回归复检


def cross_check(
    values: Sequence[Any],
    constraints: Sequence[Any],
    field: str,
    index: UniqueIndex | None = None,
) -> list[str]:
    """列式快路径 vs 逐行参考实现的**整批**对拍，返回不一致的描述（空 = 一致）。

    列式优化（eval_column）的许可证是「与逐行 check_value 等价」。等价性一旦破了，报告里
    所有「清洗后违规数」都是错的 —— 而那正是用户唯一会相信的那一列数字。所以 verify_every
    打开时，引擎每 N 批拿真数据跑一次这个对拍。

    **调用方控制频率**（逐行参考实现慢约 20×，所以只在抽样批次上跑），而本函数内部
    逐行比对**整个批**：unique 是有状态的约束，从批中间抽几行单独复算会得到不同的索引
    状态，那样比出来的差异是假的。参考索引从 `index` 克隆，两边从同一个状态出发。
    """
    ref_index = index.clone() if index is not None else None
    col_index = index.clone() if index is not None else None
    column = eval_column(values, constraints, field, col_index)
    problems: list[str] = []
    for position, value in enumerate(values):
        expected = check_value(value, constraints, field, ref_index)
        if expected is None and ref_index is not None:
            ref_index.add(value)
        got = column[position] if position < len(column) else None
        want_kind = expected.kind if expected else None
        got_kind = got.kind if got else None
        if want_kind != got_kind:
            if len(problems) < 5:  # 只留前几条：一个系统性 bug 会产生几百万条同样的差异
                problems.append(
                    f"字段 {field!r} 第 {position} 行值 {value!r}："
                    f"逐行判为 {want_kind or '通过'}，列式判为 {got_kind or '通过'}"
                )
            elif len(problems) == 5:
                problems.append("（后续差异省略）")
    return problems


def regression_findings(
    before: StatsSet,
    after: StatsSet,
    counters: Counters,
    fields: Iterable[str],
) -> list[str]:
    """回归复检的结论条目（「规则符合性 + 前后统计对比」里的对比部分）。

    只报告**能从统计里确定的事**，不下「数据变好了」这种判断 —— 那是用户的判断。
    每条结论都带数字，让用户可以自己核对。
    """
    findings: list[str] = []
    for name in fields:
        old, new = before.field(name), after.field(name)
        if old is None or new is None:
            findings.append(f"- `{name}`：只有单侧统计（另一侧没有这一列），无法对比")
            continue
        if old.empty != new.empty:
            findings.append(
                f"- `{name}` 空值：{old.empty} → {new.empty}"
                f"（{_pct(old.empty_rate)} → {_pct(new.empty_rate)}）"
            )
        if old.length_min != new.length_min or old.length_max != new.length_max:
            findings.append(
                f"- `{name}` 长度区间：{old.length_min}~{old.length_max}"
                f" → {new.length_min}~{new.length_max}"
            )
        if old.numeric and new.numeric and abs(old.num_mean - new.num_mean) > 1e-9:
            findings.append(
                f"- `{name}` 均值：{old.num_mean:.4f} → {new.num_mean:.4f}"
                f"（标准差 {old.numeric_stddev:.4f} → {new.numeric_stddev:.4f}）"
            )
        if old.freq.exact and new.freq.exact:
            old_top = dict(old.freq.top(20))
            new_top = dict(new.freq.top(20))
            only_before = sorted(set(old_top) - set(new_top))
            only_after = sorted(set(new_top) - set(old_top))
            if only_before or only_after:
                findings.append(
                    f"- `{name}` 高频值变化："
                    f"{_preview(only_before)} 退出前 20，{_preview(only_after)} 进入前 20"
                )
    if not findings:
        findings.append("- 前后统计没有可报告的变化（空值、长度区间、均值、高频值均一致）")
    return findings


def _pct(ratio: float) -> str:
    return f"{ratio * 100:.2f}%"


def _preview(values: Sequence[str], limit: int = 3) -> str:
    if not values:
        return "（无）"
    shown = "、".join(f"`{value}`" for value in values[:limit])
    return shown if len(values) <= limit else f"{shown} 等 {len(values)} 个"
