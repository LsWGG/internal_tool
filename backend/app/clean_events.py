"""任务的实时事件通道：订阅扇出 + 内存环形缓冲 + `events.jsonl` 持久化。

这一层只负责「把事件送出去」，不产生事件：引擎在工作线程里 `emit`，SSE 端点在事件循环里
`subscribe`。三件事必须在设计里写死，否则 GB 级任务上会以三种不同的方式失败：

1. **订阅者队列有界，满了丢最旧。** 一个卡住的浏览器标签页不能把服务端内存拖垮，更不能
   让引擎线程阻塞在 put 上。丢掉的条数记在 `Subscription.gap` 上，前端据此提示「日志过快，
   已省略 N 条」并走 REST 补齐。
2. **跨线程投递走 `loop.call_soon_threadsafe`。** `asyncio.Queue` 不是线程安全的，引擎线程
   直接 `put_nowait` 会在压力下静默损坏队列；反过来用 `queue.Queue` + 同步等待又会在事件
   循环里占住线程（40 个观察者就能把线程池占满）。所以：交给循环，由循环执行 `put_nowait`。
3. **持久化与扇出分开。** `events.jsonl` 是报告与下载产物的来源，由 `EventLog` 按批写盘
   （≥batch 条或 ≥interval 秒），并在引擎的提交点显式 `flush()`；内存环形缓冲只服务断线
   重放。不要在每次 `emit` 里同步写一行 JSON —— 那是每秒几千次小写。

告警日志的有界化（`LogGate`）也在这层：需求是「每次触发回退都要打告警」，但在 GB 级任务上
「每次一行」等于给一条配错的规则写几百万行，日志本身就废了。于是改为：**计数精确且始终
报告**，日志行只在每组 `(文件, 字段, 违规类型, 策略)` 首次出现时打一条，之后每个数量级
（10/100/1000…）再打一条 —— 用户仍然能看到实时升级，只是不会被淹没。明细样本另有
`fallback.jsonl`（不截断、有上限）。
"""

from __future__ import annotations

import asyncio
import itertools
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import orjson

from .clean_state import now_iso

# -- 级别与种类 -------------------------------------------------------------
#
# 级别名刻意与前端既有的日志面板口径一致（debug/info/warn/error，不是 logging 的
# warning/critical）。多一套映射表就多一处会写错的地方。
LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")
_LEVEL_RANK = {name: rank for rank, name in enumerate(LEVELS)}

KIND_LOG = "log"              # 一行人类可读日志
KIND_PROGRESS = "progress"    # 进度与 ETA（节流）
KIND_STATUS = "status"        # 任务状态变化
KIND_FALLBACK = "fallback"    # 回退命中（有界化之后）
KIND_LLM = "llm"              # 每端点指标
KIND_REPORT = "report"        # 报告就绪
KIND_RESYNC = "resync"        # 「你漏了 N 条，去 REST 补齐」
KIND_ERROR = "error"          # 任务级原因（续跑被拒、启动失败），不是一行普通日志

RING_SIZE = 2000
QUEUE_SIZE = 1000
BATCH_EVENTS = 50
BATCH_SECONDS = 1.0

# 进度事件每任务最快 2/s。这个数字是给任务列表用的：它每秒只需要「看起来是活的」，
# 而真正的实时性由日志行承载。
PROGRESS_INTERVAL = 0.5


def level_rank(level: str) -> int:
    return _LEVEL_RANK.get(level, _LEVEL_RANK["info"])


def _dumps(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")


@dataclass(slots=True)
class Event:
    """一条事件。`seq` 是全局单调序号，同时充当 SSE 的 `id:`。

    序号必须是**总线全局**的而不是每任务各自的：浏览器的 `Last-Event-ID` 只认一条流，
    两条流各自编号会让「从 12 之后补齐」变成歧义。
    """

    seq: int
    task_id: str
    kind: str
    level: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts or now_iso(),
            "task_id": self.task_id,
            "kind": self.kind,
            "level": self.level,
            "message": self.message,
            "data": self.data,
        }

    def sse(self) -> dict[str, str]:
        """sse-starlette 的 ServerSentEvent 形状。"""
        return {
            "id": str(self.seq),
            "event": self.kind,
            "data": _dumps(self.to_dict()),
        }


# =========================================================================== 订阅


