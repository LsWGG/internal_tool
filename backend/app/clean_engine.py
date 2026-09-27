"""清洗引擎：读入 → 映射 → 清洗 → 生成 → 约束回退 → 写出 → 复检。

这一层不做 HTTP、不碰上传目录、不管任务列表 —— 那些是 `clean_manager` / `clean_api`。
引擎的输入是 `(key, 绝对路径)` 列表与配置，输出是产物 + 报告 + `TaskState`。

# 四个必须一起理解的约定

## 1. 提交顺序就是一致性协议

每一批的提交动作**必须**按这个顺序：

    writer.flush() → 索引摘要 flush → 事件/回退日志 flush → state.json（原子替换）

`state.json` 是唯一的提交记录。它写完了，说明前面几步都已经 flush 过（同进程里 flush 的
顺序就是到达 OS 的顺序，不 fsync 也成立 —— 只有断电才需要 fsync，那是 `durable_commit`）。
它写到一半崩了，前面几步做的就都不算数：恢复时所有 append-only 文件都被砍回快照里的长度，
重放严格幂等。**反过来先写 state.json 就全毁了**：状态会指向没落盘的行。

## 2. 批边界

提交只发生在完整的读取批之后，所以 `FileState.rows_in` 永远是读取批大小的整数倍 ——
这是 `TaskProgress` 要求的（前后两侧的蓄水池共用一条随机流，批切分变了 p50/p95 就变了）。
读取批大小由 `commit_rows` 派生，整轮任务不变。短批只可能出现在文件末尾，而那时文件已经
跑完，不存在「从短批中间续跑」这种情况。

## 3. 产物列 = 目的字段

批表里同时有**源列与目的字段**（所以算子可以按源列名写，也可以按目的字段名写 ——
"按一个你不保留的列去重"因此是合法的），但**只有 `config.dest_fields()` 按顺序写出去**。
源列里没被任何目的字段引用的部分会列在报告的「未映射列」里 —— 默默丢列比报错更难查。

## 4. 为什么文件是串行处理的（不是没做并发）

用户要求了「异步 + 并发」，这里要说清楚并发的**位置**，因为它不在文件之间：

- **大模型**：`LlmPool` 按端点的 `max_concurrency` 并行发请求，这是 GB 级任务真正的
  瓶颈所在，也是负载均衡那一整套存在的理由。
- **用户 Python 函数**：子进程（真并行），只是父进程按批等它。
- **列式求值**：numpy / RE2 在扫列时释放 GIL。
- **异步**：任务跑在后台线程池里，请求立刻返回，日志与进度走 SSE。

文件之间**刻意不做并行**，因为续跑坐标是一条**全局序列**：唯一性索引与去重索引各有一个
append-only 摘要日志，`state.json` 只记「日志到第 N 条摘要」这一个坐标；`carry`（ffill 的
跨块携带值）与 file 作用域去重索引也只重建**一个**文件的（`TaskState.partial` 就是那个
「正在跑且提交过内容的文件」）。两个文件同时处于「跑到一半」时，快照记录不了两条独立的
坐标，恢复必然错位 —— 而错位会静默改变产物内容（少去重几行、唯一性判错），这是最坏的一
类 bug。

要开文件级并行，得把「每文件索引坐标」也持久化，并且只允许无跨行状态的算子并行。
在那之前，宁慢不错。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clean_events import KIND_LLM, KIND_REPORT, KIND_STATUS, EventBus, LogGate
from .clean_generate import GenerationBatch, build_generators, generated_fields
from .clean_io import CleanIoError, open_reader, open_writer, precount
from .clean_llm import (
    COMMIT_ROWS_DEFAULT,
    COMMIT_ROWS_LLM,
    CallBudget,
    LlmBudgetExhausted,
    LlmCache,
    LlmClient,
    LlmPool,
    build_llm_factory,
)
from .clean_models import CleanTaskConfig, Constraint, Fallback, FieldSpec
from .clean_ops import Table, apply_ops
from .clean_python import PythonPool
from .clean_report import FileReport, ReportInput, build_report, redact
from .clean_schema import apply_fallback, check_value, count_warnings, eval_column
from .clean_state import (
    FileState,
    LogCoords,
    ResumeDecision,
    StateStore,
    TaskProgress,
    TaskState,
    now_iso,
    resolve_resume,
)
from .clean_stats import cross_check, regression_findings
from .task_manager import TaskCancelled, TaskControl

LOGGER = logging.getLogger(__name__)

# 单批读入上限。它同时决定内存峰值与取消响应：50 万行的一批会让取消等到这一批跑完。
READ_BATCH_MAX = 20_000
# 用户函数一次调用的行数。子进程的超时是按调用算的，一批两万行配 5 秒超时必然误杀。
FUNCTION_CHUNK = 2_000

RATE_WINDOW = 60.0
UNSTABLE_RATIO = 3.0
PROGRESS_INTERVAL = 1.0
METRICS_INTERVAL = 5.0

FALLBACK_LOG_BYTES = 64 * 1024 * 1024
NOTE_LIMIT = 200
SAMPLE_TEXT_LIMIT = 200

# 表头规范化时要丢掉的零宽字符。从网页/Excel 里复制出来的表头经常带这些，
# 肉眼完全看不出差别，但按名匹配会失败。
_ZERO_WIDTH = "​‌‍⁠﻿"
_WS = re.compile(r"\s+")


class CleanEngineError(RuntimeError):
    """任务级失败：配置与数据不可能匹配，或者护栏判死。"""


class ResumeRefused(CleanEngineError):
    """拒绝续跑，附带机器可读的原因（`decision.code`）。"""

    def __init__(self, decision: ResumeDecision):
        super().__init__(decision.reason)
        self.decision = decision


class FileFailed(CleanEngineError):
    """单文件失败。**不终止整个任务** —— 一个坏文件不该废掉跑了半小时的一批。"""


# =========================================================================== 输入清单


def collect_inputs(roots: Sequence[str | Path], options: Any) -> list[tuple[str, str]]:
    """展开来源路径为 `(key, 绝对路径)`，顺序即处理顺序。

    `key` 是相对输入根的路径 —— 它同时是产物名与续跑身份（见 `clean_state.FileState`）。
    只有真的重名（多个根里有同名文件）才给 key 加根名前缀：不加前缀时 key 最短最好读，
    而一旦加了前缀，换个根目录就会让所有 key 变化、续跑被拒 —— 所以只在必要时加。
    """
    from .clean_io import list_input_files

    roots = [Path(root) for root in roots]
    files = list_input_files([str(root) for root in roots], options)
    pairs: list[tuple[Path, Path, Path]] = []
    for path in files:
        base: Path | None = None
        relative = Path(path.name)
        for root in roots:
            candidate = root if root.is_dir() else root.parent
            try:
                relative = path.relative_to(candidate.resolve())
            except ValueError:
                continue
            base = root
            break
        pairs.append((base or Path("."), relative, path))

    names = Counter(str(relative) for _, relative, _ in pairs)
    seen: Counter[str] = Counter()
    out: list[tuple[str, str]] = []
    for base, relative, path in pairs:
        key = str(relative)
        if names[key] > 1:
            prefix = base.name if base.is_dir() else base.stem
            key = f"{prefix or 'root'}/{relative}"
        seen[key] += 1
        if seen[key] > 1:
            key = f"{key}#{seen[key]}"
        out.append((key, str(path)))
    return out


# =========================================================================== 表头映射


def normalize_field_name(name: Any) -> str:
    """匹配用的规范化名字：NFKC + 去零宽 + 折叠空白 + 大小写无关。

    **绝不按位置兜底**：位置匹配在 GB 文件上会把「第三列」当成「姓名」，产出一份
    看起来完全正常的垃圾。匹配不到就是匹配不到，交给 `on_missing` 处理。
    """
    text = unicodedata.normalize("NFKC", str(name if name is not None else ""))
    for char in _ZERO_WIDTH:
        text = text.replace(char, "")
    return _WS.sub(" ", text).strip().casefold()


@dataclass
class _Mapping:
    """一个文件的表头映射结果。按文件算：目录来源下每个文件的表头都可能不同。"""

    index: dict[str, int] = field(default_factory=dict)
    """目的字段 → 源列下标。"""
    column_map: dict[str, str] = field(default_factory=dict)
    """源列 → 目的字段，给报告看（一列可以映到多个目的字段）。"""
    missing: list[str] = field(default_factory=list)
    """没有任何来源的目的字段。"""
    unmapped: tuple[str, ...] = ()
    """没被任何目的字段引用的源列。"""


def resolve_mapping(columns: Sequence[str], fields: Sequence[FieldSpec]) -> _Mapping:
    """精确名 → 规范化名 → 放弃。重复表头一律取**第一次**出现的那一列
    （与 `Table.find` 的语义一致，否则映射与取值会指向不同的列）。"""
    exact: dict[str, int] = {}
    folded: dict[str, int] = {}
    for position, name in enumerate(columns):
        exact.setdefault(str(name), position)
        folded.setdefault(normalize_field_name(name), position)

    mapping = _Mapping()
    for spec in fields:
        if not spec.source:
            continue
        position = exact.get(spec.source)
        if position is None:
            position = folded.get(normalize_field_name(spec.source))
        if position is None:
            mapping.missing.append(spec.dest)
            continue
        mapping.index[spec.dest] = position
        mapping.column_map.setdefault(str(columns[position]), spec.dest)
    used = set(mapping.index.values())
    mapping.unmapped = tuple(
        str(name) for position, name in enumerate(columns) if position not in used
    )
    return mapping


# =========================================================================== 进度与 ETA


class _Meter:
    """进度与 ETA。**百分比单调钳制，ETA 不钳制** —— 百分比倒退是纯 bug，而 ETA 确实会
    变大（端点变慢、开始重试），钳制它就是在撒谎。"""

    def __init__(
        self, total: int, *, exact: bool, now: Callable[[], float] = time.monotonic
    ):
        self.total = max(0, int(total))
        self.exact = bool(exact)
        self.done = 0
        self.peak_pct = 0.0
        self._now = now
        self._started = now()
        self._samples: deque[tuple[float, int]] = deque()

    def note(self, rows: int) -> None:
        self.done += max(0, int(rows))
        moment = self._now()
        if self.done > self.total:
            # 估算偏低时进度条会倒退，所以把分母抬到实际值再重算百分比
            self.total = self.done
        self._samples.append((moment, self.done))
        while len(self._samples) > 1 and moment - self._samples[0][0] > RATE_WINDOW:
            self._samples.popleft()

    def _rates(self) -> tuple[float, float]:
        """返回 (慢速率, 快速率)：最近一个时间窗与全程平均。两个都算，区间就是它们。"""
        now = self._now()
        elapsed = max(1e-6, now - self._started)
        overall = self.done / elapsed
        window = 0.0
        if len(self._samples) >= 2:
            first_at, first_done = self._samples[0]
            span = now - first_at
            if span > 1e-6:
                window = (self.done - first_done) / span
        rates = [rate for rate in (window, overall) if rate > 0]
        if not rates:
            return 0.0, 0.0
        return min(rates), max(rates)

    def snapshot(self, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        now = self._now()
        elapsed = max(0.0, now - self._started)
        slow, fast = self._rates()
        pct = (self.done / self.total * 100.0) if self.total else 0.0
        pct = min(100.0, max(pct, self.peak_pct))
        self.peak_pct = pct
        remaining = max(0, self.total - self.done)
        payload: dict[str, Any] = {
            "done": self.done,
            "total": self.total or None,
            "total_known": bool(self.total),
            "exact": self.exact,
            "pct": round(pct, 2),
            "elapsed": round(elapsed, 1),
            "rate": round(fast, 2),
            "eta_low": round(remaining / fast, 1) if (self.total and fast > 0) else None,
            "eta_high": round(remaining / slow, 1) if (self.total and slow > 0) else None,
            "unstable": bool(slow > 0 and fast / slow > UNSTABLE_RATIO),
        }
        if extra:
            payload.update(extra)
        return payload


# =========================================================================== 结果


@dataclass
class EngineResult:
    task_id: str
    status: str
    report: str
    files: list[FileReport] = field(default_factory=list)
    state: TaskState | None = None
    error: str = ""
    duration: float = 0.0
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def as_dict(self) -> dict[str, Any]:
        state = self.state
        return {
            "task_id": self.task_id,
            "status": self.status,
            "error": self.error,
            "duration": round(self.duration, 2),
            "cancelled": self.cancelled,
            "files": len(self.files),
            "rows_in": sum(item.rows_in for item in self.files),
            "rows_out": sum(item.rows_out for item in self.files),
            "resumed": bool(state and state.resume_count),
        }


# =========================================================================== 单文件运行时


@dataclass
class _FileRun:
    """一个文件的运行时状态（只在引擎内部流转）。"""

    key: str
    path: Path
    fs: FileState
    reader: Any = None
    writer: Any = None
    mapping: _Mapping | None = None
    read_rows: int = 0
    # 这两个**只在这里累加**，提交时才写回 `fs`。写在 fs 上会让「写了但没提交」的那一段
    # 也计进去：取消时磁盘上的尾巴会被截掉重跑，于是同一个批次被数了两遍，报告里的
    # 「输出行数」比产物实际的行数多出一段 —— 而产物本身是对的，所以这种错很难被发现。
    rows_out: int = 0
    dropped: int = 0
    batch_no: int = 0
    skip_rows: bool = False
    skip_message: str = ""
    started: float = 0.0


# =========================================================================== 引擎


class CleanEngine:
    """一次任务的全部处理逻辑。**一个实例只跑一次**（`run` 之后状态就定格了）。"""

    def __init__(
        self,
        task_id: str,
        config: CleanTaskConfig,
        store: StateStore,
        *,
        bus: EventBus | None = None,
        control: TaskControl | None = None,
        llm_pool: LlmPool | None = None,
        llm_client: LlmClient | None = None,
        seed: int = 0,
        secrets: Iterable[str] = (),
        python_executable: str | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        row_limit: int = 0,
    ):
        self.task_id = str(task_id)
        self.config = config
        self.store = store
        self.bus = bus
        self.control = control
        self.llm_pool = llm_pool
        # numpy 的 `default_rng` 只收非负整数种子，而 `seed: int` 的合法域是整个 int。
        # 取模到 32 位：负数于是只是「另一个合法种子」，而不是启动期的 `ValueError` ——
        # 后者会从 `_build_runtime` 抛出来，把整个任务变成一个没有报告的失败。
        self.seed = int(seed) % (2**32)
        self.secrets = tuple(item for item in secrets if item)
        self.python_executable = python_executable
        self.on_progress = on_progress
        self._clock = clock
        # 试跑用的每文件行数上限（0 = 不限）。**只给「配置干跑」用**：正式任务不许截断
        # 数据 —— 那会产出一份「看起来正常」的半截产物。干跑走的是临时状态目录，跑完就删。
        self._row_limit = max(0, int(row_limit or 0))

        self._dest: list[str] = list(config.dest_fields())
        self._ops = list(config.ops)
        self._functions = list(config.functions)
        self._pre_functions = [fn for fn in self._functions if fn.phase == "pre"]
        self._post_functions = [fn for fn in self._functions if fn.phase == "post"]
        self._constrained = [spec for spec in config.fields if spec.constraints]
        self._generators: list[Any] = []
        self._generated: list[str] = []

        self._commit_rows = self._resolve_commit_rows()
        self._read_batch = max(1, min(self._commit_rows, READ_BATCH_MAX))
        self._verify_every = int(config.output.verify_every or 0)

        # 护栏与客户端：**预算由引擎建**，因为它来自本任务的配置；端点池是全局的，由调用方
        # 建。客户端若由外部注入，必须自带 budget —— 否则护栏会静默消失（测试与将来的
        # 「自定义客户端」路径都靠这条断言兜住）。
        self._budget = CallBudget(
            max_calls=int(config.llm.max_calls or 0),
            abort_error_rate=float(config.llm.abort_error_rate),
        )
        self._cache: LlmCache | None = None
        if llm_client is not None:
            if getattr(llm_client, "budget", None) is None:
                raise CleanEngineError("注入的 LlmClient 必须带 budget，否则调用上限失效")
            self._budget = llm_client.budget
            self._cache = getattr(llm_client, "cache", None)
            self.llm_client = llm_client
        elif llm_pool is not None:
            self._cache = LlmCache(store.llm_cache_path)
            self.llm_client = LlmClient(llm_pool, cache=self._cache, budget=self._budget)
        else:
            self.llm_client = None

        self.progress: TaskProgress | None = None
        self.state: TaskState | None = None
        self._python: PythonPool | None = None
        self._gate = LogGate()
        self._notes: list[str] = []
        self._meter: _Meter | None = None
        self._finished_files = 0
        self._writer_counters: dict[str, int] = {}
        self._llm_per_field: dict[str, dict[str, int]] = {}
        self._post_touched: set[str] = set()
        self._llm_exhausted = False
        self._fallback_bytes = 0
        self._fallback_dropped = 0
        self._last_progress_at = 0.0
        self._last_metrics_at = 0.0
        self._lock = threading.Lock()

    # -- 配置派生 ----------------------------------------------------------

    def _resolve_commit_rows(self) -> int:
        configured = int(self.config.output.commit_rows or 0)
        if configured:
            return max(1, configured)
        return COMMIT_ROWS_LLM if self.config.llm_fields() else COMMIT_ROWS_DEFAULT


    def commit_rows(self) -> int:
        return self._commit_rows

    def read_batch(self) -> int:
        return self._read_batch

    def report_path(self) -> Path:
        return report_path(self.store)

    # -- 日志 --------------------------------------------------------------

    def _log(self, level: str, message: str, **data: Any) -> None:
        text = redact(str(message), self.secrets)
        if self.bus is not None:
            self.bus.log(self.task_id, text, level=level, data=data or None)
        else:
            LOGGER.log(
                {"debug": 10, "info": 20, "warn": 30, "error": 40}.get(level, 20), text
            )

    def _note(self, text: str) -> None:
        """进报告的一次性说明（有界）。"""
        text = str(text)
        with self._lock:
            if text not in self._notes and len(self._notes) < NOTE_LIMIT:
                self._notes.append(text)

    def _status(self, status: str, message: str = "") -> None:
        if self.bus is not None:
            self.bus.emit(
                self.task_id, KIND_STATUS, message or status, data={"status": status}
            )

    # -- 生命周期 ----------------------------------------------------------

    def run(
        self, inputs: Sequence[tuple[str, str]], *, resume: bool = False
    ) -> EngineResult:
        """跑完（或被取消/失败）。**不抛异常** —— 失败也返回带 `status`/`error` 的结果，
        因为报告与状态在任何一种收尾里都要落盘。唯一例外是 `ResumeRefused`：它发生在任何
        副作用之前，调用方要据此让用户选「重跑」还是「只下载已有产物」。"""
        started = self._clock()
        # 索引摘要日志是 append-only 裸文件，打开它不会自己建目录 —— 目录必须先存在，
        # 否则唯一性/去重索引会在第一批就炸（而且报出来的是「文件处理失败」，
        # 看起来像数据问题，其实是环境问题）。
        try:
            self.store.ensure()
        except OSError as exc:
            raise CleanEngineError(f"无法创建任务目录：{exc}") from exc
        try:
            state, notes = self._prepare_state(inputs, resume=resume)
        except ResumeRefused:
            raise
        except Exception as exc:  # 状态文件损坏等
            raise CleanEngineError(f"无法准备任务状态：{exc}") from exc

        self.state = state
        for text in notes:
            self._note(text)
        try:
            self._build_runtime(state)
        except ResumeRefused:
            raise
        except Exception as exc:
            # 启动期的错误（模型池没配好、生成规则校验不过……）也走「失败但要落盘」这条路：
            # `run` 的契约是不抛异常，抛出去的话调用方连报告与状态都拿不到。
            message = str(exc) if isinstance(exc, CleanEngineError) else f"{type(exc).__name__}: {exc}"
            self._log("error", f"任务启动失败：{message}")
            LOGGER.warning("清洗任务 %s 启动失败", self.task_id, exc_info=True)
            return self._finish(
                state, status="failed", error=message, cancelled=False, started=started
            )
        total, exact = self._estimate_total(inputs)
        self._meter = _Meter(total, exact=exact, now=self._clock)
        # 续跑时把已提交的行数补进进度：用户打开页面就该看到「已经跑到 30%」
        self._meter.note(sum(item.rows_in for item in state.files.values()))
        self._finished_files = len(state.done_keys())

        status = "completed"
        error = ""
        cancelled = False
        if self._row_limit:
            self._note(
                f"试跑：每个文件最多处理前 {self._row_limit} 行，其余行未读取"
                "（产物不完整，仅用于提交前校验配置）"
            )
        self._status("running", "开始处理" if not resume else "继续处理")
        self._log(
            "info",
            f"任务开始：{len(inputs)} 个文件，输出 {len(self._dest)} 列，"
            f"提交间隔 {self._commit_rows} 行（每批 {self._read_batch} 行）",
            files=len(inputs),
            commit_rows=self._commit_rows,
        )
        try:
            self._process_all(inputs)
        except TaskCancelled:
            cancelled = True
            status = "cancelled"
            error = "任务被取消，已提交的产物保留，可以从提交点续跑"
            self._log("warn", error)
        except CleanEngineError as exc:
            status = "failed"
            error = str(exc)
            self._log("error", error)
        except Exception as exc:  # 未预期的异常也要落盘报告，否则现场什么都没有
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            self._log("error", f"任务异常中止：{error}")
            LOGGER.exception("清洗任务 %s 异常中止", self.task_id)
        finally:
            status, error = self._take_files_into_account(state, status, error)

        return self._finish(
            state, status=status, error=error, cancelled=cancelled, started=started
        )

    def _take_files_into_account(
        self, state: TaskState, status: str, error: str
    ) -> tuple[str, str]:
        """有文件失败时任务整体记为失败 —— 但不能掩盖取消。"""
        failed = [item for item in state.ordered() if item.status == "failed"]
        if status == "completed" and failed:
            names = "、".join(item.key for item in failed[:5])
            more = "" if len(failed) <= 5 else f" 等 {len(failed)} 个"
            return "failed", f"{len(failed)} 个文件处理失败：{names}{more}"
        return status, error

    def progress_payload(self) -> dict[str, Any]:
        if self._meter is None:
            return {}
        state = self.state
        return self._meter.snapshot(
            extra={
                "files_done": self._finished_files,
                "files_total": len(state.files) if state else 0,
            }
        )

    # -- 准备 --------------------------------------------------------------

    def _prepare_state(
        self, inputs: Sequence[tuple[str, str]], *, resume: bool
    ) -> tuple[TaskState, list[str]]:
        store = self.store
        existing = store.load()
        notes: list[str] = []
        if resume:
            decision = resolve_resume(existing, self.config, inputs, store=store)
            if not decision.resumable or decision.state is None:
                raise ResumeRefused(decision)
            state = decision.state
            state.resume_count += 1
            state.status = "running"
            notes.extend(decision.notes)
            # 截断必须发生在 TaskProgress.restore（重建索引）之前
            for action in store.truncate_index_logs(state):
                notes.append(f"索引日志已截断：{action}")
            self._log("info", f"续跑第 {state.resume_count} 次：从提交点继续")
            return state, notes

        # 新任务：清掉上一次的状态与产物（沿用同一个任务目录重跑时）
        store.delete()
        _clear_outputs(store.output_dir)
        state = TaskState(
            task_id=self.task_id,
            config_hash=self.config.config_hash(),
            status="running",
            created_at=now_iso(),
            started_at=now_iso(),
            updated_at=now_iso(),
        )
        # **一开始就把全部输入登记进状态**，而不是等处理到它才登记。否则崩在第一个文件里、
        # 后面的文件还没登记过，续跑时 `resolve_resume` 会把它们当成「新增的文件」而拒绝
        # 续跑 —— 那恰恰是这个功能最该覆盖的场景（崩在开头/中间）。顺带的好处是进度里的
        # 文件总数从第一秒就是对的，而不是随着处理过程一个个涨上去。
        for key, path in inputs:
            state.file(key, str(path), *_fingerprint(path))
        return state, notes

    def _build_runtime(self, state: TaskState) -> None:
        self.progress = TaskProgress(self.config, self.store, seed=self.seed)
        if state.progress:
            partial = state.partial
            self.progress.restore(
                state.progress, current_file=partial.key if partial else ""
            )
            # carry 不在 TaskProgress 的快照里（它是每文件的有界状态），所以在 FileState 上
            if partial is not None:
                self.progress.ops.carry.update(partial.carry or {})
        self._llm_per_field = {
            str(name): {k: int(v) for k, v in (entry or {}).items()}
            for name, entry in (state.llm.get("per_field") or {}).items()
        }
        self._llm_exhausted = bool(state.llm.get("exhausted"))
        self._fallback_bytes = int(state.logs.fallback_bytes or 0)
        for text in state.notes or []:
            self._note(text)
        if self._cache is not None:
            try:
                loaded = self._cache.restore()
                count = loaded if isinstance(loaded, int) else 0
                if count:
                    self._log("debug", f"大模型缓存重放 {count} 条")
            except Exception as exc:
                self._log("warn", f"大模型缓存重放失败（将以空缓存继续）：{exc}")

        if self._functions:
            self._python = PythonPool(
                self._functions,
                self.config.python,
                seed=self.seed,
                on_event=self._python_event,
                python_executable=self.python_executable,
            )

        factory = self._llm_factory if self.llm_client is not None else None
        self._generators = build_generators(
            self.config, seed=self.seed, llm_factory=factory
        )
        self._generated = generated_fields(self._generators)
        for generator in self._generators:
            try:
                generator.validate(self._dest)
            except Exception as exc:
                raise CleanEngineError(f"生成规则校验失败：{exc}") from exc
        self._check_llm_ready()
        self._warn_about_warn_unique()

    def _check_llm_ready(self) -> None:
        """有大模型字段却没有可用端点时**立刻失败**。

        不这么做的话，每行都会拿到一个空值、再被约束判为违规、再走一遍回退 —— 任务「跑完」
        了，产物全是空，报告里是几百万条回退记录。启动时一句话讲清楚，比事后看报告好得多。
        """
        llm_rules = [
            generator
            for generator in self._generators
            if getattr(getattr(generator, "rule", None), "kind", "") == "llm"
        ]
        if not llm_rules:
            return
        if self.llm_client is None:
            raise CleanEngineError("配置里有大模型生成字段，但没有提供模型端点")
        if self.llm_pool is None:
            return
        if not self.llm_pool.usable:
            raise CleanEngineError(
                "配置里有大模型生成字段，但模型池里没有启用的端点（请先在「模型池」里添加）"
            )
        # 这里**不**提示「端点都在熔断冷却中」：池是每任务新建的（见 `CleanManager._build_pool`），
        # 构造时 `circuit_until` 全是 0、令牌桶是满的，这句话在这条路径上永远不成立 —— 写成
        # `if` 只会让人以为它在兜底。真要在冷却期间告诉用户「不必重启」，得挂到跑起来之后。

    def _llm_factory(self, spec: FieldSpec, rule: Any, seed: int) -> Any:
        factory = build_llm_factory(
            self.llm_client, self.config.llm, on_note=self._gen_note(spec.dest)
        )
        return factory(spec, rule, seed)

    def _gen_note(self, dest: str) -> Callable[[str, str], None]:
        """生成器的告警回调。**键必须与行号无关** —— `LlmGenerator` 的失败消息里带行号，
        按消息做键会得到几百万条一次性日志，那正是 `LogGate` 要防的事。"""

        def note(level: str, message: str) -> None:
            text = str(message)
            if self.progress is not None:
                self.progress.ops.note_once(f"llm:{dest}:{level}", text[:300])
            self._log(level if level in ("warn", "error") else "info", text)

        return note

    def _warn_about_warn_unique(self) -> None:
        """warn 级 unique 是个静默失效的配置，必须说出来：唯一性检查需要索引，
        而 warn 级只计数不检查（见 `clean_schema._fail_indices`）。"""
        for spec in self.config.fields:
            for constraint in spec.constraints:
                if constraint.kind == "unique" and constraint.severity != "error":
                    self._note(
                        f"`{spec.dest}` 的 unique 约束是 warn 级：唯一性检查需要索引，"
                        "而 warn 级只计数不检查，所以它不会生效。要检查请改成 error 级。"
                    )

    def _estimate_total(self, inputs: Sequence[tuple[str, str]]) -> tuple[int, bool]:
        """总行数估算。`precount` 打开时精确（代价是多读一遍），否则按头部样本外推。"""
        if self.config.input.precount:
            total = 0
            for _, path in inputs:
                try:
                    total += precount(path, self.config.input)
                except Exception as exc:
                    self._log("warn", f"{Path(path).name} 精确行数统计失败：{exc}")
            return total, True
        total = 0
        for _, path in inputs:
            try:
                reader = open_reader(path, self.config.input, 1)
                try:
                    estimate = int(reader.row_estimate or 0)
                finally:
                    reader.close()
            except Exception:
                estimate = 0
            total += estimate
        return total, False

    # -- 文件循环 ----------------------------------------------------------

    def _process_all(self, inputs: Sequence[tuple[str, str]]) -> None:
        state = self._expect_state()
        for key, path in inputs:
            self._checkpoint("文件之间")
            fs = state.file(key, path, *_fingerprint(path))
            if fs.done:
                self._log("info", f"跳过已完成文件：{key}")
                continue
            self._run_file(key, Path(path), fs)

    def _run_file(self, key: str, path: Path, fs: FileState) -> None:
        state = self._expect_state()
        progress = self._expect_progress()
        run = _FileRun(key=key, path=path, fs=fs, started=self._clock())
        resume_from = self._resume_point(fs, state)
        reader = None
        self._log(
            "info",
            f"开始处理 {key}" + (f"（从第 {resume_from} 行续）" if resume_from else ""),
        )
        self._report_progress(force=True, message=f"处理 {key}")
        try:
            reader = open_reader(path, self.config.input, self._read_batch)
            run.reader = reader
            run.mapping = resolve_mapping(reader.columns, self.config.fields)
            self._check_mapping(run)
            self._record_mapping(state, run)
            for text in list(getattr(reader, "notes", None) or []):
                progress.ops.note_once(f"reader:{key}:{text}", str(text))

            if run.skip_rows:
                fs.status = "done"
                fs.message = run.skip_message
                fs.seconds = round(self._clock() - run.started, 3)
                self._log("warn", f"{key}：{run.skip_message}")
                self._commit(run)
                self._finish_file(run)
                return

            # 续跑：跳过已提交的行。**按行号跳过，不按字节偏移** —— 行号续跑与格式无关，
            # xlsx / JSONL 也能用，代价只是重读一个文件（秒级）。
            if resume_from:
                reader.skip(resume_from)
                progress.ops.reset_file(key)
                progress.ops.carry.update(fs.carry or {})
            else:
                progress.ops.reset_file(key)
            run.read_rows = resume_from
            # 行数也从**提交点**接上：`_resume_point` 已经把不能续的文件归零了，能续的那个
            # 保留的是提交时的计数，磁盘上那段没提交的尾巴没算进来 —— 这正是要的。
            run.rows_out = fs.rows_out
            run.dropped = fs.dropped

            run.writer = self._open_writer(key, fs, resume=bool(resume_from))
            fs.status = "running"
            self._consume(run, reader, run.writer)

            run.writer.flush()
            final = run.writer.close()
            run.writer = None
            fs.status = "done"
            if _is_append_format(self.config):
                fs.out_bytes = _size_of(final)
                if self.config.output.durable_commit:
                    _fsync_file(final)
            fs.seconds = round(self._clock() - run.started, 3)
            fs.carry = {}
            self._commit(run)
            self._finish_file(run)
            self._log(
                "info",
                f"完成 {key}：读入 {fs.rows_in} 行，输出 {fs.rows_out} 行，"
                f"丢弃 {fs.dropped} 行，用时 {fs.seconds:.2f}s",
                rows_in=fs.rows_in,
                rows_out=fs.rows_out,
                dropped=fs.dropped,
            )
        except TaskCancelled:
            self._suspend(run)
            raise
        except FileFailed as exc:
            self._fail_file(run, str(exc))
        except CleanIoError as exc:
            self._fail_file(run, f"读写失败：{exc}")
        except Exception as exc:
            self._fail_file(run, f"{type(exc).__name__}: {exc}")
            LOGGER.exception("文件 %s 处理失败", key)
        finally:
            if run.writer is not None:
                try:
                    run.writer.suspend()
                except Exception:
                    LOGGER.debug("挂起 writer 失败", exc_info=True)
                run.writer = None
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    LOGGER.debug("关闭 reader 失败", exc_info=True)

    def _resume_point(self, fs: FileState, state: TaskState) -> int:
        """这个文件能从哪一行接上。**同一个时刻只允许一个文件有半截进度**。

        理由见模块 docstring 第 4 条：carry 与 file 作用域去重索引只重建一个文件。若状态里
        有第二个半截文件（历史遗留或人为改过），它必须从头跑 —— 而从头跑必须把产物也砍掉，
        否则新行会接在旧行后面，产出一份「看起来正常但中间重复了一段」的文件。
        """
        partial = state.partial
        if partial is not None and partial.key == fs.key and fs.rows_in:
            return int(fs.rows_in)
        if fs.rows_in or fs.rows_out:
            self._note(
                f"{fs.key} 上次跑到一半但无法续（同一次只能续一个文件），已从头重跑"
            )
            fs.rows_in = 0
            fs.rows_out = 0
            fs.out_bytes = 0
            fs.dropped = 0
            fs.carry = {}
        return 0

    def _check_mapping(self, run: _FileRun) -> None:
        missing = run.mapping.missing if run.mapping is not None else []
        if not missing:
            return
        layout = self.config.output.layout
        mode = self.config.output.on_missing
        text = f"缺少映射列：{'、'.join(missing)}"
        if layout == "strict" or mode == "error":
            raise FileFailed(f"{text}（layout={layout}、on_missing={mode} 都要求列齐全）")
        if mode == "skip_row":
            run.skip_rows = True
            run.skip_message = f"{text}，按 on_missing=skip_row 跳过该文件"
            return
        run.fs.message = f"{text}（按 on_missing=empty 填空）"
        self._note(
            f"目录里有文件的表头缺列（例如 {run.key} 缺 {'、'.join(missing)}），"
            "这些文件里缺的列按 on_missing=empty 填默认值"
        )
        self._log("warn", f"{run.key}：{text}，已填空")

    def _record_mapping(self, state: TaskState, run: _FileRun) -> None:
        mapping = run.mapping
        if mapping is None:
            return
        state.extra.setdefault("mapping", {})[run.key] = {
            "column_map": dict(mapping.column_map),
            "unmapped": list(mapping.unmapped),
        }

    def _open_writer(self, key: str, fs: FileState, *, resume: bool) -> Any:
        target = self.store.output_path(key, self.config.output.format)
        if not resume:
            _clear_file_outputs(target)
        return open_writer(
            target,
            self._dest,
            self.config.output,
            append_bytes=fs.out_bytes if resume else None,
            counters=self._writer_counters,
            sanitize_formula=self.config.input.sanitize_formula,
        )

    def _fail_file(self, run: _FileRun, message: str) -> None:
        run.fs.status = "failed"
        run.fs.message = message
        run.fs.seconds = round(self._clock() - run.started, 3)
        self._log("error", f"{run.key} 处理失败：{message}")
        self._note(f"文件 {run.key} 处理失败：{message}")
        try:
            self._commit(run)
        except Exception:
            LOGGER.warning("失败文件的提交点写入失败", exc_info=True)

    def _suspend(self, run: _FileRun) -> None:
        """取消/异常退出：**保住已提交的内容**，把 writer 关掉但不做格式转换。

        用 `abort()` 会把已提交的 spool 一起删掉（续跑就没了）；用 `close()` 会把只写了
        一半的表转成 xlsx（一个「看起来完整」的半成品）。两条路都不行。

        **刻意不动 `fs.out_bytes`**：它是上一个提交点的字节数，磁盘上可能比它多出一段
        未提交的行（`suspend()` 关句柄时会把缓冲区刷出去）。续跑时 `append_bytes` 会先把
        文件截断回这个坐标再接着写 —— writer 的 `suspend()` 契约就是「`out_bytes` 仍然
        有效」。在这里改成「已 flush 的字节数」会让截断坐标跑到提交点之后，续跑的产物就会
        多出这一段（重复行），而报告里的行数还是对的 —— 最难发现的那种错。
        """
        writer = run.writer
        if writer is None:
            return
        try:
            writer.suspend()
        except Exception:
            LOGGER.warning("挂起 %s 的产出失败", run.key, exc_info=True)

    def _finish_file(self, run: _FileRun) -> None:
        self._finished_files += 1
        self._report_progress(force=True, message=f"{run.key} 完成")

    # -- 读批循环 ----------------------------------------------------------

    def _consume(self, run: _FileRun, reader: Any, writer: Any) -> None:
        since_commit = 0
        while True:
            self._checkpoint(f"{run.key} 分块之间")
            rows = reader.read_batch()
            if not rows:
                break
            if self._row_limit:
                # 试跑：**按行截断，不是读满一批就停** —— 一批最多 20000 行，直接停会让
                # 「跑前 500 行」实际跑 20000 行，也正是干跑最不该花的那些大模型调用。
                left = self._row_limit - run.read_rows
                if left <= 0:
                    break
                rows = rows[:left]
            run.batch_no += 1
            out_rows = self._process_batch(run, rows)
            if out_rows:
                writer.write(out_rows)
                run.rows_out += len(out_rows)
            run.dropped += max(0, len(rows) - len(out_rows))
            run.read_rows += len(rows)
            since_commit += len(rows)
            self._collect_reader_counters(run)
            self._meter_note(len(rows))
            self._report_progress(message=f"处理 {run.key}")
            if since_commit >= self._commit_rows:
                self._commit(run)
                since_commit = 0
        self._collect_reader_counters(run)
        # 收尾时也要提交一次：一批都没读到的空文件同样要落进统计与状态
        self._commit(run)

    def _collect_reader_counters(self, run: _FileRun) -> None:
        """把 reader 的坏行/乱码计数搬到 FileState 上。读不到就保持原值（各格式暴露的
        计数不同，缺一个不该让整个文件失败）。"""
        reader = run.reader
        if reader is None:
            return
        for name in ("parse_errors", "ragged_rows", "replacement_chars"):
            try:
                value = getattr(reader, name)
            except Exception:
                continue
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            try:
                setattr(run.fs, name, int(value or 0))
            except (TypeError, ValueError):
                continue

    def _meter_note(self, rows: int) -> None:
        if self._meter is not None:
            self._meter.note(rows)

    # -- 一批的流水线 ------------------------------------------------------

    def _process_batch(
        self, run: _FileRun, rows: Sequence[Sequence[Any]]
    ) -> list[list[Any]]:
        progress = self._expect_progress()
        base = run.read_rows
        table = self._map_batch(run, rows)
        count = table.nrows()
        # 清洗前统计：**映射后的值**，按目的字段对齐，且刻意复制一份 —— 后面的阶段会就地
        # 改写列里的值（用户函数写回、生成覆盖），不复制的话「清洗前」会跟着一起变。
        before = [(name, list(table.column(name))) for name in self._dest]

        table = self._run_functions("pre", table, run)
        if self._ops:
            table = apply_ops(table, self._ops, progress.ops)
        table = self._generate(table, run, base)
        table = self._enforce(table, run, base)
        table = self._run_functions("post", table, run)

        progress.before.update([name for name, _ in before], [col for _, col in before])
        after = [list(table.column(name)) for name in self._dest]
        progress.after.update(self._dest, after)
        self._regress(after, run)
        return _transpose(after, count)

    def _map_batch(self, run: _FileRun, rows: Sequence[Sequence[Any]]) -> Table:
        """建批表：源列 + 目的字段。**映射出来的目的字段是源列的副本** —— 直接把源列的
        列表对象交出去，后续阶段一改就改掉了源数据，「清洗前 vs 清洗后」的对比会变成两份
        一样的值。"""
        table = Table.from_rows(list(run.reader.columns), rows)
        count = table.nrows()
        index = run.mapping.index if run.mapping is not None else {}
        for spec in self.config.fields:
            position = index.get(spec.dest)
            if position is None or position >= len(table.data):
                table = table.with_column(spec.dest, [_blank(spec)] * count)
            else:
                table = table.with_column(spec.dest, list(table.data[position]))
        return table

    # -- 用户函数 ----------------------------------------------------------

    def _run_functions(self, phase: str, table: Table, run: _FileRun) -> Table:
        plan = self._pre_functions if phase == "pre" else self._post_functions
        if not plan or self._python is None:
            return table
        count = table.nrows()
        if not count:
            return table
        for fn in plan:
            for start in range(0, count, FUNCTION_CHUNK):
                self._checkpoint(f"{run.key} 用户函数 {fn.name}")
                self._call_function(fn, table, start, min(count, start + FUNCTION_CHUNK), run)
            if phase == "post":
                self._post_touched.update(fn.output_fields)
        return table

    def _call_function(
        self, fn: Any, table: Table, start: int, end: int, run: _FileRun
    ) -> None:
        progress = self._expect_progress()
        assert self._python is not None
        worker = self._python.worker()
        inputs = list(fn.input_fields)
        missing = [name for name in inputs if table.find(name) is None]
        if missing:
            progress.ops.note_once(
                f"fn_missing:{fn.name}",
                f"函数 {fn.name} 的输入字段在数据里不存在：{'、'.join(missing)}"
                "（这些输入按空值传入）",
            )
        width = end - start
        if fn.mode == "column":
            columns = {
                name: (
                    list(table.column(name)[start:end])
                    if table.find(name) is not None
                    else [""] * width
                )
                for name in inputs
            }
            result = worker.call_columns(fn.name, columns, timeout=fn.timeout_s)
        else:
            rows = [
                [
                    (
                        table.column(name)[position]
                        if table.find(name) is not None
                        else ""
                    )
                    for name in inputs
                ]
                for position in range(start, end)
            ]
            result = worker.call_rows(fn.name, inputs, rows, timeout=fn.timeout_s)

        if not result.ok:
            progress.ops.count(f"fn:{fn.name}:{result.failure}", 1)
            progress.ops.note_once(
                f"fn_fail:{fn.name}:{result.failure}",
                f"函数 {fn.name} 调用失败（{result.failure}）：{result.detail or ''}"
                "——这些批次的值原样通过",
            )
            self._log("error", f"函数 {fn.name} 调用失败：{result.failure} {result.detail}")
            if self._python.disabled:
                progress.ops.note_once(
                    "fn_disabled",
                    f"用户函数已整体禁用：{self._python.reason}（后续按原值通过）",
                )
            return

        outputs = list(fn.output_fields)
        if fn.mode == "column":
            for name in outputs:
                values = (result.columns or {}).get(name)
                if values is None:
                    progress.ops.note_once(
                        f"fn_shape:{fn.name}",
                        f"函数 {fn.name} 没有返回输出字段 {name}，该列保持原值",
                    )
                    continue
                target = table.column(name)
                for offset, value in enumerate(list(values)[:width]):
                    target[start + offset] = value
            return

        items = result.rows or []
        for offset in range(width):
            item = items[offset] if offset < len(items) else None
            if item is None:
                progress.ops.count(f"fn:{fn.name}:row_error", 1)
                if fn.on_row_error == "empty":
                    for name in outputs:
                        table.column(name)[start + offset] = ""
                continue
            for name in outputs:
                if name in item:
                    table.column(name)[start + offset] = item[name]
        errors = result.error_pairs()
        if errors:
            first = errors[0]
            progress.ops.note_once(
                f"fn_row_error:{fn.name}",
                f"函数 {fn.name} 有 {len(errors)} 行执行出错（首条：第 {first[0]} 行 "
                f"{first[1]}），这些行按 on_row_error={fn.on_row_error} 处理",
            )

    # -- 生成 --------------------------------------------------------------

    def _generate(self, table: Table, run: _FileRun, base: int) -> Table:
        if not self._generators:
            return table
        if not table.nrows():
            return table
        for generator in self._generators:
            reason = self._llm_stop_reason(generator)
            if reason == "aborted":
                raise CleanEngineError(
                    "大模型错误率超过阈值，任务已中止（一个「成功」但全是回退值的任务"
                    "比诚实失败更糟）"
                )
            if reason == "exhausted":
                self._note(
                    f"大模型调用次数已达上限 {self.config.llm.max_calls}，"
                    "之后的生成全部跳过（按回退策略处理）"
                )
                continue
            table = self._generate_field(generator, table, run, base)
        return table

    def _llm_stop_reason(self, generator: Any) -> str:
        """护栏状态要在**每次调用之前**看，不能靠异常：`LlmGenerator` 内部把 `LlmError`
        全部吞掉并按「本行生成失败」处理，而 `LlmBudgetExhausted` / `LlmAbortError` 正是
        `LlmError` 的子类 —— 只看异常的话，护栏会退化成「每行都失败」。"""
        rule = getattr(generator, "rule", None)
        if rule is None or getattr(rule, "kind", "") != "llm":
            return ""
        if self._budget.aborted:
            return "aborted"
        if self._budget.exhausted:
            self._llm_exhausted = True
            return "exhausted"
        return ""

    def _generate_field(
        self, generator: Any, table: Table, run: _FileRun, base: int
    ) -> Table:
        progress = self._expect_progress()
        count = table.nrows()
        fields = [name for name in generator.output_fields if name in table.columns]
        if not fields:
            return table
        limit = self._sample_limit(generator)
        positions = [
            position for position in range(count) if not limit or base + position < limit
        ]
        if not positions:
            if limit:
                progress.ops.note_once(
                    f"sample:{fields[0]}",
                    f"字段「{'、'.join(fields)}」只对前 {limit} 行生成（sample_rows），"
                    "其余行保留原值",
                )
            return table

        # 分块**只影响可取消的粒度**：大模型批量合并由生成器内部的 batch_size 决定，
        # 这里再按 batch_size 切一次会让批量失效（一次请求变 N 次，钱也变 N 倍）。
        chunk = self._commit_rows
        updates = {name: list(table.column(name)) for name in fields}
        for start in range(0, len(positions), chunk):
            self._checkpoint(f"{run.key} 字段 {fields[0]} 生成")
            reason = self._llm_stop_reason(generator)
            if reason == "aborted":
                raise CleanEngineError("大模型错误率超过阈值，任务已中止")
            if reason == "exhausted":
                break
            window = positions[start : start + chunk]
            batch = GenerationBatch(
                indices=[base + position for position in window],
                rows=[_row_dict(table, position) for position in window],
            )
            produced = self._call_generator(generator, batch, fields)
            if produced is None:
                continue
            for position, item in zip(window, produced):
                if not item:
                    continue
                for name, value in item.items():
                    if name in updates:
                        updates[name][position] = value
        for name, values in updates.items():
            table = table.with_column(name, values)
        return table

    def _call_generator(
        self, generator: Any, batch: GenerationBatch, fields: Sequence[str]
    ) -> list[dict[str, Any]] | None:
        """调用生成器并记账（调用数/失败数/缓存命中按字段分组）。

        调用数用**前后差值**，而不是自己去数 —— 批量生成一次请求出 N 行、失败重试、缓存
        命中、多端点故障转移都在生成器与客户端内部决定，引擎从外面数只会数错。
        """
        progress = self._expect_progress()
        stats = self._llm_per_field.setdefault(fields[0], {})
        for name in fields[1:]:
            self._llm_per_field.setdefault(name, stats)
        calls_before = self._budget.calls
        failed_before = self._budget.failed
        ok_before = self._budget.ok
        hits_before = self._cache.hits if self._cache is not None else 0
        try:
            produced = generator.generate(batch)
        except LlmBudgetExhausted as exc:
            # 理论上到不了这里（生成器会吞掉 LlmError 子类），留一条兜底路径
            self._llm_exhausted = True
            self._log("error", f"字段「{fields[0]}」的生成被调用上限截断：{exc}")
            return None
        except Exception as exc:
            # 其余异常（解析崩溃、网络库的意外异常）绝不能把整个任务带走：生成失败是
            # 「这个字段这一批没值」，不是「任务失败」。
            progress.ops.note_once(
                f"gen_fail:{fields[0]}",
                f"字段「{'、'.join(fields)}」生成失败：{type(exc).__name__}: {exc}"
                "（保留原值）",
            )
            self._log("error", f"字段「{fields[0]}」生成失败：{exc}")
            if self._budget.exhausted:          # 预算可能正是在这次调用里花完的
                self._llm_exhausted = True
            return None
        # 护栏状态也要在**调用之后**补看一眼：`_llm_stop_reason` 只在每个字段 / 每个分块之前
        # 调用，所以「最后一个分块里把预算花完」时没人再去看一眼 —— state.json 里
        # `llm.exhausted` 会是 False，报告里那句「调用次数已达上限」整条消失，用户看到的
        # 是一份「正常完成」而后面几万行全是空值。并发之后一轮就能花完预算，这条路更容易走到。
        if self._budget.exhausted:
            self._llm_exhausted = True
        calls = self._budget.calls - calls_before
        failures = self._budget.failed - failed_before
        stats["calls"] = stats.get("calls", 0) + calls
        stats["failures"] = stats.get("failures", 0) + failures
        stats["ok"] = stats.get("ok", 0) + (self._budget.ok - ok_before)
        if calls:
            progress.counters.llm_calls += calls
        if failures:
            progress.counters.llm_failures += failures
        if self._cache is not None:
            hits = self._cache.hits - hits_before
            stats["cache_hits"] = stats.get("cache_hits", 0) + hits
            if hits:
                progress.counters.llm_cache_hits += hits
        return produced

    def _is_llm(self, generator: Any) -> bool:
        return getattr(getattr(generator, "rule", None), "kind", "") == "llm"

    def _sample_limit(self, generator: Any) -> int:
        """`sample_rows` 只截大模型字段：本地随机生成不花钱、也不慢，截它只会让人困惑。"""
        if not self._is_llm(generator):
            return 0
        return int(self.config.llm.sample_rows or 0)

    # -- 约束与回退 --------------------------------------------------------

    def _enforce(self, table: Table, run: _FileRun, base: int) -> Table:
        if not self._constrained:
            return table
        if not table.nrows():
            return table
        progress = self._expect_progress()
        for spec in self._constrained:
            constraints = list(spec.constraints)
            unique = any(item.kind == "unique" for item in constraints)
            index = progress.unique_index(spec.dest) if unique else None
            values = list(table.column(spec.dest))
            violations = eval_column(values, constraints, spec.dest, index)
            if self._verify_every and run.batch_no % self._verify_every == 0:
                self._verify_column(values, constraints, spec.dest, index)
            for key, number in count_warnings(values, constraints, spec.dest).items():
                if number:
                    field_name, _, kind = key.partition(":")
                    progress.counters.note_violation(field_name, f"warn:{kind}")
            for position, violation in enumerate(violations):
                if violation is None:
                    if index is not None:
                        index.add(values[position])
                    continue
                progress.counters.note_violation(spec.dest, violation.kind)
                values[position] = self._resolve_row(
                    spec, constraints, values, position, base, run, index, table
                )
                if index is not None:
                    # 解决一行就立刻登记一行：**不能攒到最后批量登记** —— 回退可能产出与
                    # 后面行相同的值，而后面行的判重必须能看到它（见 UniqueIndex.check_batch）。
                    index.add(values[position])
            table = table.with_column(spec.dest, values)
        return table

    def _resolve_row(
        self,
        spec: FieldSpec,
        constraints: Sequence[Constraint],
        values: list[Any],
        position: int,
        base: int,
        run: _FileRun,
        index: Any,
        table: Table,
    ) -> Any:
        progress = self._expect_progress()
        original = values[position]
        current = original
        fallback = spec.fallback or Fallback()
        policy = fallback.on_violation
        generator = self._generator_for(spec.dest)
        rounds = 1 + (int(fallback.max_retries) if policy == "retry" and generator else 0)
        for attempt in range(rounds):
            violation = check_value(current, constraints, spec.dest, index)
            if violation is None:
                return current
            if policy == "retry" and generator is not None and attempt < rounds - 1:
                progress.counters.retries += 1
                fresh = self._regenerate(
                    generator, spec.dest, table, run, base + position, position,
                    attempt + 1, violation,
                )
                if fresh is not None and fresh != current:
                    current = fresh
                    continue
            if policy == "retry":
                # 重试次数用尽：保留最后一次生成的值，并明确记为降级 —— 不能转圈，
                # 也不能假装成功。
                self._note_fallback(
                    spec.dest, violation, policy, degraded=True,
                    note="重试后仍不合规，保留最后一次生成的值",
                    run=run, position=base + position, before=original, after=current,
                )
                return current
            constraint = _constraint_for(current, violation, constraints, spec.dest, index)
            result = apply_fallback(current, violation, fallback, constraint, index)
            self._note_fallback(
                spec.dest, violation, policy, degraded=bool(result.degraded),
                note=result.note, run=run, position=base + position, before=original,
                after=result.value,
            )
            if result.changed and result.value != current:
                # 修完再验一轮：截断可能撞上唯一性，那一轮由 apply_fallback 自己追加后缀
                current = result.value
                continue
            return current
        return current

    def _regenerate(
        self,
        generator: Any,
        dest: str,
        table: Table,
        run: _FileRun,
        absolute: int,
        local: int,
        attempt: int,
        violation: Any,
    ) -> Any:
        """带着「上一版哪里不合格」的反馈重新生成一行，返回该字段的新值。"""
        row = _row_dict(table, local) if 0 <= local < table.nrows() else {}
        batch = GenerationBatch(
            indices=[absolute],
            rows=[row],
            attempt=attempt,
            feedback=[violation.detail or violation.kind],
        )
        produced = self._call_generator(generator, batch, [dest])
        if not produced:
            return None
        item = produced[0] or {}
        # 多字段规则：兄弟字段的值写回表（它们是同一次生成的产物，只取一个会让同一行里
        # 「一起生成的两列」来自两次不同的生成）。
        for name, value in item.items():
            if name == dest or table.find(name) is None:
                continue
            if 0 <= local < table.nrows():
                table.column(name)[local] = value
        return item.get(dest)

    def _generator_for(self, dest: str) -> Any:
        for generator in self._generators:
            if dest in generator.output_fields:
                return generator
        return None

    def _note_fallback(
        self,
        field_name: str,
        violation: Any,
        policy: str,
        *,
        degraded: bool,
        note: str,
        run: _FileRun,
        position: int,
        before: Any,
        after: Any,
    ) -> None:
        progress = self._expect_progress()
        progress.counters.note_fallback(
            field_name, violation.kind, policy, degraded=degraded
        )
        sample = {
            "file": run.key,
            "row": position,
            "field": field_name,
            "kind": violation.kind,
            "policy": policy,
            "value": _sample_text(before, self.secrets),
            "result": _sample_text(after, self.secrets),
            "detail": _sample_text(violation.detail or "", self.secrets),
            "note": _sample_text(note or "", self.secrets),
            "degraded": degraded,
        }
        progress.add_fallback_sample(sample)
        self._append_fallback(sample)
        # 计数永远精确；日志行按 (文件,字段,类型,策略) 首次 + 每个数量级各一条
        total = sum(progress.counters.field_fallbacks(field_name).values())
        if self._gate.should_log((run.key, field_name, violation.kind, policy), total):
            self._log(
                "error" if degraded else "warn",
                f"回退命中：{run.key} 字段「{field_name}」{violation.kind}"
                f"（{policy}{'，已降级' if degraded else ''}）：{note or ''}"
                f"（该类累计 {total} 次）",
                field=field_name,
                kind=violation.kind,
                policy=policy,
            )

    def _verify_column(
        self,
        values: Sequence[Any],
        constraints: Sequence[Constraint],
        field: str,
        index: Any,
    ) -> None:
        """列式快路径 vs 逐行参考实现的整批对拍（`verify_every` 打开时，开发/排障用）。"""
        try:
            problems = cross_check(values, constraints, field, index)
        except Exception as exc:
            self._log("warn", f"参考实现对拍本身出错（{field}）：{exc}")
            return
        if not problems:
            return
        self._log("error", f"列式求值器与参考实现不一致（{field}）：{problems[0]}")
        self._note(
            f"⚠ 列式求值器与逐行参考实现在字段 `{field}` 上不一致：{problems[0]}"
            "（报告里该字段的违规计数可能有偏差，请把这个情况反馈出来）"
        )

    # -- 复检 --------------------------------------------------------------

    def _regress(self, after: Sequence[list[Any]], run: _FileRun) -> None:
        """对**输出值**跑同一套约束。与正向清洗共用同一个列式求值器、同样的输出行序 ——
        输入序列相同，结果必然相同。这条等价性是报告可信度的根，所以它是「同一份数据跑
        两遍」而不是「另写一套校验」。

        唯一性用的是**另一条索引**（`after_unique_index`）：正向那条记的是「接受过的
        值」，其中包括回退**改掉之前**的原值，拿它来复检会把每一行都判成重复（值早就
        在索引里了）。复检问的是另一个问题 ——「输出列里还剩多少重复」—— 所以它从空索引
        开始，边问边登记，答案就是「第 n 次出现的第 2 个起算违规」。
        """
        if not self._constrained or self.progress is None or not after:
            return
        progress = self.progress
        for spec in self._constrained:
            if spec.dest not in self._dest:
                continue
            constraints = list(spec.constraints)
            unique = any(item.kind == "unique" for item in constraints)
            index = None
            if unique:
                if spec.dest in self._post_touched:
                    # post 函数改写了唯一性字段：索引里是 post 之前的值，两者不再对应。
                    # 与其报一个假的重复，不如明说这个字段没复检唯一性。
                    progress.note_once(
                        f"post_unique:{spec.dest}",
                        f"字段 `{spec.dest}` 有 unique 约束，但 post 用户函数会改写它 —— "
                        "唯一性索引记录的是改写之前的值，因此回归复检不对它校验唯一性。"
                        "要检查请把约束挪到 post 函数不改写的字段上。",
                    )
                else:
                    index = progress.after_unique_index(spec.dest)
            values = after[self._dest.index(spec.dest)]
            for key, number in count_warnings(values, constraints, spec.dest).items():
                if number:
                    field_name, _, kind = key.partition(":")
                    progress.after_counters.note_violation(field_name, f"warn:{kind}")
            for position, violation in enumerate(
                eval_column(values, constraints, spec.dest, index)
            ):
                if index is not None:
                    # 复检阶段不再有回退，**违规值也照样登记**：不清洗了，第三次出现的
                    # 同一个值仍然是重复，只登记首次出现会把它漏掉。
                    index.add(values[position])
                if violation is not None:
                    progress.after_counters.note_violation(spec.dest, violation.kind)

    # -- 提交与收尾 --------------------------------------------------------

    def _commit(self, run: _FileRun) -> None:
        """持久提交。**顺序即协议**，见模块 docstring 第 1 条。"""
        state = self._expect_state()
        progress = self._expect_progress()
        fs = run.fs
        if run.writer is not None:
            fs.out_bytes = run.writer.flush()
        progress.flush()
        if self.bus is not None:
            self.bus.flush_log(self.task_id)
        fs.rows_in = run.read_rows
        fs.rows_out = run.rows_out
        fs.dropped = run.dropped
        if fs.status == "pending":
            fs.status = "running"
        fs.carry = dict(progress.ops.carry)
        state.progress = progress.snapshot()
        state.regression_done = list(progress.regression_done)
        state.llm = self._llm_state()
        state.notes = list(self._notes)
        state.logs = LogCoords(
            events_bytes=_size_of(self.store.events_path),
            fallback_bytes=self._fallback_bytes,
        )
        state.updated_at = now_iso()
        self.store.save(state)

    def _llm_state(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "per_field": {
                name: dict(values) for name, values in self._llm_per_field.items()
            },
            "exhausted": bool(self._llm_exhausted),
            "budget": self._budget.snapshot(),
        }
        if self.llm_pool is not None:
            payload["endpoints"] = self.llm_pool.snapshot()
        return payload

    def _append_fallback(self, sample: Mapping[str, Any]) -> None:
        """回退明细逐行 append 落盘。**不是每批 flush** —— 明细是「事后查一条具体记录」
        用的，丢尾部不影响正确性，而每次提交都 fsync 一个大文件会把吞吐拖垮。"""
        import orjson

        if self._fallback_bytes >= FALLBACK_LOG_BYTES:
            self._fallback_dropped += 1
            return
        try:
            line = orjson.dumps(dict(sample)) + b"\n"
            with open(self.store.fallback_path, "ab") as handle:
                handle.write(line)
            self._fallback_bytes += len(line)
        except OSError:
            LOGGER.debug("回退明细写入失败", exc_info=True)

    def _report_progress(self, *, force: bool = False, message: str = "") -> None:
        if self._meter is None:
            return
        now = self._clock()
        if not force and now - self._last_progress_at < PROGRESS_INTERVAL:
            return
        self._last_progress_at = now
        payload = self.progress_payload()
        if message:
            payload["message"] = message
        if self.bus is not None:
            self.bus.progress(self.task_id, payload, message=message, force=force)
        if self.on_progress is not None:
            try:
                self.on_progress(payload)
            except Exception:
                LOGGER.debug("进度回调失败", exc_info=True)
        self._report_metrics(now, force=force)

    def _report_metrics(self, now: float, *, force: bool = False) -> None:
        """端点级指标（在途/成功/失败/延迟/熔断）随进度一起推给界面。"""
        if self.bus is None or self.llm_pool is None:
            return
        if not force and now - self._last_metrics_at < METRICS_INTERVAL:
            return
        self._last_metrics_at = now
        self.bus.emit(
            self.task_id,
            KIND_LLM,
            "模型端点指标",
            level="debug",
            data={
                "endpoints": self.llm_pool.snapshot(),
                "budget": self._budget.snapshot(),
            },
        )

    def _checkpoint(self, where: str = "") -> None:
        """块间检查点：暂停在此生效，取消在此抛出。**不能放在块内** —— 一个两万行的块配
        两个大模型字段就是几十分钟不可中断。"""
        if self.control is None:
            return
        was_paused = bool(getattr(self.control, "paused", False))
        try:
            self.control.checkpoint()
        except TaskCancelled:
            self._log("warn", f"任务被取消（{where or '检查点'}）")
            raise
        if was_paused and not self.control.paused:
            self._log("info", "已从暂停继续")

    def _close_regression(self) -> None:
        progress = self.progress
        state = self.state
        if progress is None or state is None:
            return
        try:
            progress.regression_done = state.done_keys()
            findings = regression_findings(
                progress.before, progress.after, progress.counters, self._dest
            )
        except Exception as exc:
            self._log("warn", f"复检结论生成失败：{exc}")
            return
        if self._post_functions:
            findings.append(
                "- 复检对**最终产物**（含 post 用户函数）求值；若某个字段在 post 阶段被改写，"
                "它的「清洗后违规」反映的是改写之后的值。"
            )
        if self._llm_exhausted:
            findings.append(
                "- ⚠ 本任务的大模型调用次数达到上限，上限之后的生成被跳过，"
                "受影响的行按回退策略处理（见「约束检查与回退」）。"
            )
        state.regression = list(findings)

    def _finish(
        self,
        state: TaskState,
        *,
        status: str,
        error: str,
        cancelled: bool,
        started: float,
    ) -> EngineResult:
        progress = self.progress
        if progress is None:
            # 启动期就失败（`_build_runtime` 抛异常）：这里**不能**再用 `_expect_progress()`
            # 抛出去 —— `run()` 的契约是「不抛异常，失败也返回带状态的结果」，而启动失败
            # 恰恰是最需要一份报告说明原因的时候。抛出去的结果是用户看到 500、磁盘上什么都
            # 没有，连失败原因都只留在进程日志里。
            self._log("error", "引擎未能启动，跳过累加器相关步骤")
        state.status = status
        state.finished_at = now_iso()
        state.updated_at = state.finished_at
        if error and status != "completed":
            # 任务级原因也要进报告：一份只有「失败」没有原因的产物等于让用户去猜。
            # （TaskState 没有 error 字段，笔记是唯一会持久化并渲染出来的地方。）
            self._note(f"任务级原因：{error}")
        self._close_regression()
        state.notes = list(self._notes)
        report = ""
        # 状态与报告**分开兜异常**：`state.json` 是续跑的坐标，报告是给人看的。把两者放
        # 在同一个 try 里，会让「报告生成失败」连状态一起丢掉 —— 于是取消过的任务既没有
        # 产物坐标也没有报告，用户看到的是「什么都没发生」。
        try:
            # `state.progress` 是**提交坐标**，不是「现在内存里有什么」。
            # 完成时两者相等（最后一次提交之后只剩收尾动作，不再动任何累加器），所以直接
            # 刷新；取消/失败时不能刷新 —— 现场那份含着没提交的那一批，记下去续跑就会把它
            # 当成已经做完的（索引里多了没提交的摘要、计数被数了两遍）。
            if progress is None:
                # 没有累加器，所以**提交坐标一个字节都不动**：`state.progress` 还是上一次
                # 提交留下的那份。只有一次都没提交过时才补一份清零快照，好让报告与续跑判定
                # 有个形状（`zeroed_snapshot` 保留索引键，见它的 docstring）。
                if not state.progress:
                    state.progress = TaskProgress(
                        self.config, None, seed=self.seed
                    ).zeroed_snapshot()
            elif status == "completed":
                state.progress = progress.snapshot()
            elif not state.progress:
                # 一次都没提交过：记一份清零但保留索引键的快照（见 zeroed_snapshot）
                state.progress = progress.zeroed_snapshot()
            # 大模型那一段走另一条口径：调用次数与耗材是**真花掉的**，报告要说实话，
            # 所以取消时也取现场值（预算护栏因此偏保守：重复的那一批会再算一次）。
            state.llm = self._llm_state()
            self.store.save(state)
        except Exception as exc:
            LOGGER.warning("状态落盘失败", exc_info=True)
            self._log("error", f"状态落盘失败：{exc}")
        try:
            report = state_to_report(
                self.config,
                state,
                task_id=self.task_id,
                secrets=self.secrets,
                writer_counters=self._writer_counters,
            )
            self._write_report(report)
            if self.config.output.durable_commit:
                for path in (self.store.state_path, self.report_path()):
                    if Path(path).exists():
                        _fsync_file(Path(path))
        except Exception as exc:
            LOGGER.warning("报告生成失败", exc_info=True)
            self._log("error", f"报告生成失败：{exc}")
        finally:
            if progress is not None:
                progress.close()
            if self._python is not None:
                try:
                    self._python.close()
                except Exception:
                    LOGGER.debug("关闭子进程池失败", exc_info=True)
                self._python = None
            if self.llm_client is not None and self.llm_pool is None:
                # 池是全局的、任务之间共用，只有自己建的客户端才归自己关
                try:
                    self.llm_client.close()
                except Exception:
                    LOGGER.debug("关闭大模型客户端失败", exc_info=True)
        if not report:
            report = "# 文件清洗报告\n\n> 报告生成失败，请查看任务日志。\n"
        duration = self._clock() - started
        labels = {"completed": "完成", "failed": "失败", "cancelled": "已取消"}
        self._log("info", f"任务{labels.get(status, status)}：用时 {duration:.1f}s")
        if self.bus is not None:
            self.bus.emit(
                self.task_id,
                KIND_REPORT,
                "报告已生成",
                level="info" if status == "completed" else "warn",
                data={"status": status, "duration": round(duration, 2), "error": error},
            )
            # `flush_progress` 自己就是「强制发一次」，别再传 force —— 一个不存在的关键字
            # 参数会在任务**已经跑完之后**抛异常，于是产物与报告都在、任务却显示失败。
            self.bus.flush_progress(self.task_id)
            self.bus.flush_log(self.task_id)
        return EngineResult(
            task_id=self.task_id,
            status=status,
            report=report,
            files=_file_reports(state),
            state=state,
            error=error,
            duration=duration,
            cancelled=cancelled,
        )

    def _write_report(self, report: str) -> None:
        path = self.report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(report, encoding="utf-8")
        os.replace(tmp, path)

    # -- 断言辅助 ----------------------------------------------------------

    def _expect_state(self) -> TaskState:
        if self.state is None:
            raise CleanEngineError("引擎还没启动")
        return self.state

    def _expect_progress(self) -> TaskProgress:
        if self.progress is None:
            raise CleanEngineError("引擎还没启动")
        return self.progress

    def _python_event(self, level: str, message: str) -> None:
        self._log(level if level in ("warn", "error") else "info", message)


# =========================================================================== 工具函数


def report_path(store: StateStore) -> Path:
    """`report.md` 的唯一位置。引擎写它、API 读它，必须是同一个路径。"""
    return Path(store.dir) / "report.md"


def _blank(spec: FieldSpec) -> Any:
    return "" if spec.default is None else spec.default


def _row_dict(table: Table, position: int) -> dict[str, Any]:
    return {name: table.data[index][position] for index, name in enumerate(table.columns)}


def _transpose(columns: Sequence[Sequence[Any]], count: int) -> list[list[Any]]:
    if not count:
        return []
    return [list(row) for row in zip(*columns)]


def _constraint_for(
    value: Any,
    violation: Any,
    constraints: Sequence[Constraint],
    field: str,
    index: Any,
) -> Constraint:
    """违规 → 具体是哪条约束。同类型的约束可能有多条，用「单独判一次谁会报」来定 ——
    `apply_fallback` 需要具体的约束参数（正则模式、长度上下界），给错了会修出别的形状。"""
    fallback: Constraint | None = None
    for constraint in constraints:
        if constraint.kind != violation.kind:
            continue
        if fallback is None:
            fallback = constraint
        if check_value(value, [constraint], field, index) is not None:
            return constraint
    return fallback or constraints[0]


def _sample_text(value: Any, secrets: Sequence[str]) -> str:
    text = "" if value is None else str(value)
    if len(text) > SAMPLE_TEXT_LIMIT:
        text = text[:SAMPLE_TEXT_LIMIT] + "…"
    return redact(text, secrets)


def _fingerprint(path: str | Path) -> tuple[int, int]:
    try:
        info = os.stat(path)
    except OSError:
        return 0, 0
    return int(info.st_size), int(info.st_mtime_ns)


def _size_of(path: str | Path) -> int:
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


def _fsync_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        LOGGER.debug("fsync %s 失败", path, exc_info=True)


def _is_append_format(config: CleanTaskConfig) -> bool:
    return config.output.format in ("csv", "jsonl")


def _clear_file_outputs(target: Path) -> None:
    """开写之前清掉这个文件可能残留的产物（上一次跑到一半的 .part / spool）。

    不清的话，「从头重跑」会把新内容接在旧内容后面，产出一份「看起来正常但中间重复了
    一段」的文件。
    """
    for candidate in (
        target,
        Path(str(target) + ".part"),
        Path(str(target) + ".part.csv"),
    ):
        try:
            candidate.unlink()
        except OSError:
            pass


def _clear_outputs(output_dir: Path) -> None:
    """新任务开始时清空产出目录。**只清产出** —— 输入目录不归引擎管。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(
        output_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            pass


def _file_reports(state: TaskState) -> list[FileReport]:
    mapping = state.extra.get("mapping") or {}
    reports: list[FileReport] = []
    for entry in state.ordered():
        info = mapping.get(entry.key) or {}
        reports.append(
            FileReport(
                name=entry.key,
                size=entry.size,
                status=entry.status,
                rows_in=entry.rows_in,
                rows_out=entry.rows_out,
                dropped=entry.dropped,
                parse_errors=entry.parse_errors,
                ragged_rows=entry.ragged_rows,
                replacement_chars=entry.replacement_chars,
                seconds=entry.seconds,
                message=entry.message,
                column_map=dict(info.get("column_map") or {}),
                unmapped=tuple(info.get("unmapped") or ()),
            )
        )
    return reports


def state_to_report(
    config: CleanTaskConfig,
    state: TaskState,
    *,
    task_id: str = "",
    secrets: Iterable[str] = (),
    writer_counters: Mapping[str, int] | None = None,
) -> str:
    """从 `state.json` 生成报告。

    引擎收尾与 API 的 `/report` 共用这一处实现：两处各写一遍，报告就会在「任务刚跑完」
    与「刷新页面再看」两种情况下不一样 —— 那种差异最难查，因为两边看起来都合理。
    部分完成（中断/取消）的任务也能生成：它是当时快照的忠实描述。
    """
    snapshot = state.progress or {}
    progress = TaskProgress(config, None, seed=int(snapshot.get("seed", 0) or 0))
    if snapshot:
        progress.restore(snapshot)
    llm = dict(state.llm or {})
    budget = dict(llm.get("budget") or {})
    per_field = llm.get("per_field") or {}
    usage: dict[str, Any] = {
        "calls": int(budget.get("calls", 0) or 0),
        "cache_hits": int(budget.get("cache_hits", 0) or 0)
        or sum(int(item.get("cache_hits", 0) or 0) for item in per_field.values()),
        "failures": int(budget.get("failed", 0) or 0),
        "exhausted": bool(llm.get("exhausted")),
        "aborted": bool(budget.get("aborted")),
        "abort_error_rate": config.llm.abort_error_rate,
        "per_field": per_field,
        "endpoints": [
            {
                "id": item.get("id", ""),
                "name": item.get("name", "") or item.get("model", ""),
                "model": item.get("model", ""),
                "ok": item.get("ok", 0),
                "failed": item.get("failed", 0),
                "latency_ms": item.get("ewma_ms", 0.0),
            }
            for item in (llm.get("endpoints") or [])
        ],
    }
    data = ReportInput(
        config=config,
        task_id=task_id or state.task_id,
        status=state.status,
        files=_file_reports(state),
        before=progress.before,
        after=progress.after,
        counters=progress.counters,
        after_counters=progress.after_counters if config.fields else None,
        ops_counters=progress.ops.counters,
        notes=list(state.notes or []) + list(progress.ops.notes.values()),
        fallback_samples=progress.fallback_samples,
        regression=list(state.regression or []),
        llm=usage,
        started_at=_parse_time(state.started_at or state.created_at),
        finished_at=_parse_time(state.finished_at),
        resumed=bool(state.resume_count),
        secrets=tuple(secrets),
        extra={"writer": dict(writer_counters or {})},
    )
    return build_report(data)


def _parse_time(text: str) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(text)[:19], fmt)
        except ValueError:
            continue
    return None
