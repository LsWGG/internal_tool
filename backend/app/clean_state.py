"""断点续跑的持久化：`state.json` 原子快照 + append-only 文件截断重放。

# 一致性协议（顺序本身就是协议）

每个提交点（commit）按**固定顺序**做四件事，顺序不能换：

    1. writer.flush()          → 产出文件的字节数
    2. progress.flush()        → 唯一/去重日志（摘要）、events/fallback 日志
    3. state.json 落盘         ← **这一步才是提交**
    4. os.replace(tmp, state)  → 原子

崩溃发生在 1–3 之间时，`state.json` 仍指向**上一个**提交点，于是恢复时每个 append-only
文件都被砍回它记录的长度重来一遍 —— 重放严格幂等。

反过来（先写 state.json 再 flush 数据）会得到「状态说写完了、文件里没有」，那是数据丢失，
比重复计算严重得多。

# 两类 append-only 文件，两种处理

| 文件 | 恢复动作 |
|---|---|
| 产出（`.part` / `.part.csv` / `.csv` / `.jsonl`） | 由 `clean_io._open_append` 按记录字节数截断 —— **本模块不碰产出文件** |
| 唯一 / 去重摘要日志（定长 8 字节） | `os.truncate(count*8)`，然后 `np.sort` 重建索引 |
| `events.jsonl` / `fallback.jsonl` | 不截断，允许丢尾部（丢日志可接受，丢数据不行） |
| `llm_cache.jsonl` | 不截断，重放时**最后一个 key 赢**，坏行跳过 |

产出文件的截断刻意留在这里之外：只有 writer 知道哪个文件是真正的追加目标（xlsx/parquet
写的是 `.part.csv` spool），两边各写一遍命名规则就是续跑错位的开始。

# 拒绝续跑的条件（一律给出明确原因）

配置指纹变了、输入文件的大小或修改时间变了、输入文件集合变了、状态文件版本不认识、
状态文件损坏、产出文件与记录对不上 —— 任何一条命中就**不续跑**，让用户明确选择
「重跑（清空产物）」或「只下载已有产物」，而不是悄悄重跑一遍让用户以为产物是接上的。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clean_io import append_target
from .clean_models import CleanTaskConfig
from .clean_ops import OpContext
from .clean_schema import UniqueIndex
from .clean_stats import Counters, StatsSet

STATE_VERSION = 1
STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"
FALLBACK_FILENAME = "fallback.jsonl"
LLM_CACHE_FILENAME = "llm_cache.jsonl"
LOG_DIRNAME = "logs"
# 内存里最多留几条回退样本。报告里列几条是另一回事（`output.top_k`），这里的上限只
# 决定「报告最多能列到第几条」，同时也是 state.json 不至于被样本撑大的护栏。
FALLBACK_SAMPLE_LIMIT = 200


class StateError(Exception):
    """状态文件本身的问题（版本、损坏、坐标对不上）。调用方据此判「不可续跑」。"""


# =========================================================================== 工具


def now_iso() -> str:
    """时间一律存 UTC（与 task_manager 的口径一致）；报告展示时再转本地时区。"""
    return datetime.now(timezone.utc).isoformat()


def to_local(text: str | None) -> datetime | None:
    """UTC ISO → 本地时间的 naive datetime，交给报告格式化。

    报告是给人看的，「开始时间 03:12」而用户墙上挂钟显示 11:12 会让人以为任务没跑。
    """
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def fingerprint(path: str | Path) -> tuple[int, int]:
    """输入文件的身份：(字节数, mtime_ns)。

    续跑要的是「这跟我上次读的是同一个文件吗」。大小 + mtime 能挡住绝大多数「文件被换过了」
    的情形，代价是零（`os.stat` 一次）。内容摘要更可靠但要读完整个 GB 文件 —— 那个代价
    每次启动都要付，不划算。
    """
    info = os.stat(path)
    return int(info.st_size), int(info.st_mtime_ns)


def _log_name(prefix: str, key: str) -> str:
    """日志文件名：可读前缀 + 内容摘要。

    摘要不是装饰：两个不同的去重键**绝不能**共用一个日志，那会把两个索引静默合并，
    结果是随机丢行 —— 产物看起来完全正常。所以宁可名字丑也要保证单射。
    """
    label = re.sub(r"[^\w一-鿿]+", "_", key, flags=re.UNICODE).strip("_")[:24]
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest()
    return f"{prefix}-{label or 'k'}-{digest}.log"


def truncate_file(path: Path, size: int) -> str:
    """把文件砍回 size 字节。返回一句日志（空 = 没动它）。

    只在**文件更大**时动手：文件比记录的小意味着产物被外部删改过，那是要报错的，
    而这里悄悄补零或从头开始都会产出错误结果。
    """
    if not path.exists():
        return ""
    actual = path.stat().st_size
    if actual == size:
        return ""
    if actual < size:
        raise StateError(
            f"{path.name} 只有 {actual} 字节，提交点记录了 {size} 字节：产物被外部改动过，拒绝续跑"
        )
    with open(path, "r+b") as handle:
        handle.truncate(size)
    return f"{path.name} 截回 {size} 字节（丢弃了未提交的 {actual - size} 字节）"


def ensure_private_dir(path: Path) -> Path:
    """建目录并锁成 0700。这里放的是端点池与任务级密钥，别的用户不该读得到。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:                  # 某些文件系统不支持，不是致命问题
        pass
    return path