def _put_drop_oldest(box: Any, item: Any, asyncio_queue: bool) -> Any:
    """入队；满了就丢最旧的一条。返回**被挤掉的那一条**（没挤掉任何东西则 None）。

    `asyncio.Queue` 与 `queue.Queue` 的方法名一样、异常类型不同，所以在这里一次收口。
    两种队列的 `put_nowait` 都不阻塞（有界队列满时抛的是「满」异常），丢最旧是唯一的
    不阻塞选项。

    刻意返回被挤掉的对象而不是「挤掉了没有」：丢失计数必须按**丢的是什么**来算，而不是
    按**谁在入队**。`resync` 占位帧自己也会挤掉一条真事件，那是真丢，得记上。
    """
    try:
        box.put_nowait(item)
        return None
    except Exception:
        pass
    empty = asyncio.QueueEmpty if asyncio_queue else queue.Empty
    try:
        evicted = box.get_nowait()
    except empty:
        evicted = None
    try:
        box.put_nowait(item)
    except Exception:
        return item if evicted is None else evicted
    return evicted




class Subscription:
    """一个订阅者。异步侧（SSE）用 `await get()`，同步侧（REST/测试）用 `drain()`。

    在事件循环里创建时用 `asyncio.Queue`（由循环持有并执行入队）；在循环外创建时用
    `queue.Queue`（测试与同步补齐）。这个分叉是必要的：`asyncio.Queue` 在循环外等不到
    东西，`queue.Queue` 在循环里等会占住线程。
    """

    def __init__(self, bus: "EventBus", task_id: str = "", *, min_level: str = "info",
                 maxsize: int = QUEUE_SIZE):
        self.bus = bus
        self.task_id = task_id
        self.min_level = min_level
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._loop = loop
        self._asyncio = loop is not None
        self._box: Any = asyncio.Queue(maxsize=maxsize) if loop is not None else queue.Queue(maxsize=maxsize)
        self.gap = 0              # 被丢弃的条数（背压 + 断线漏掉的）
        self.dropped_by_backpressure = 0
        self._reported = 0        # 已经通过 resync 帧告知消费者的那部分
        self._last_seq = 0        # 最近一次投递的序号，给合成的 resync 帧当 id
        self._closed = False
        self._lock = threading.Lock()

    # -- 生产端（引擎线程） ------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def matches(self, event: Event) -> bool:
        if event.task_id and self.task_id and event.task_id != self.task_id:
            return False
        # 全局流不收 debug：它是给任务列表用的，debug 只对打开某个任务的人有意义
        floor = self.min_level
        if not self.task_id and level_rank(floor) < level_rank("info"):
            floor = "info"
        return level_rank(event.level) >= level_rank(floor)

    def deliver(self, event: Event) -> None:
        """投递一条。可能被任何线程调用；事件循环的那份队列必须由循环自己入队。"""
        if self._closed:
            return
        if self._asyncio:
            loop = self._loop
            assert loop is not None
            try:
                loop.call_soon_threadsafe(self._deliver_now, event)
            except RuntimeError:
                # 循环已关闭（服务在关停）—— 订阅随任务一起消失，不是错误
                self._closed = True
            return
        self._deliver_now(event)

    def _deliver_now(self, event: Event) -> None:
        with self._lock:
            if event.seq:
                self._last_seq = event.seq
            # 队列里只放真事件。丢失的通知由**消费端**合成（见 `_resync_notice`），
            # 不占队列格子 —— 让通知占一格的后果是它会挤掉一条真事件，于是它所报告的
            # 那个数字被自己撑大了。
            if _put_drop_oldest(self._box, event, self._asyncio) is not None:
                self.gap += 1
                self.dropped_by_backpressure += 1

    # -- 消费端 ------------------------------------------------------------

    def _resync_notice(self) -> Event | None:
        """上次通知之后又丢了事件就合成一条 resync 帧。

        语义：`gap` 是**累计**丢掉的条数，`_reported` 是已经报给消费者的那部分；差值大于
        零就还要再报一次。重连时前端拿到的 `gap` 因此是「你一共漏了多少」，可以直接
        显示成「已省略 N 条」。

        调用方必须持有 `self._lock`：这段「读 gap → 记 reported」不能被打断，否则两个
        消费者会各看到一次（这里本来也只有单消费者，但别让正确性依赖那个前提）。
        """
        if self.gap <= self._reported:
            return None
        self._reported = self.gap
        return Event(
            seq=self._last_seq, task_id=self.task_id, kind=KIND_RESYNC, level="warn",
            message=f"日志过快，已省略 {self.gap} 条", data={"gap": self.gap},
        )

    async def get(self) -> Event:
        """异步取一条（SSE 用）。仅当订阅创建于事件循环内时可用。"""
        if not self._asyncio:
            raise RuntimeError("这个订阅建在事件循环之外，请用 drain()")
        with self._lock:
            notice = self._resync_notice()
        if notice is not None:
            return notice
        return await self._box.get()

    def drain(self, limit: int | None = None) -> list[Event]:
        """非阻塞取走当前积压（REST 补齐与测试用）。"""
        empty = asyncio.QueueEmpty if self._asyncio else queue.Empty
        items: list[Event] = []
        with self._lock:
            notice = self._resync_notice()
            if notice is not None:
                items.append(notice)
            while limit is None or len(items) < limit:
                try:
                    items.append(self._box.get_nowait())
                except empty:
                    break
        return items

    def close(self) -> None:
        self._closed = True
        self.bus.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# =========================================================================== 持久化


