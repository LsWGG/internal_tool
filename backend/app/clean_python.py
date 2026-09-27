"""用户 Python 函数的父进程侧：管一个热复用的子进程，给它超时、内存上限和重启预算。

子进程侧在 `clean_worker.py`（协议与安全边界的说明都在那边）。

# 为什么要热复用

冷启动一个解释器约 200–400ms。若每块数据都重启，100 万行 / 2000 行一块 = 500 次启动
≈ 2.5 分钟纯开销、零计算 —— 这就是热复用的全部理由，也是**绝不能用 `communicate()`**
的原因：它会回收子进程，热复用直接失效。

# 为什么是读取线程而不是 `select`

协议是「一问一答」，但父进程可能在写一个很大的批次（几 MB），此时子进程正往 stdout 写
上一帧 —— 双方都可能在写，谁都不肯先读，管道写满就死锁。一个**专用读取线程**把帧收进
队列，既解耦了读写，又是那道死锁护栏。调用方只做 `queue.get(timeout=剩余时间)`。

# 线程模型

**一个工作线程一个 `PythonWorker`**（`PythonPool` 负责按线程发放）。所以 `PythonWorker`
不是线程安全的，`call_*` 会检查调用线程并在跨线程调用时直接报错 —— 静默串行化会让
「为什么变慢了」变成一个很难查的问题。
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clean_models import PythonFunction, PythonOptions
from .clean_worker import PROTOCOL_VERSION, dumps, loads

WORKER_SCRIPT = Path(__file__).with_name("clean_worker.py")

STDERR_TAIL = 20
"""为报错保留的 stderr 尾部行数。用户 `print()` 出来的调试信息就在这里，
失败时的可诊断性基本全靠它。"""


class PythonRunnerError(RuntimeError):
    """父进程侧的用法错误（跨线程调用、传了没声明的函数……），不是用户代码的错。"""


@dataclass
class RowError:
    row: int
    error: str


@dataclass
class CallResult:
    """一次调用的结果。

    `failure` 为空表示调用成功 —— 注意「成功」不等于「每行都对」：单行异常记在
    `errors` 里、那一行结果置空，其余行照常返回。
    """

    rows: list[dict[str, Any] | None] | None = None
    columns: dict[str, list[Any]] | None = None
    errors: list[RowError] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    failure: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure

    def error_pairs(self) -> list[tuple[int, str]]:
        return [(item.row, item.error) for item in self.errors]


# =========================================================================== worker


class PythonWorker:
    """一个子进程 + 一条管道。**不要跨线程共享**（见模块 docstring）。"""

    def __init__(
        self,
        functions: Sequence[PythonFunction],
        options: PythonOptions | None = None,
        *,
        seed: int = 0,
        pool: "PythonPool | None" = None,
        on_stderr: Callable[[str], None] | None = None,
        python_executable: str | None = None,
        env: Mapping[str, str] | None = None,
    ):
        self.functions = list(functions)
        self.options = options or PythonOptions()
        self.seed = seed
        self._pool = pool
        self._on_stderr = on_stderr
        self._executable = python_executable or sys.executable
        self._env = dict(env) if env is not None else _child_env()
        self._proc: subprocess.Popen | None = None
        self._frames: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._tail: deque[str] = deque(maxlen=STDERR_TAIL)
        self._lock = threading.Lock()
        self._id = 0
        self._owner: int | None = None
        self._inited = False
        self._closed = False
        self._dead_reason = ""
        self._suppressed = 0
        self.stats = {
            "starts": 0, "restarts": 0, "calls": 0, "timeouts": 0,
            "stale_frames": 0, "row_errors": 0, "stderr_lines": 0,
        }

    # -- 生命周期 ----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def disabled(self) -> bool:
        return self._pool.disabled if self._pool is not None else self._dead_reason == "disabled"

    def start(self) -> CallResult | None:
        """起进程并下发函数。返回 None 表示就绪，否则是失败结果。"""
        self._stop_process("重启")
        self._frames = queue.Queue()
        self._tail.clear()
        self._inited = False
        self._dead_reason = ""
        # -u 不缓冲（协议帧要立刻出去）；-E 忽略 PYTHON* 环境变量（父进程的 PYTHONPATH
        # 不该改变子进程能 import 到什么）。不加 -S：那会连 venv 的 site-packages 一起
        # 关掉，orjson 就没了。
        command = [self._executable, "-u", "-E", str(WORKER_SCRIPT)]
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - 路径与参数都是常量
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # 新会话：子进程成为新进程组的组长，于是 killpg 能连它 fork 出的
                # 孙子进程一起收掉（对齐 docker_manager 的做法）
                start_new_session=True,
                env=self._env,
                cwd=str(WORKER_SCRIPT.parent),
                bufsize=0,
            )
        except OSError as exc:
            self._dead_reason = "start_failed"
            return CallResult(failure="start_failed", detail=f"无法启动子进程：{exc}")
        self.stats["starts"] += 1
        self._start_readers()
        frame = self._exchange(
            {
                "op": "init",
                "protocol": PROTOCOL_VERSION,
                "seed": self.seed,
                "memory_mb": self.options.memory_mb,
                "functions": [_function_spec(fn) for fn in self.functions],
            },
            self.options.start_timeout_s,
        )
        if frame is None:
            return CallResult(
                failure="protocol_error",
                detail=f"初始化超时或子进程未应答{self._tail_text()}",
            )
        if not frame.get("ok"):
            kind = str(frame.get("kind") or "init_error")
            self._dead_reason = kind
            # 函数装载失败是配置问题，重试一万次也是同一份源码：直接禁用整池
            if self._pool is not None:
                self._pool.disable(f"用户函数装载失败：{frame.get('error')}")
            self._stop_process("装载失败")
            return CallResult(failure=kind, detail=str(frame.get("error") or ""))
        notice = frame.get("notice")
        if notice:
            self._emit("warn", str(notice))
        self._inited = True
        return None

    def close(self) -> None:
        self._closed = True
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()      # 子进程读到 EOF 会自己退出
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill_process(proc)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()
        self._inited = False

    def __del__(self):  # pragma: no cover - 兜底，正常路径由 close() 覆盖
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            self._kill_process(proc)

    # -- 调用 --------------------------------------------------------------

    def call_rows(
        self,
        name: str,
        fields: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        timeout: float | None = None,
    ) -> CallResult:
        return self._call(
            {"op": "call", "fn": name, "fields": list(fields),
             "rows": [list(row) for row in rows]},
            timeout,
        )

    def call_columns(
        self,
        name: str,
        columns: Mapping[str, Sequence[Any]],
        *,
        timeout: float | None = None,
    ) -> CallResult:
        return self._call(
            {"op": "call", "fn": name, "columns": {k: list(v) for k, v in columns.items()}},
            timeout,
        )

    def _call(self, request: dict, timeout: float | None) -> CallResult:
        with self._lock:
            self._check_thread()
            if self.disabled:
                return CallResult(failure="disabled", detail="用户函数已被禁用")
            ready = self._ensure()
            if ready is not None:
                return ready
            frame = self._exchange(
                request, timeout if timeout is not None else self.options.timeout_default_s
            )
        return self._parse(frame, bool(request.get("columns") is not None))

    def _ensure(self) -> CallResult | None:
        """确保子进程就绪。返回 None 表示可用，否则是失败结果。"""
        if self._closed:
            # 关掉之后再调用会静默起一个新子进程 —— 那是个泄漏，不是功能
            return CallResult(failure="closed", detail="该 worker 已关闭")
        if self.alive and self._inited:
            return None
        if self._dead_reason == "disabled":
            return CallResult(failure="disabled", detail="用户函数已被禁用")
        if self._proc is not None and not self.alive and not self._dead_reason:
            # 被外面杀掉（OOM killer、段错误）时我们自己没记过原因；退出码是唯一线索，
            # 留个「未知原因」会让人以为是本工具的 bug
            self._dead_reason = f"子进程已退出（退出码 {self._proc.returncode}）"
        if self._proc is not None or self.stats["starts"]:
            # 走到这里说明上一轮死了：先问池子还有没有重启预算
            reason = self._dead_reason or "原因未知"
            if self._pool is not None and not self._pool.note_restart(reason):
                return CallResult(failure="disabled", detail="重启次数已达上限")
            self.stats["restarts"] += 1
            self._emit("warn", f"用户函数子进程已重启（{reason}）")
        return self.start()

    def _exchange(self, request: dict, timeout: float | None) -> dict | None:
        """发一帧、等一帧。超时则杀掉子进程并返回 None。"""
        self._id += 1
        frame_id = self._id
        request = dict(request, id=frame_id)
        if timeout is None or timeout <= 0:
            timeout = self.options.timeout_default_s
        self._drain()          # 上一轮被杀后残留的帧在这里清掉，队列不会越积越长
        try:
            self._send(request)
        except OSError as exc:
            self._dead_reason = "broken_pipe"
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stats["timeouts"] += 1
                self._dead_reason = "timeout"
                self._emit(
                    "error",
                    f"用户函数执行超过 {timeout:g} 秒，子进程已被强制结束"
                    f"（函数：{request.get('fn')}）",
                )
                self._stop_process("超时")
                return None
            try:
                frame = self._frames.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if frame is None:
                self._dead_reason = "exited"
                return None
            if frame.get("id") != frame_id:
                self.stats["stale_frames"] += 1
                continue
            return frame

    def _send(self, frame: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise BrokenPipeError("子进程不在")
        proc.stdin.write(dumps(frame))
        proc.stdin.write(b"\n")
        proc.stdin.flush()

    def _parse(self, frame: dict | None, column_mode: bool) -> CallResult:
        self.stats["calls"] += 1
        if frame is None:
            return CallResult(
                failure=self._dead_reason or "protocol_error",
                detail=f"子进程未返回结果{self._tail_text()}",
            )
        if not frame.get("ok"):
            kind = str(frame.get("kind") or "runtime_error")
            detail = str(frame.get("error") or "")
            if kind == "memory_error":
                # 撞过内存上限的进程，分配器状态未可知，下一批不一定会更好 —— 直接换一个
                self._dead_reason = "memory_error"
                self._stop_process("内存超限")
            # 我们自己出问题（协议错、框架抛异常）时，用户 `print()` 的调试输出就是唯一线索
            if kind in ("protocol_error", "runtime_error"):
                detail += self._tail_text()
            return CallResult(failure=kind, detail=detail)
        errors = [RowError(int(e["row"]), str(e["error"])) for e in frame.get("errors") or []]
        self.stats["row_errors"] += len(errors)
        result = CallResult(
            errors=errors,
            notes=[str(n) for n in frame.get("notes") or []],
        )
        if column_mode:
            result.columns = dict(frame.get("columns") or {})
        else:
            result.rows = list(frame.get("rows") or [])
        return result

    # -- 子进程管理 --------------------------------------------------------

    def _start_readers(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for target in (self._read_frames, self._read_stderr):
            thread = threading.Thread(target=target, args=(proc,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _read_frames(self, proc) -> None:
        stream = proc.stdout
        if stream is None:
            self._frames.put(None)
            return
        while True:
            try:
                line = stream.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            try:
                self._frames.put(loads(line))
            except Exception as exc:
                self._tail.append(f"回帧无法解析：{exc}")
        self._frames.put(None)      # EOF 哨兵：调用方据此判定「子进程没了」

    def _read_stderr(self, proc) -> None:
        stream = proc.stderr
        if stream is None:
            return
        limit = self.options.stderr_per_minute
        window = time.monotonic()
        emitted = 0
        while True:
            try:
                line = stream.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self.stats["stderr_lines"] += 1
            self._tail.append(text)
            if self._on_stderr is not None:
                self._on_stderr(text)
            now = time.monotonic()
            if now - window >= 60.0:
                if self._suppressed:
                    self._emit("warn", f"用户函数另有 {self._suppressed} 行 stderr 输出被省略")
                    self._suppressed = 0
                window, emitted = now, 0
            if emitted < limit:
                emitted += 1
                self._emit("info", f"[用户函数] {text}")
            else:
                self._suppressed += 1

    def _stop_process(self, reason: str) -> None:
        proc, self._proc = self._proc, None
        self._inited = False
        if proc is None:
            return
        if proc.poll() is None:
            self._kill_process(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads.clear()
        if reason != "重启":
            self._emit("warn", f"用户函数子进程已停止（{reason}）")

    def _kill_process(self, proc) -> None:
        """杀掉整个进程组。孙进程（用户函数 fork 出来的东西）也一起走。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL 后极罕见
            pass

    # -- 杂项 --------------------------------------------------------------

    def _check_thread(self) -> None:
        current = threading.get_ident()
        if self._owner is None:
            self._owner = current
        elif self._owner != current:
            raise PythonRunnerError(
                "PythonWorker 不能跨线程使用：每个工作线程要有自己的子进程"
                "（用 PythonPool.worker() 取本线程的那个）"
            )

    def _drain(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return

    def _tail_text(self, limit: int = 5) -> str:
        if not self._tail:
            return ""
        lines = list(self._tail)[-limit:]
        return "；子进程输出：" + " / ".join(lines)

    def _emit(self, level: str, message: str) -> None:
        if self._pool is not None:
            self._pool.emit(level, message)


# =========================================================================== pool


class PythonPool:
    """任务级的子进程池：按线程发放 worker，管重启预算与「整体禁用」。

    禁用是**任务级**的：一个函数块块超时，如果不整体禁用，每个文件都会再付 5 次重启的
    代价（1000 个文件 = 5000 次启动）。禁用后行原样通过，报告里记一条 error。
    """

    def __init__(
        self,
        functions: Sequence[PythonFunction],
        options: PythonOptions | None = None,
        *,
        seed: int = 0,
        on_event: Callable[[str, str], None] | None = None,
        python_executable: str | None = None,
    ):
        self.functions = list(functions)
        self.options = options or PythonOptions()
        self.seed = seed
        self._on_event = on_event
        self._executable = python_executable
        self._local = threading.local()
        self._workers: list[PythonWorker] = []
        self._lock = threading.Lock()
        self._restarts = 0
        self._disabled = ""
        self._broken = False

    @property
    def disabled(self) -> bool:
        return bool(self._disabled)

    @property
    def reason(self) -> str:
        return self._disabled

    @property
    def restarts(self) -> int:
        return self._restarts

    def worker(self) -> PythonWorker:
        """取本线程的 worker（没有就建一个，但**不**启动子进程 —— 首次调用时才起）。"""
        existing = getattr(self._local, "worker", None)
        if existing is not None:
            return existing
        worker = PythonWorker(
            self.functions,
            self.options,
            seed=self.seed,
            pool=self,
            python_executable=self._executable,
        )
        self._local.worker = worker
        with self._lock:
            self._workers.append(worker)
        return worker

    def note_restart(self, reason: str) -> bool:
        """登记一次重启。返回 False 表示预算用尽、本池就此禁用。"""
        with self._lock:
            if self._disabled:
                return False
            self._restarts += 1
            if self._restarts > self.options.max_restarts:
                self._disabled = (
                    f"用户函数子进程重启次数已达上限（{self.options.max_restarts} 次），"
                    f"本次任务后续的 Python 函数全部跳过，数据原样通过。最后一次原因：{reason}"
                )
                self._broken = True
                self.emit("error", self._disabled)
                return False
        self.emit("warn", f"重启用户函数子进程（第 {self._restarts} 次）：{reason}")
        return True

    def disable(self, reason: str) -> None:
        with self._lock:
            if self._disabled:
                return
            self._disabled = reason
        self.emit("error", f"用户函数已禁用：{reason}")

    def emit(self, level: str, message: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(level, message)
            except Exception:      # 日志回调不该把任务拖垮
                pass

    def close(self) -> None:
        with self._lock:
            workers, self._workers = self._workers, []
        for worker in workers:
            try:
                worker.close()
            except Exception:
                pass

    def __enter__(self) -> "PythonPool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# =========================================================================== 辅助


def _function_spec(fn: PythonFunction) -> dict[str, Any]:
    return {
        "name": fn.name,
        "source": fn.source,
        "entry": fn.entry,
        "mode": fn.mode,
        "input_fields": list(fn.input_fields),
        "output_fields": list(fn.output_fields),
    }


def _child_env() -> dict[str, str]:
    """子进程的环境变量：只留跑解释器必需的。

    用户的机器上可能有一堆 `*_API_KEY`（本仓库自己就有 `AI_NATIVE_LLM_*`），
    用户函数没有任何正当理由读到它们 —— 顺手传下去等于给日志里的密钥泄露多开一扇门。
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "PYTHONHASHSEED")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def describe(functions: Iterable[PythonFunction]) -> str:
    """给日志用的一句话摘要。"""
    items = list(functions)
    if not items:
        return "无"
    return "、".join(f"{fn.name}({fn.phase}/{fn.mode})" for fn in items)