def write_private(path: Path, payload: bytes) -> None:
    """原子写一个只给本人读的文件（0600）。

    先写成 0600 的临时文件再 `os.replace` —— 直接写目标文件会有一个「内容已落盘但权限
    还没收紧」的窗口，而这类文件的窗口期正是它的全部意义所在。
    """
    path = Path(path)
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


# =========================================================================== 状态结构


@dataclass
class FileState:
    """一个输入文件的进度。`key` 是相对输入根的路径 —— 它同时是产物名与续跑身份。

    刻意不用「文件在列表里的下标」当身份：用户往目录里加一个文件，下标全体位移，
    续跑就会把 A 的进度安到 B 头上，而产物看起来毫无异常。
    """

    key: str
    path: str = ""
    size: int = 0
    mtime_ns: int = 0
    # pending: 还没开始 / running: 处理中（可续跑）/ done: 完成 / failed / skipped
    status: str = "pending"
    rows_in: int = 0            # 已提交读入的行数 = 续跑时要跳过的行数
    rows_out: int = 0
    out_bytes: int = 0          # 已提交的产出字节数（截断坐标）
    dropped: int = 0
    parse_errors: int = 0
    ragged_rows: int = 0
    replacement_chars: int = 0
    seconds: float = 0.0
    message: str = ""
    # ffill 的跨块携带值。文件边界上必须为空（见 OpContext.reset_file）
    carry: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status == "done"

    @property
    def started(self) -> bool:
        return self.status in ("running", "failed") or self.rows_in > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "status": self.status,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "out_bytes": self.out_bytes,
            "dropped": self.dropped,
            "parse_errors": self.parse_errors,
            "ragged_rows": self.ragged_rows,
            "replacement_chars": self.replacement_chars,
            "seconds": self.seconds,
            "message": self.message,
            "carry": dict(self.carry),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileState":
        return cls(
            key=str(data.get("key", "")),
            path=str(data.get("path", "")),
            size=int(data.get("size", 0)),
            mtime_ns=int(data.get("mtime_ns", 0)),
            status=str(data.get("status", "pending")),
            rows_in=int(data.get("rows_in", 0)),
            rows_out=int(data.get("rows_out", 0)),
            out_bytes=int(data.get("out_bytes", 0)),
            dropped=int(data.get("dropped", 0)),
            parse_errors=int(data.get("parse_errors", 0)),
            ragged_rows=int(data.get("ragged_rows", 0)),
            replacement_chars=int(data.get("replacement_chars", 0)),
            seconds=float(data.get("seconds", 0.0)),
            message=str(data.get("message", "")),
            carry=dict(data.get("carry") or {}),
        )


@dataclass
class LogCoords:
    """append-only 日志的提交坐标。**不截断**它们，记长度只为报告与排查。"""

    events_bytes: int = 0
    fallback_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"events_bytes": self.events_bytes, "fallback_bytes": self.fallback_bytes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LogCoords":
        return cls(
            events_bytes=int(data.get("events_bytes", 0)),
            fallback_bytes=int(data.get("fallback_bytes", 0)),
        )