class EventLog:
    """一个任务的 `events.jsonl`。按批写盘，提交点由引擎显式 flush。

    尾部丢失是可接受的（丢日志可以，丢数据不行），所以这个文件**不参与截断重放**：
    续跑时新的行直接追加，报告读全文件、按 seq 去重即可。
    """

    def __init__(self, path: Path, *, batch: int = BATCH_EVENTS, interval: float = BATCH_SECONDS):
        self.path = Path(path)
        self.batch = max(1, int(batch))
        self.interval = float(interval)
        self._pending: list[bytes] = []
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self.written = 0

    def append(self, event: Event) -> None:
        line = orjson.dumps(event.to_dict(), option=orjson.OPT_NON_STR_KEYS) + b"\n"
        with self._lock:
            self._pending.append(line)
            if len(self._pending) >= self.batch or time.monotonic() - self._last >= self.interval:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            self._last = time.monotonic()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as handle:
            handle.write(b"".join(self._pending))
        self.written += len(self._pending)
        self._pending.clear()
        self._last = time.monotonic()

    def close(self) -> None:
        self.flush()

    @staticmethod
    def read(path: Path, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """读历史（断线补齐 / 报告）。损坏的尾行直接跳过 —— 崩溃时最后一行可能写了一半。"""
        items: list[dict[str, Any]] = []
        if not path.exists():
            return items
        with open(path, "rb") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if int(item.get("seq", 0)) <= after:
                    continue
                items.append(item)
                if len(items) >= limit:
                    break
        return items


# =========================================================================== 告警有界化


class LogGate:
    """同一组键只在**首次**与**每个数量级**放行一条。

    这是对「每次触发回退都要打告警」的刻意偏离，理由见模块 docstring：计数精确且始终在
    报告里，日志行按数量级抽稀，让用户在「不被淹没」与「看得见升级」之间拿到平衡。
    """

    THRESHOLDS = (1, 10, 100, 1000, 10000, 100000, 1000000, 10000000)

    def __init__(self) -> None:
        self._seen: dict[tuple, int] = {}
        self._lock = threading.Lock()

    def should_log(self, key: tuple, count: int) -> bool:
        """`count` 是该组键的累计次数。返回 True 表示这一条值得打。"""
        with self._lock:
            previous = self._seen.get(key)
            if previous is None:
                self._seen[key] = 1
                return True
            crossed = _crossed_threshold(previous, count)
            if crossed:
                self._seen[key] = count
            return crossed

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()

    @property
    def groups(self) -> int:
        with self._lock:
            return len(self._seen)


def _crossed_threshold(previous: int, count: int) -> bool:
    if count <= previous:
        return False
    for threshold in LogGate.THRESHOLDS:
        if previous < threshold <= count:
            return True
    return False


# =========================================================================== 总线


class EventBus:
    """扇出中心。全局一个实例，进程内共享。

    每个 `emit` 做三件事：进环形缓冲（服务断线重放）、扇给订阅者、追加到该任务的
    `EventLog`。三件事都不阻塞，所以引擎可以在工作线程里放心地每批调用。
    """

    def __init__(self, *, ring: int = RING_SIZE, maxsize: int = QUEUE_SIZE):
        self._seq = itertools.count(1)
        self._issued = 0                  # 已发出的最大序号：环形缓冲会绕圈，序号不能丢
        self._ring: deque[Event] = deque(maxlen=max(1, int(ring)))
        self._subs: list[Subscription] = []
        self._logs: dict[str, EventLog] = {}
        self._lock = threading.RLock()
        self._progress_at: dict[str, float] = {}
        self._progress_pending: dict[str, dict[str, Any]] = {}
        self._counts: dict[str, int] = {}
        self.maxsize = maxsize

    # -- 序号与环形缓冲 ----------------------------------------------------

    @property
    def ring(self) -> Sequence[Event]:
        with self._lock:
            return tuple(self._ring)

    @property
    def last_seq(self) -> int:
        """已发出的最大序号。环形缓冲为空（或一切都被忘了）时退化到内部计数。"""
        with self._lock:
            if self._ring:
                return self._ring[-1].seq
            return self._issued

    def counts(self) -> dict[str, int]:
        """按 kind 的事件计数，给测试与诊断用。"""
        with self._lock:
            return dict(self._counts)

    # -- 持久化挂载 --------------------------------------------------------

    def attach_log(self, task_id: str, log: EventLog) -> EventLog:
        with self._lock:
            self._logs[task_id] = log
        return log

    def detach_log(self, task_id: str) -> None:
        with self._lock:
            log = self._logs.pop(task_id, None)
        if log is not None:
            log.close()

    def flush_log(self, task_id: str) -> None:
        """提交点调用：日志也一起落盘。丢日志可以，但「提交了却没写」会让人怀疑数据。"""
        with self._lock:
            log = self._logs.get(task_id)
        if log is not None:
            log.flush()

    # -- 订阅 --------------------------------------------------------------

    def subscribe(self, task_id: str = "", *, min_level: str = "info",
                  maxsize: int | None = None) -> Subscription:
        sub = Subscription(self, task_id, min_level=min_level,
                           maxsize=maxsize or self.maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # -- 发事件 ------------------------------------------------------------

    def emit(self, task_id: str, kind: str, message: str, *, level: str = "info",
             data: Mapping[str, Any] | None = None, count: bool = True) -> Event | None:
        seq = next(self._seq)
        self._issued = seq
        event = Event(
            seq=seq, task_id=task_id, kind=kind, level=level,
            message=message, data=dict(data or {}), ts=now_iso(),
        )
        return self.publish(event, count=count)

    def log(self, task_id: str, message: str, *, level: str = "info",
            data: Mapping[str, Any] | None = None) -> Event | None:
        return self.emit(task_id, KIND_LOG, message, level=level, data=data)

    def publish(self, event: Event, *, count: bool = True) -> Event | None:
        with self._lock:
            self._ring.append(event)
            subs = [item for item in self._subs if not item.closed and item.matches(event)]
            log = self._logs.get(event.task_id)
            if count:
                self._counts[event.kind] = self._counts.get(event.kind, 0) + 1
        for sub in subs:
            sub.deliver(event)
        if log is not None:
            log.append(event)
        return event

    # -- 进度（节流合并） --------------------------------------------------

    def progress(self, task_id: str, payload: Mapping[str, Any], *, message: str = "",
                 force: bool = False, level: str = "info") -> Event | None:
        """进度事件节流到 2/s。**合并**而不是丢弃：被节流掉的那次会留在 `pending` 里，
        下一次放行时带上最新的值。若只丢弃，任务结束时最后几次进度就永远发不出去。"""
        now = time.monotonic()
        merged = dict(payload)
        if message:
            merged["message"] = message
        with self._lock:
            pending = self._progress_pending.setdefault(task_id, {})
            pending.update(merged)
            last = self._progress_at.get(task_id, 0.0)
            if not force and now - last < PROGRESS_INTERVAL:
                return None
            self._progress_at[task_id] = now
            body = dict(pending)
            pending.clear()
        return self.emit(task_id, KIND_PROGRESS, str(body.pop("message", "")), data=body,
                         level=level, count=False)

    def flush_progress(self, task_id: str) -> Event | None:
        """把最后攒着的那次进度发出去（任务收尾时调用）。"""
        return self.progress(task_id, {}, force=True)

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._progress_at.pop(task_id, None)
            self._progress_pending.pop(task_id, None)

    # -- 断线重放 ----------------------------------------------------------

    def replay(self, after: int, task_id: str = "", *, limit: int = 500,
               min_level: str = "info") -> tuple[list[Event], int]:
        """`Last-Event-ID` 之后的事件。返回 `(事件, 漏掉的条数)`。

        `after` 落在环形缓冲之外（断得太久，缓冲已经绕过去了）时返回 gap > 0，调用方据此
        先发一条 `resync`，让前端去 REST 补历史而不是以为「没有新事件」。
        """
        with self._lock:
            items = list(self._ring)
            oldest = items[0].seq if items else 0
        if not items:
            return [], 0
        if after and after < oldest - 1:
            gap = oldest - 1 - after
            start = 0
        else:
            gap = 0
            start = None
            for position, event in enumerate(items):
                if event.seq > after:
                    start = position
                    break
            if start is None:
                return [], 0
        picked = [
            event for event in items[start:]
            if (not task_id or not event.task_id or event.task_id == task_id)
            and level_rank(event.level) >= level_rank("info" if not task_id else min_level)
        ][:limit]
        return picked, gap

    # -- 收尾 --------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs)
            logs = list(self._logs.values())
            self._subs.clear()
            self._logs.clear()
        for sub in subs:
            sub._closed = True
        for log in logs:
            log.close()

    def __enter__(self) -> "EventBus":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def write_sse(event: Event) -> dict[str, str]:
    """薄封装，免得每个调用点都要记住 `.sse()`。"""
    return event.sse()


def filter_events(events: Iterable[Event], *, min_level: str = "info") -> Iterator[Event]:
    floor = level_rank(min_level)
    return (event for event in events if level_rank(event.level) >= floor)