@dataclass
class TaskState:
    """`state.json` 的全部内容。字段刻意保持小而固定 —— 大东西都在 append-only 文件里。"""

    task_id: str
    config_hash: str
    version: int = STATE_VERSION
    status: str = "running"
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    resume_count: int = 0
    files: dict[str, FileState] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    logs: LogCoords = field(default_factory=LogCoords)
    regression: list[str] = field(default_factory=list)
    regression_done: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 文件视图 ----------------------------------------------------------

    def file(self, key: str, path: str = "", size: int = 0, mtime_ns: int = 0) -> FileState:
        state = self.files.get(key)
        if state is None:
            state = self.files[key] = FileState(
                key=key, path=path, size=size, mtime_ns=mtime_ns
            )
        return state

    @property
    def partial(self) -> FileState | None:
        """正在跑、且已经提交过内容的文件（续跑要从它的行号接上）。"""
        for state in self.files.values():
            if state.status == "running" and state.rows_in > 0:
                return state
        return None

    def ordered(self) -> list[FileState]:
        return [self.files[key] for key in sorted(self.files)]

    def done_keys(self) -> list[str]:
        return sorted(key for key, state in self.files.items() if state.done)

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "config_hash": self.config_hash,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "resume_count": self.resume_count,
            "files": {key: state.to_dict() for key, state in self.files.items()},
            "progress": self.progress,
            "logs": self.logs.to_dict(),
            "regression": list(self.regression),
            "regression_done": list(self.regression_done),
            "notes": list(self.notes),
            "llm": dict(self.llm),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskState":
        version = int(data.get("version", 0))
        if version != STATE_VERSION:
            raise StateError(
                f"状态文件版本 {version} 不认识（本版本支持 {STATE_VERSION}），无法续跑"
            )
        task_id = str(data.get("task_id", ""))
        if not task_id:
            raise StateError("状态文件缺少 task_id，视为损坏")
        if not isinstance(data.get("files"), dict):
            raise StateError("状态文件缺少 files 段，视为损坏")
        state = cls(
            task_id=task_id,
            config_hash=str(data.get("config_hash", "")),
            version=version,
            status=str(data.get("status", "running")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            resume_count=int(data.get("resume_count", 0)),
            progress=dict(data.get("progress") or {}),
            logs=LogCoords.from_dict(data.get("logs") or {}),
            regression=[str(item) for item in (data.get("regression") or [])],
            regression_done=[str(item) for item in (data.get("regression_done") or [])],
            notes=[str(item) for item in (data.get("notes") or [])],
            llm=dict(data.get("llm") or {}),
            extra=dict(data.get("extra") or {}),
        )
        for key, entry in data["files"].items():
            if not isinstance(entry, Mapping):
                raise StateError(f"状态文件的 {key} 段不是对象，视为损坏")
            state.files[str(key)] = FileState.from_dict(entry)
        return state


# =========================================================================== 目录与文件


class StateStore:
    """任务目录的布局与 `state.json` 的读写。所有路径都在这里定义，别处不许拼。"""

    def __init__(self, task_dir: str | Path, *, durable: bool = False):
        self.dir = Path(task_dir)
        self.durable = durable          # 打开 = 每次提交 fsync（默认关，见模块 docstring）
        self.state_path = self.dir / STATE_FILENAME
        self.events_path = self.dir / EVENTS_FILENAME
        self.fallback_path = self.dir / FALLBACK_FILENAME
        self.llm_cache_path = self.dir / LLM_CACHE_FILENAME
        self.output_dir = self.dir / "output"
        self.input_dir = self.dir / "input"
        self.log_dir = self.dir / LOG_DIRNAME

    def ensure(self) -> "StateStore":
        for directory in (self.dir, self.output_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    # -- 日志路径 ----------------------------------------------------------

    def dedupe_log(self, key: str) -> Path:
        """去重索引的摘要日志。键是 (作用域, 字段集合) 派生的，重启前后同一个键。"""
        return self.log_dir / _log_name("dedupe", key)

    def unique_log(self, field: str) -> Path:
        """unique 约束的摘要日志。**按目的字段名**分的，不是按配置下标 —— 用户调整规则
        顺序后，下标变了而字段含义没变，按下标分会让续跑接错索引。"""
        return self.log_dir / _log_name("unique", field)

    def after_unique_log(self, field: str) -> Path:
        """回归复检（清洗后）用的唯一性日志。**必须与正向的 `unique_log` 分开**。

        两个索引问的是不同的问题：正向索引记录「已接受的值」，用来决定新的一行要不要
        走回退，因此里面**含被回退改掉之前的原值**；复检索引记录「输出列里已出现过的
        值」，用来数清洗后还剩多少重复。共用一个日志会让续跑的截断坐标互相污染：
        正向的 count 与复检的 count 不是同一个序列，截断谁都是错的。
        """
        return self.log_dir / _log_name("unique-after", field)

    def output_path(self, key: str, fmt: str) -> Path:
        """产出文件路径。`key` 是相对输入根的路径，子目录照样保留。"""
        safe = Path(key)
        name = safe.name or "output"
        if "." in name:
            name = name.rsplit(".", 1)[0]
        suffix = {"csv": ".csv", "xlsx": ".xlsx", "jsonl": ".jsonl", "parquet": ".parquet"}
        parent = self.output_dir
        # 只保留叶子目录一层：输入目录可能有很多层，产物目录跟着长没意义，还会撞上
        # 「不同目录下的同名文件」——那种撞名会静默覆盖产物，所以用父目录名做区分。
        if safe.parent.name:
            parent = parent / re.sub(r"[^\w一-鿿]+", "_", safe.parent.name)[:40]
        return parent / (name + suffix.get(fmt, "." + fmt))

    # -- 读写 --------------------------------------------------------------

    def load(self) -> TaskState | None:
        """读状态文件。**不存在**返回 None（新任务）；**损坏**抛 StateError。"""
        if not self.state_path.exists():
            return None
        try:
            import orjson

            data = orjson.loads(self.state_path.read_bytes())
        except ImportError:      # pragma: no cover - 只在缺 orjson 的环境里走到
            import json

            data = json.loads(self.state_path.read_text("utf-8"))
        except Exception as exc:
            raise StateError(f"状态文件无法解析：{type(exc).__name__}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise StateError("状态文件的顶层不是对象，视为损坏")
        return TaskState.from_dict(data)

    def save(self, state: TaskState) -> None:
        """原子落盘。**这是提交记录**，必须发生在所有 append-only 文件 flush 之后。"""
        import orjson

        state.updated_at = now_iso()
        payload = orjson.dumps(state.to_dict())
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(STATE_FILENAME + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if self.durable:
                os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)

    def delete(self) -> None:
        """清空状态（重跑时用）。**只删状态文件与索引日志**，产出目录由调用方决定。"""
        for path in (self.state_path, self.events_path, self.fallback_path,
                     self.llm_cache_path):
            try:
                path.unlink()
            except OSError:
                pass
        if self.log_dir.exists():
            for path in self.log_dir.iterdir():
                try:
                    path.unlink()
                except OSError:
                    pass

    # -- 截断 --------------------------------------------------------------

    def truncate_index_logs(self, state: TaskState) -> list[str]:
        """把唯一/去重日志砍回提交点。**必须在重建索引对象之前做** ——
        索引对象一构造就以追加模式打开在文件**物理末尾**，那时尾部若还留着未提交的摘要，
        新条目会接在它们后面，`count*8` 这个坐标就永久错位了。
        """
        actions: list[str] = []
        entries: list[tuple[Path, int]] = []
        for key, entry in (state.progress.get("dedupe") or {}).items():
            entries.append((self.dedupe_log(key), int((entry or {}).get("count", 0))))
        for field_name, count in (state.progress.get("unique") or {}).items():
            entries.append((self.unique_log(field_name), int(count or 0)))
        for field_name, count in (state.progress.get("after_unique") or {}).items():
            entries.append((self.after_unique_log(field_name), int(count or 0)))
        for path, count in entries:
            note = truncate_file(path, max(0, count) * 8)
            if note:
                actions.append(note)
        return actions


# =========================================================================== 进度累加器


class TaskProgress:
    """一次任务的全部跨块累加器，以及它们的快照/恢复。

    这些状态**必须**在续跑时精确还原，否则「续跑后的产物与不中断运行一致」就不成立：
    StatsSet 决定报告里的前后统计，Counters 决定违规与回退数，UniqueIndex 决定哪些行被
    判为重复（这一项错了就直接改变产物内容）。

    # 调用方必须遵守：提交点要与批切分对齐

    同一侧的「长度蓄水池」与「数值蓄水池」共用一条随机流（一个 StatsSet 一个生成器）。
    因此**批切分变了，两个蓄水池抽到的随机数就变了**，p50/p95 会与不中断运行不同 ——
    实测：批切分对齐时样本逐个相同，错位时不同。

    所以引擎必须保证 `commit_rows` 是读取批大小的整数倍，让续跑从一个完整的批边界开始。
    这条约束只在近似统计（p50/p95）上体现，产物内容不受影响。
    """

    def __init__(self, config: CleanTaskConfig, store: StateStore | None = None, *, seed: int = 0):
        self.config = config
        self.store = store
        self.seed = int(seed)
        # 频次上限是 Space-Saving 的误差界（N/limit）而不是展示条数；展示条数是 top_k。
        # 用一个下限免得用户把 top_k 调小以后报告里的高频值变得又少又不准。
        self.limit = max(1000, config.output.top_k)
        self.reservoir_size = config.output.reservoir_size
        self.before = self._new_stats(self.seed)
        # 两侧用不同的种子：共用一条随机流会让两边的蓄水池抽到完全相同的下标，
        # 于是「前后对比」在统计上不是独立的两次抽样（虽然样本量够大时无伤大雅）。
        self.after = self._new_stats(self.seed + 1)
        self.counters = Counters()
        self.after_counters = Counters()
        log_for = (lambda key: store.dedupe_log(key)) if store is not None else None
        self.ops = OpContext(log_for)
        self._unique: dict[str, UniqueIndex] = {}
        # 复检用的索引与正向索引是两套（见 StateStore.after_unique_log）：
        # 正向的成员里含被回退改掉之前的原值，复检的成员是清洗后的输出值
        self._after_unique: dict[str, UniqueIndex] = {}
        self.fallback_samples: list[dict[str, Any]] = []
        self.regression_done: list[str] = []

    def _new_stats(self, seed: int) -> StatsSet:
        return StatsSet(limit=self.limit, reservoir_size=self.reservoir_size, seed=seed)

    # -- unique 约束的索引 -------------------------------------------------

    def unique_index(self, field_name: str) -> UniqueIndex:
        """某个目的字段的 unique 约束索引。日志按字段名分，重启前后还是同一个文件。"""
        index = self._unique.get(field_name)
        if index is None:
            path = self.store.unique_log(field_name) if self.store is not None else None
            index = self._unique[field_name] = UniqueIndex(path)
        return index

    def after_unique_index(self, field_name: str) -> UniqueIndex:
        """复检（清洗后）看某个字段还有没有重复用的索引。

        与正向索引分开的第二个理由在 `check_batch` 的语义上：正向是「先问要不要改，
        再登记」，复检是「问完就登记」——同一份日志上两种用法得到的 count 不同，
        续跑时按哪个截断都会错。
        """
        index = self._after_unique.get(field_name)
        if index is None:
            path = self.store.after_unique_log(field_name) if self.store is not None else None
            index = self._after_unique[field_name] = UniqueIndex(path)
        return index

    @staticmethod
    def _rebuild(log_path: Path | None, count: int) -> UniqueIndex:
        """从摘要日志重建索引。**没有 store 时只造一个内存壳**（报告生成、干跑）。

        报告只读计数与统计，不需要索引成员 —— 而真的去读日志会以 `"ab"` 打开一个可能
        还不存在的文件，一次 `GET /report` 不该在磁盘上留下东西，也不该多出一个没人
        关闭的句柄（`ResourceWarning` 就是这么来的）。
        """
        if log_path is None:
            return UniqueIndex(None)
        return UniqueIndex.rebuild(log_path, max(0, int(count)))

    # -- 快照与恢复 --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "reservoir_size": self.reservoir_size,
            "seed": self.seed,
            "before": self.before.snapshot(),
            "after": self.after.snapshot(),
            "counters": self.counters.snapshot(),
            "after_counters": self.after_counters.snapshot(),
            "ops_counters": dict(self.ops.counters),
            "ops_notes": dict(self.ops.notes),
            "dedupe": self.ops.dedupe_snapshot(),
            "unique": {name: index.count for name, index in self._unique.items()},
            "after_unique": {
                name: index.count for name, index in self._after_unique.items()
            },
            "regression_done": list(self.regression_done),
            # 回退样本必须进快照：报告是**从 state.json 生成**的（引擎收尾与 `/report`
            # 共用 `_build_report`，那里是 `restore(snapshot)` 出来的新对象），不写进
            # 快照就等于那张样本表永远是空的 —— 报告里明明有回退次数、却一条样本都
            # 列不出来，还会倒过来赖到「断点续跑」头上。有界（见 `add_fallback_sample`），
            # 值也已经过 secrets 脱敏。
            "fallback_samples": [dict(item) for item in self.fallback_samples],
        }

    def zeroed_snapshot(self) -> dict[str, Any]:
        """「什么都没提交」的快照：计数与统计清零，但**保留索引键**。

        取消/失败可能落在第一次提交之前，而索引摘要是处理过程中就追加进日志的（提交点才
        记坐标）。于是磁盘上已经有「算过但没提交」的摘要了。这些键必须留在快照里，续跑
        才能按 `count=0` 把它们**截断掉** —— 不截断的话，续跑会把那些值当成已经见过：
        实测（task 作用域去重 + 第一批就被取消）会平白丢掉产物里的第一行，而报告里一行
        都不少，属于最难发现的那种错。计数与统计则必须是 0，因为那些行会从头再跑一遍。
        """
        empty = TaskProgress(self.config, None, seed=self.seed).snapshot()
        live = self.snapshot()
        empty["unique"] = {name: 0 for name in (live.get("unique") or {})}
        empty["after_unique"] = {name: 0 for name in (live.get("after_unique") or {})}
        empty["dedupe"] = {
            key: {"count": 0, **({"file_start": 0, "file": ""} if "file_start" in value else {})}
            for key, value in (live.get("dedupe") or {}).items()
        }
        return empty

    def restore(self, snapshot: Mapping[str, Any], *, current_file: str = "") -> None:
        """按快照重建累加器。**调用前必须先 truncate_index_logs**（见 StateStore）。

        `current_file` 是接着要跑的那个文件（不续跑就传空）。file 作用域的去重索引靠它
        判断可见区间：快照里记的文件名与它相同才重放，否则说明那个文件已经完成，索引必须
        从空开始。
        """
        self.limit = int(snapshot.get("limit", self.limit))
        self.reservoir_size = int(snapshot.get("reservoir_size", self.reservoir_size))
        self.before = StatsSet.restore(
            snapshot.get("before") or {},
            limit=self.limit,
            reservoir_size=self.reservoir_size,
            seed=self.seed,
        )
        self.after = StatsSet.restore(
            snapshot.get("after") or {},
            limit=self.limit,
            reservoir_size=self.reservoir_size,
            seed=self.seed + 1,
        )
        self.counters = Counters()
        self.counters.restore(snapshot.get("counters") or {})
        self.after_counters = Counters()
        self.after_counters.restore(snapshot.get("after_counters") or {})
        self.ops.counters = {str(k): int(v) for k, v in (snapshot.get("ops_counters") or {}).items()}
        self.ops.notes = {str(k): str(v) for k, v in (snapshot.get("ops_notes") or {}).items()}
        self.ops.restore_indexes(snapshot.get("dedupe") or {}, current_file=current_file)
        for name, count in (snapshot.get("unique") or {}).items():
            path = self.store.unique_log(name) if self.store is not None else None
            self._unique[name] = self._rebuild(path, int(count or 0))
        for name, count in (snapshot.get("after_unique") or {}).items():
            path = self.store.after_unique_log(name) if self.store is not None else None
            self._after_unique[name] = self._rebuild(path, int(count or 0))
        self.regression_done = [str(item) for item in (snapshot.get("regression_done") or [])]
        # 旧版本的 state.json 里没有这个键 —— 报告那边专门有一句话说清这种情况，
        # 不能默默少一张表（见 `_section_fallback`）。
        self.fallback_samples = [
            dict(item) for item in (snapshot.get("fallback_samples") or [])
        ][:FALLBACK_SAMPLE_LIMIT]

    # -- 提交与生命周期 ----------------------------------------------------

    def flush(self) -> None:
        """把摘要日志刷到 OS。提交顺序要求它发生在 state.json 落盘之前。"""
        self.ops.flush()
        for index in self._unique.values():
            index.flush()
        for index in self._after_unique.values():
            index.flush()

    def close(self) -> None:
        self.ops.close()
        for index in self._unique.values():
            index.close()
        for index in self._after_unique.values():
            index.close()
        self._unique.clear()
        self._after_unique.clear()

    def note_once(self, key: str, text: str) -> None:
        self.ops.note_once(key, text)

    def add_fallback_sample(
        self, sample: Mapping[str, Any], *, limit: int = FALLBACK_SAMPLE_LIMIT
    ) -> None:
        """回退明细的内存样本（有界）。完整明细在 `fallback.jsonl` 里，那份不截断。

        上限定成常数是为了让报告能说出「最多留 N 条」这句实话 —— 报告里的样本表条数是
        `output.top_k`（界面上可调，0 = 不列），两者不是一回事。
        """
        if len(self.fallback_samples) < limit:
            self.fallback_samples.append(dict(sample))


# =========================================================================== 续跑判定


@dataclass
class ResumeDecision:
    """能不能续跑，以及为什么。`code` 给程序看，`reason` 给人看。"""

    resumable: bool
    code: str
    reason: str
    notes: list[str] = field(default_factory=list)
    state: TaskState | None = None

    def __bool__(self) -> bool:
        return self.resumable


def _describe_changes(expected: FileState, actual: tuple[int, int]) -> str:
    size, mtime = actual
    if size != expected.size:
        return f"大小 {expected.size} → {size} 字节"
    return f"修改时间变了（{expected.mtime_ns} → {mtime}）"


def resolve_resume(
    state: TaskState | None,
    config: CleanTaskConfig,
    inputs: Sequence[tuple[str, str]],
    *,
    store: StateStore | None = None,
) -> ResumeDecision:
    """判断能否续跑。`inputs` 是 (`key`, 绝对路径) 的序列，顺序即处理顺序。

    所有拒绝理由都要**说清是哪个文件、差在哪** —— 「输入变了」这种话等于没说，
    用户没法据此决定是重跑还是去把文件改回来。
    """
    if state is None:
        return ResumeDecision(False, "fresh", "没有找到状态文件，按新任务处理")

    expected_hash = config.config_hash()
    if state.config_hash != expected_hash:
        return ResumeDecision(
            False, "config",
            f"清洗配置与上次不同（配置指纹 {state.config_hash[:8]} → {expected_hash[:8]}），"
            "不能把两次不同规则的产物接在一起",
            state=state,
        )

    current = {key: path for key, path in inputs}
    notes: list[str] = []
    for key in sorted(state.files):
        entry = state.files[key]
        path = current.get(key)
        if path is None:
            if entry.done:
                return ResumeDecision(
                    False, "inputs",
                    f"输入文件已不在这批里：{key}（它的产物已经产出，续跑无法保证产物完整）",
                    state=state,
                )
            # 没开始过的文件被移走了：不影响已产出的部分，记一句就够
            notes.append(f"上次记录的文件已不在输入里（未开始处理）：{key}")
            continue
        try:
            actual = fingerprint(path)
        except OSError as exc:
            return ResumeDecision(
                False, "inputs", f"输入文件读不到：{key}（{exc.strerror or exc}）", state=state
            )
        if entry.size and (actual[0] != entry.size or actual[1] != entry.mtime_ns):
            return ResumeDecision(
                False, "inputs",
                f"输入文件已变化：{key}（{_describe_changes(entry, actual)}）—— "
                "已跳过的行不再是原来的那些行，续跑会错位",
                state=state,
            )

    for key in sorted(current):
        if key not in state.files:
            return ResumeDecision(
                False, "inputs", f"输入里多了上次没有的文件：{key}", state=state
            )

    # 产出文件必须在**续写之前**就查一遍：等引擎跑起来才由 `_open_append` 报「文件不存在」
    # 是能拦住，但那时任务已经开始了，用户看到的是「跑到一半失败」而不是「不能续跑」。
    partial = state.partial
    if store is not None and partial is not None and partial.out_bytes:
        target = append_target(
            store.output_path(partial.key, config.output.format), config.output
        )
        if not target.exists():
            return ResumeDecision(
                False, "output",
                f"{partial.key} 的产出文件不见了（应是 {target.name}），"
                "无法从上次的字节位置接着写",
                state=state,
            )

    done = state.done_keys()
    if done:
        notes.append(f"已完成 {len(done)} 个文件，这些文件不会重跑")
    if state.partial is not None:
        notes.append(
            f"从 {state.partial.key} 的第 {state.partial.rows_in} 行继续"
        )
    return ResumeDecision(True, "resumed", "可以续跑", notes=notes, state=state)
