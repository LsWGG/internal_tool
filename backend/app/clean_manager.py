"""清洗任务的服务层：任务记录、目录布局、上传、引擎驱动、事件总线、模型端点池。

这一层**不碰数据**（那是 `clean_engine` 的事），它负责一个任务的生老病死：

- 任务记录（`tasks.json`）：每次变更原子落盘，形状与仓库其它工具一致，好让共享的任务
  列表组件直接复用。
- 引擎跑在工作线程里；进度既更新任务记录（节流），也推给 SSE 总线（见 `clean_events`）。
- `interrupted` 状态与「续跑 / 重跑 / 只下载」的判定（复用 `clean_state.resolve_resume`）。
- 数据来源两种模式的归一化：上传目录 与 服务器路径（后者带根目录白名单）。
- 干跑（`/validate`）与预估（`/estimate`）：跑的是**同一条流水线**，只是截断行数。
- 全局模型端点池（密钥只落 `llm_endpoints.json`，权限 0600）。

# 目录布局

```
<root>/
  tasks.json                    任务记录（脱敏；密钥永不进这里）
  llm_endpoints.json            模型端点池，0600（唯一存密钥的地方）
  uploads/<upload_id>/...       上传目录（含 upload.json 清单）
  tasks/<task_id>/              StateStore 的 dir：state.json / report.md / output/ / logs/
  previews/<preview_id>/        干跑的临时状态目录，跑完即删
  <task_id>.zip                 下载用的压缩包，按需重建
```

# 并发

**任务是并行的，文件是串行的**。文件串行是引擎的设计（一条全局追加索引序列 + 只有一个
半截文件能被重建，见 `clean_engine` 模块 docstring 第 4 条）—— 换来的是「续跑后的产物与
不中断运行逐字节相同」这个可以证明的性质。真正的并行在别处：numpy/RE2 释放 GIL、用户
Python 跑在隔离子进程、大模型是网络 I/O。所以这里的线程池只负责**任务之间**的并行，默认
两个：再多的任务是互相抢磁盘，而不是跑得更快。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clean_engine import (
    CleanEngine,
    CleanEngineError,
    ResumeRefused,
    collect_inputs,
    report_path,
)
from .clean_events import KIND_ERROR, EventBus, EventLog
from .clean_format import format_source, ruff_command
from .clean_io import CleanIoError, append_target, file_kind, inspect_file, precount
from .clean_llm import (
    MAX_FAILOVER_ENDPOINTS,
    SENTINEL,
    CallBudget,
    LlmClient,
    LlmEndpoint,
    LlmPool,
    estimate_llm,
    load_pool,
    merge_endpoint,
    save_pool,
)
from .clean_models import CleanSourceConfig, CleanTaskConfig, InputOptions
from .clean_python import PythonWorker
from .clean_report import report_filename
from .clean_stats import Counters, StatsSet
from .clean_state import (
    StateError,
    StateStore,
    ensure_private_dir,
    resolve_resume,
)
from .clean_worker import inspect_source
from .task_manager import TaskControl

LOGGER = logging.getLogger(__name__)

TASKS_FILE = "tasks.json"
POOL_FILE = "llm_endpoints.json"
UPLOAD_MANIFEST = "upload.json"
TASK_DIRNAME = "tasks"
UPLOAD_DIRNAME = "uploads"
PREVIEW_DIRNAME = "previews"

INPUT_EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".jsonl", ".ndjson", ".parquet")
ARCHIVE_EXTENSIONS = (".zip",)

# 上传**不设大小上限**：这个工具的目标输入就是 GB 级甚至更大的数据目录，一个拍脑袋的
# 字节数（曾经是单文件 4GB / 合计 8GB）唯一的效果是把正常输入挡在门外。真正的上界是
# 磁盘的物理剩余空间，由 `ensure_disk_space` 在写之前查（见下）—— 那比常量准，也把
# 别的上传与别的进程一起算进去了。
#
# 个数上限保留：它不是大小护栏，是别让一个疯跑的压缩包把 inode 耗光（20000 个空文件只
# 占几十 MB，却能造出 20000 个目录项）。
MAX_UPLOAD_FILES = 20000
MAX_ZIP_ENTRIES = 20000

# 磁盘剩余空间低于这个数就不再写盘。**不是上限**，是「别再写了」的水位线：留一点余量给
# 引擎自己落盘（状态、日志、产物），否则上传会把盘占到最后一字节，任务跑起来立刻失败。
DISK_FREE_FLOOR = 512 * 1024**2

# 任务记录里进度的写入节流（SSE 每条都推，任务记录只是刷新页面时的那一份）
RECORD_INTERVAL = 5.0
# 干跑默认与上限
DRY_RUN_ROWS = 200
DRY_RUN_MAX_ROWS = 5000
# 干跑预览返回几行数据。与试跑行数是两回事：试跑可以跑 5000 行看统计，但界面上的
# 「清洗前 VS 清洗后」两栏一屏就那么多，多传只是白占内存与带宽。
DRY_PREVIEW_ROWS = 200
# 表头映射那一步的实时预览要几行：默认与上限。与上面同一套道理 —— 这是给人眼看的一屏，
# 上限防的是「客户端要 100 万行」这种把服务端内存当带宽用的请求。
INSPECT_ROWS = 20
INSPECT_MAX_ROWS = 200
# 上传目录保留时间：任务引用它，所以不能随任务删除而删（可能有多个任务共用）
UPLOAD_TTL = 7 * 86400
PREVIEW_TTL = 3600

# 「试跑这个函数」的样本行数：默认与上限。默认 20 是因为面板要逐行贴进/出，20 行正好
# 一屏；上限走干跑那套（同一份数据不该有两套上限）。
FUNCTION_TEST_ROWS = 20
FUNCTION_TEST_MAX_ROWS = DRY_RUN_MAX_ROWS
# 调试面板里 `print()` 输出的行数上限。函数里一个死循环 print 能刷出几十万行，
# 超过就报「另有 N 行省略」——省略要**说出来**，静默截断会让人以为函数没打印。
PRINTED_LIMIT = 200
# 子进程的 stderr 与结果帧走**两条管道**，谁先到没有保证：最后一行 `print` 可能比结果帧
# 晚几毫秒。回包前等到「安静」这么久（上限 STDERR_DRAIN_MAX_S），否则面板会漏掉用户
# 刚打印的那几行 —— 一个偶尔少一行的调试面板比一个稳定慢 50ms 的更难用。
STDERR_QUIET_S = 0.05
STDERR_DRAIN_MAX_S = 0.3

STATUS_TEXT = {
    "queued": "等待执行",
    "running": "处理中",
    "paused": "已暂停",
    "interrupted": "已中断，可继续",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

# 「不会再往前走了」的那几个状态。判断「产物为空是因为还没跑到 vs 确实没有行」靠它 ——
# 用写反的集合去猜，界面就会对着一个已经结束的任务说「正在处理中」。
TERMINAL_STATUS = frozenset({"completed", "failed", "cancelled", "interrupted"})

# 文件级状态（`FileState`）：与任务级状态刻意不同词，别混用
FILE_STATUS_TEXT = {
    "pending": "未开始",
    "running": "处理中",
    "done": "已完成",
    "failed": "失败",
    "skipped": "已跳过",
}


class CleanError(ValueError):
    """用户可修的输入问题 → 400。"""


class CleanNotFound(LookupError):
    """找不到对象 → 404。"""


class CleanConflict(RuntimeError):
    """当前状态做不了这件事 → 409。"""


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, value: Any, *, private: bool = False) -> None:
    """原子写 JSON。`private=True` 时权限 0600（端点池用它）。"""
    path = Path(path)
    if private:
        ensure_private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
    os.replace(tmp, path)


def safe_relpath(raw: str) -> str:
    """上传时浏览器给的相对路径 → 安全的相对路径。挡绝对路径、`..`、Windows 盘符。"""
    text = str(raw or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise CleanError(f"文件路径不安全：{raw or '未命名文件'}")
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise CleanError(f"文件路径不安全：{raw}")
    return "/".join(parts)


def free_bytes(path: str | Path) -> int:
    """路径所在文件系统的剩余空间。路径还不存在（上传目录刚建）就往上一级找。

    单独抽出来是为了**能被测试替换**：真把盘写满来测这条分支是不可能的，`patch.object`
    这个函数才是这条护栏唯一可测的入口。
    """
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        # 问不到剩余空间时**不要**当作「满了」把上传全拒掉（有些网络文件系统就是答不了）；
        # 真写不下的时候，写那一步自己会报错。
        return DISK_FREE_FLOOR


def ensure_disk_space(path: str | Path, need_bytes: int = 0) -> None:
    """写盘前的磁盘兜底：这一笔写下去会跌破水位线就立刻停。

    替代了原来的大小上限（4GB/8GB）。区别在于它不是拍脑袋的数字，而是此刻真实的剩余
    空间 —— 盘上有别的上传、别的东西占着，这里都算得进去。
    """
    free = free_bytes(path)
    if free - max(0, int(need_bytes)) < DISK_FREE_FLOOR:
        raise CleanError(f"数据盘剩余空间不足（剩 {free / 1024**2:.0f} MB），已停止写入")


class CleanControl(TaskControl):
    """`TaskControl` + 状态回拨。

    「暂停」有两层含义：用户点了按钮（`请求中`）与引擎真的停在了检查点（`已暂停`）。
    引擎在 `checkpoint()` 里阻塞等待，没有机会自己报告后者，所以由这个子类在**进入等待
    之前**把状态翻过去 —— 界面于是不会永远显示「正在暂停」。
    """

    def __init__(self, manager: "CleanManager", task_id: str):
        super().__init__()
        self._manager = manager
        self._task_id = task_id
        self._reported = False

    def checkpoint(self) -> bool:
        if self.paused and not self.cancelled and not self._reported:
            self._reported = True
            self._manager._update(self._task_id, status="paused", message="已暂停",
                                  paused_at=_iso())
        return super().checkpoint()

    def resume(self) -> None:
        self._reported = False
        super().resume()


class CleanManager:
    def __init__(
        self,
        root: str | Path,
        *,
        python_executable: str | None = None,
        auto_resume: bool = False,
        allowed_roots: Sequence[str] | None = None,
        max_tasks: int = 2,
        clock=time.time,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_dir = self.root / TASK_DIRNAME
        self.uploads_dir = self.root / UPLOAD_DIRNAME
        self.preview_dir = self.root / PREVIEW_DIRNAME
        for folder in (self.tasks_dir, self.uploads_dir, self.preview_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.tasks_file = self.root / TASKS_FILE
        self.pool_file = self.root / POOL_FILE
        ensure_private_dir(self.root)
        self.python_executable = python_executable
        self._allowed_roots = [Path(item).expanduser().resolve() for item in (allowed_roots or [])]
        self._clock = clock
        self.lock = threading.RLock()
        self.bus = EventBus()
        self.tasks: dict[str, dict[str, Any]] = self._load_tasks()
        self.controls: dict[str, CleanControl] = {}
        self._record_at: dict[str, float] = {}
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(max_tasks)),
                                       thread_name_prefix="clean-task")
        self._closed = False
        self._cleanup_scratch()
        interrupted = self._mark_interrupted()
        if auto_resume:
            for task_id in interrupted:
                self._submit_resume(task_id)

    # ==================================================================== 记录

    def _load_tasks(self) -> dict[str, dict[str, Any]]:
        raw = _read_json(self.tasks_file, {})
        if not isinstance(raw, dict):
            return {}
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}

    def _save(self) -> None:
        _write_json(self.tasks_file, self.tasks)

    def _mark_interrupted(self) -> list[str]:
        """启动时把「上次没跑完」的任务标成 `interrupted`（**不是 failed**）。

        与仓库其它工具不同：这里的状态文件与产物是保留的，所以「中断」与「失败」必须分开
        —— 失败的语义是「重来一遍」，中断的语义是「接着跑」。用 failed 会让用户以为前面
        跑的那些小时白费了。
        """
        interrupted: list[str] = []
        changed = False
        with self.lock:
            for task_id, task in self.tasks.items():
                if task.get("status") in ("queued", "running", "paused"):
                    task.update(status="interrupted", message=STATUS_TEXT["interrupted"],
                                updated_at=_iso())
                    interrupted.append(task_id)
                    changed = True
            if changed:
                self._save()
        for task_id in interrupted:
            self.bus.emit(task_id, "status", STATUS_TEXT["interrupted"],
                          data={"status": "interrupted"})
        return interrupted

    def _cleanup_scratch(self) -> None:
        """清掉过期的工作目录：干跑目录与没人引用的上传目录。

        上传目录有引用计数问题（一个上传可以被多个任务用），所以按时间清：
        `upload.json` 的创建时间超过 `UPLOAD_TTL` 且没有活跃任务引用它，才删。
        """
        now = self._clock()
        for folder in self.preview_dir.glob("*"):
            try:
                if folder.is_dir() and now - folder.stat().st_mtime > PREVIEW_TTL:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue
        referenced = {
            str((task.get("request") or {}).get("source", {}).get("upload_id") or "")
            for task in self.tasks.values()
            if (task.get("request") or {}).get("source", {}).get("mode") == "upload"
        }
        for folder in self.uploads_dir.glob("*"):
            if folder.name in referenced or not folder.is_dir():
                continue
            try:
                if now - folder.stat().st_mtime > UPLOAD_TTL:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue

    # -- 查询 --------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            items = sorted(self.tasks.values(), key=lambda item: item.get("created_at", ""),
                           reverse=True)
            return [self.public_task(item) for item in items]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(str(task_id))
            return self.public_task(task) if task else None

    def _record(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.tasks.get(str(task_id))

    def public_task(self, task: Mapping[str, Any] | None) -> dict[str, Any]:
        """给界面的形状。**密钥不进这里** —— 任务配置里本来就没有密钥，端点池不进任务。"""
        if not task:
            return {}
        item = dict(task)
        item.setdefault("progress", 0)
        item.setdefault("error", "")
        item["status_text"] = STATUS_TEXT.get(str(item.get("status")), str(item.get("status")))
        item["resume"] = self.resume_decision(item)
        return item

    def _update(self, task_id: str, **values: Any) -> None:
        with self.lock:
            task = self.tasks.get(str(task_id))
            if task is None:
                return
            values["updated_at"] = _iso()
            task.update(values)
            self._save()

    def _update_throttled(self, task_id: str, **values: Any) -> None:
        """进度这种高频更新：按 `RECORD_INTERVAL` 节流落盘。SSE 不受影响（每条都推）。"""
        now = self._clock()
        if now - self._record_at.get(str(task_id), 0.0) < RECORD_INTERVAL:
            with self.lock:
                task = self.tasks.get(str(task_id))
                if task is not None:
                    task.update(values)
            return
        self._record_at[str(task_id)] = now
        self._update(task_id, **values)

    # ==================================================================== 配置

    def config(self, payload: Mapping[str, Any]) -> CleanTaskConfig:
        """把请求体变成配置，附上人能读懂的校验错误。"""
        body = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        try:
            return CleanTaskConfig.model_validate(body)
        except Exception as exc:
            raise CleanError(f"清洗配置有误：{_readable(exc)}") from exc

    def source_config(self, payload: Mapping[str, Any]) -> CleanSourceConfig:
        """只看来源与读入选项的那部分配置。

        探测必须能在一份**还没填好字段**的配置上跑：表头正是要靠探测才知道的东西，而
        `CleanTaskConfig` 要求至少一个目的字段。这里不放松主模型的校验，只投影出探测
        真正需要的那两个键。
        """
        body = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        try:
            return CleanSourceConfig.model_validate({
                "source": body.get("source"),
                "input": body.get("input") or {},
            })
        except Exception as exc:
            raise CleanError(f"清洗配置有误：{_readable(exc)}") from exc

    def _task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / str(task_id)

    def store(self, task_id: str) -> StateStore:
        task = self._record(task_id)
        durable = bool(((task or {}).get("request") or {}).get("output", {}).get("durable_commit"))
        return StateStore(self._task_dir(task_id), durable=durable)

    # ==================================================================== 数据来源

    def allowed_roots(self) -> list[Path]:
        """服务器路径模式的白名单。默认：本工具数据目录 + `$HOME`。"""
        roots = list(self._allowed_roots)
        if not roots:
            # CORS 已锁定到 dev server 源，所以这不是跨站可达的任意文件读；白名单是纵深
            # 防御 —— 将来有人把 allow_origins 改成 *，不至于顺带把任意文件读也暴露出去。
            roots = [self.root, Path.home()]
        return [item for item in roots if item.exists()] or [self.root]

    def check_allowed(self, raw: str, *, allow_file: bool = True) -> Path:
        """白名单 + `realpath` 解析（挡符号链接逃逸）+ 拒绝读自己的产出目录。"""
        text = str(raw or "").strip()
        if not text:
            raise CleanError("路径不能为空")
        if text.startswith("~"):
            text = str(Path(text).expanduser())
        path = Path(text).resolve()
        roots = self.allowed_roots()
        if not any(path == root or root in path.parents for root in roots):
            # 先报**被拒的路径**、再报允许的根：反过来写会被读成「被拒的就是这几个」，
            # 用户看着自己给的路径没出现在消息里，只能猜哪一段不合法。
            raise CleanError(
                f"不允许读取这个路径：{path}。允许的根目录："
                + "、".join(str(root) for root in roots)
                + "（可用环境变量 CLEAN_ALLOWED_ROOTS 追加，用 `:` 分隔）"
            )
        outputs = self.tasks_dir
        if path == outputs or outputs in path.parents:
            # 直接读自己的产物会把「清洗结果」当成新输入，一跑就是指数级的自我放大
            raise CleanError("不能把清洗服务的任务目录当作输入（会把自己的产物再清洗一遍）")
        if path.is_dir():
            return path
        if allow_file and path.is_file():
            return path
        raise CleanError(f"路径不存在：{path}")

    def inputs(self, config: CleanTaskConfig) -> tuple[list[tuple[str, str]], dict[str, Any]]:
        """把来源展开成 `(key, 绝对路径)`，并返回一份可展示的来源说明。"""
        source = config.source
        if source.mode == "upload":
            folder = self.uploads_dir / source.upload_id
            if not re.fullmatch(r"[0-9a-f]{32}", str(source.upload_id)) or not folder.is_dir():
                raise CleanError("上传的数据已不存在（可能已过期清理），请重新上传")
            roots = [folder]
            info = {"mode": "upload", "upload_id": source.upload_id, "roots": [str(folder)]}
        else:
            roots = [self.check_allowed(item) for item in source.paths]
            info = {"mode": "server_path", "roots": [str(item) for item in roots]}
        try:
            inputs = collect_inputs([str(item) for item in roots], config.input)
        except CleanIoError as exc:
            raise CleanError(str(exc)) from exc
        except OSError as exc:
            raise CleanError(f"读取输入目录失败：{exc}") from exc
        if not inputs:
            raise CleanError(
                "没有找到可处理的文件（检查后缀筛选与子目录开关；支持的格式："
                + "、".join(INPUT_EXTENSIONS)
                + "）"
            )
        return inputs, info

    # -- 上传 --------------------------------------------------------------

    def new_upload(self) -> tuple[str, Path]:
        upload_id = uuid.uuid4().hex
        folder = self.uploads_dir / upload_id
        folder.mkdir(parents=True)
        return upload_id, folder

    def save_upload(self, upload_id: str, entries: Iterable[tuple[str, Path]]) -> dict[str, Any]:
        """登记一批已经落盘的上传文件，必要时展开 ZIP。返回上传清单。"""
        folder = self.uploads_dir / str(upload_id)
        if not folder.is_dir():
            raise CleanNotFound("上传目录不存在")
        kept: list[dict[str, Any]] = []
        archives: list[Path] = []
        total = 0
        for raw, path in entries:
            relative = safe_relpath(raw)
            suffix = Path(relative).suffix.lower()
            if suffix in ARCHIVE_EXTENSIONS:
                archives.append(path)
                continue
            if suffix not in INPUT_EXTENSIONS:
                raise CleanError(f"不支持的文件类型：{relative}（支持 {'、'.join(INPUT_EXTENSIONS)}）")
            size = path.stat().st_size
            total += size
            kept.append({"path": relative, "size": size})
        for archive in archives:
            extracted = self._extract_zip(archive, folder)
            kept.extend(extracted)
            total += sum(item["size"] for item in extracted)
            archive.unlink(missing_ok=True)
        if not kept:
            raise CleanError("上传里没有可处理的文件")
        if len(kept) > MAX_UPLOAD_FILES:
            raise CleanError(f"文件太多（{len(kept)} 个，上限 {MAX_UPLOAD_FILES}）")
        manifest = {
            "id": str(upload_id),
            "created_at": _iso(),
            "bytes": total,
            "count": len(kept),
            "files": sorted(kept, key=lambda item: item["path"]),
        }
        _write_json(folder / UPLOAD_MANIFEST, manifest)
        return manifest

    def _extract_zip(self, archive: Path, folder: Path) -> list[dict[str, Any]]:
        """展开上传的 ZIP。安全检查与 `word_batch_engine.checked_zip` 一致（绝对路径、
        `..`、符号链接一律拒绝），大小**不设上限**：每写一个条目之前按 `item.file_size`
        问一次磁盘还剩多少（压缩包里每个条目的原始大小都在清单里，这一问是准的）。
        """
        extracted: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(archive) as package:
                infos = [item for item in package.infolist() if not item.is_dir()]
                if len(infos) > MAX_ZIP_ENTRIES:
                    raise CleanError(f"压缩包内文件太多（{len(infos)} 个，上限 {MAX_ZIP_ENTRIES}）")
                for item in infos:
                    name = item.filename.replace("\\", "/")
                    path = PurePosixPath(name)
                    if (path.is_absolute() or ".." in path.parts
                            or (item.external_attr >> 16) & 0o170000 == 0o120000):
                        raise CleanError(f"压缩包含不安全路径或符号链接：{name}")
                    if Path(name).suffix.lower() not in INPUT_EXTENSIONS:
                        continue
                    # 压缩炸弹在这里也拦得住：一个 4GB 的包能展开出 TB 级，写下去之前就报错
                    ensure_disk_space(folder, item.file_size)
                    target = folder / safe_relpath(name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item) as source, open(target, "wb") as sink:
                        shutil.copyfileobj(source, sink, 1024 * 1024)
                    extracted.append({"path": target.relative_to(folder).as_posix(),
                                      "size": target.stat().st_size})
        except zipfile.BadZipFile as exc:
            raise CleanError(f"不是有效的 ZIP：{archive.name}") from exc
        return extracted

    def upload_info(self, upload_id: str) -> dict[str, Any]:
        folder = self.uploads_dir / str(upload_id)
        manifest = _read_json(folder / UPLOAD_MANIFEST, None)
        if not isinstance(manifest, dict):
            raise CleanNotFound("上传不存在")
        return manifest

    def delete_upload(self, upload_id: str) -> bool:
        folder = self.uploads_dir / str(upload_id)
        if not folder.is_dir():
            return False
        with self.lock:
            busy = [
                task["id"] for task in self.tasks.values()
                if (task.get("request") or {}).get("source", {}).get("upload_id") == str(upload_id)
                and task.get("status") in ("queued", "running", "paused")
            ]
        if busy:
            raise CleanConflict("还有任务在使用这批上传数据")
        shutil.rmtree(folder, ignore_errors=True)
        return True

    def browse(self, raw: str = "") -> dict[str, Any]:
        """服务器路径模式的目录浏览。**只列目录与候选文件，不返回内容。**"""
        roots = self.allowed_roots()
        if raw:
            path = self.check_allowed(raw)
            if path.is_file():
                path = path.parent
        else:
            path = roots[0]
        dirs: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        try:
            entries = sorted(path.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            raise CleanError(f"目录读不了：{exc}") from exc
        for entry in entries[:2000]:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in INPUT_EXTENSIONS:
                    stat = entry.stat()
                    files.append({"name": entry.name, "path": str(entry), "size": stat.st_size,
                                  "kind": file_kind(entry, allow_unknown=True)})
            except OSError:
                continue
        parent = str(path.parent) if any(path != root and root in path.parents for root in roots) else ""
        return {
            "path": str(path),
            "parent": parent,
            "roots": [{"name": str(item), "path": str(item)} for item in roots],
            "dirs": dirs,
            "files": files,
        }

    # ==================================================================== 探测与预估

    def inspect(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """探测一个文件的编码/分隔符/表头/样本 —— 没有它用户无法配置表头映射。

        用的是 `source_config` 而不是 `config`：**表头要靠探测才知道**，所以这一步必然
        发生在「还没有任何目的字段」的时候。用完整配置去校验，界面就卡在「要先加字段才
        能探测表头、要先探测表头才能加字段」的死循环里。
        """
        config = self.source_config(payload)
        inputs, info = self.inputs(config)
        wanted = str(payload.get("file") or "").strip()
        key, path = inputs[0]
        if wanted:
            found = [item for item in inputs if item[0] == wanted]
            if not found:
                raise CleanNotFound(f"输入里没有这个文件：{wanted}")
            key, path = found[0]
        try:
            detection = inspect_file(path, config.input, sample_rows=_inspect_rows(payload.get("rows")))
        except CleanIoError as exc:
            raise CleanError(str(exc)) from exc
        result = detection.as_dict()
        result.update({"key": key, "file": Path(path).name, "size": Path(path).stat().st_size,
                       "source": info, "files": [_file_brief(item, path_) for item, path_ in inputs]})
        return result

    def estimate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """提交前的预估。**必须给区间**（见 `estimate_llm`），并且把护栏提示一起给出。"""
        config = self.config(payload)
        inputs, info = self.inputs(config)
        rows_total = 0
        exact = True
        per_file: list[dict[str, Any]] = []
        for key, path in inputs:
            count: int | None = None
            if config.input.precount:
                try:
                    count = precount(path, config.input)
                except (CleanIoError, OSError) as exc:
                    raise CleanError(f"无法统计 {key} 的行数：{exc}") from exc
            else:
                try:
                    count = inspect_file(path, config.input, sample_rows=0).row_estimate
                except (CleanIoError, OSError):
                    count = None
            if count is None:
                exact = False
                count = 0
            rows_total += count
            per_file.append({"key": key, "size": Path(path).stat().st_size, "rows": count})
        pool = self._build_pool(config)
        estimate = estimate_llm(config, pool, rows=rows_total)
        notes = list(estimate.notes)
        notes.extend(self._estimate_notes(config, rows_total, exact))
        if config.input.precount and exact:
            notes.append("行数为精确统计（precount 开启，多读了一遍文件）")
        elif not exact:
            notes.append("行数按文件大小外推，大文件可能有明显偏差；需要精确数字可开启「精确行数」")
        return {
            "rows": rows_total,
            "rows_exact": bool(exact and config.input.precount),
            "files": per_file,
            "source": info,
            "estimate": estimate.as_dict(),
            "notes": notes,
            "endpoints": self.endpoint_summary(config),
        }

    def _estimate_notes(self, config: CleanTaskConfig, rows: int, exact: bool) -> list[str]:
        notes: list[str] = []
        # 要的是**字段规格**（batch_size 在生成规则里），不是 `llm_fields()` 给的名字列表 ——
        # 拿名字当规格用会让 `/estimate` 直接 AttributeError：那正是「提交前必须显示预估」
        # 的那一次调用，一崩就等于把这个功能整个关掉。
        specs = [spec for spec in config.fields
                 if spec.generate and spec.generate.kind == "llm"]
        if not specs:
            return notes
        if not config.llm.sample_rows and rows > 20000:
            notes.append(
                f"有 {len(specs)} 个大模型字段但没限制生成行数：按 {rows} 行预估。"
                "GB 级数据建议设置「只对前 N 行生成」，其余行按回退策略置空"
            )
        batch = max(int(spec.generate.batch_size or 1) for spec in specs)
        if len(specs) > 1 and batch == 1:
            notes.append("多个大模型字段仍是一次一行：把「批量合并」调到 5–20 能省数倍时间与费用")
        return notes

    def endpoint_summary(self, config: CleanTaskConfig) -> dict[str, Any]:
        endpoints = [item for item in load_pool(self.pool_file)
                     if not config.llm.endpoint_ids or item.id in set(config.llm.endpoint_ids)]
        usable = [item for item in endpoints if item.enabled]
        return {
            "selected": [self._endpoint_brief(item) for item in endpoints],
            "usable": len(usable),
            "concurrency": sum(item.max_concurrency for item in usable),
            "missing": sorted(set(config.llm.endpoint_ids)
                              - {item.id for item in endpoints}),
        }

    @staticmethod
    def _endpoint_brief(endpoint: LlmEndpoint) -> dict[str, Any]:
        return {
            "id": endpoint.id,
            "name": endpoint.name or endpoint.model,
            "model": endpoint.model,
            "enabled": endpoint.enabled,
            "max_concurrency": endpoint.max_concurrency,
            "weight": endpoint.weight,
            "ewma_ms": round(float(endpoint.ewma_ms or 0.0), 1),
        }

    # ==================================================================== 干跑

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """创建期校验 + 用**完整流水线**跑前 N 行的干跑。

        跑的是同一个引擎、同一套规则，只是 `row_limit` 截断行数、`max_calls` 压低预算 ——
        所以它拦下的错误就是正式任务会不会犯的错误。跑在临时状态目录里，跑完即删：干跑
        的产物是半截的，绝不能留在任务目录里被当成真产物下载。
        """
        config = self.config(payload)
        inputs, info = self.inputs(config)
        rows = int(payload.get("rows") or DRY_RUN_ROWS)
        rows = max(1, min(rows, DRY_RUN_MAX_ROWS))
        target = inputs[0]
        dry = self._dry_config(config, rows)
        preview_id = uuid.uuid4().hex
        store = StateStore(self.preview_dir / preview_id)
        try:
            engine = CleanEngine(
                "dry-run",
                dry,
                store,
                llm_pool=self._build_pool(dry),
                # 固定种子：干跑要可重复，而且**要与正式任务跑出来的前 N 行一致** ——
                # 干跑展示的是「按这个配置，这些行会变成什么样」，用另一个种子展示的
                # 就是另一批随机值，于是用户确认过的预览与真正产出对不上。
                seed=0,
                python_executable=self.python_executable,
                row_limit=rows,
            )
            result = engine.run([target])
            snapshot = (result.state.progress if result.state else None) or {}
            files = [file_entry(item) for item in (result.state.ordered() if result.state else [])]
            counters = snapshot.get("counters") or {}
            return {
                "ok": result.status == "completed",
                "status": result.status,
                "error": result.error,
                "rows": rows,
                "seed": 0,
                "file": target[0],
                "source": info,
                "files": files,
                "violations": counters.get("violations") or {},
                "fallbacks": counters.get("fallbacks") or {},
                "after_violations": (snapshot.get("after_counters") or {}).get("violations") or {},
                "llm": (result.state.llm if result.state else None) or {},
                "notes": list((result.state.notes if result.state else None) or []),
                "report": result.report,
                # 真数据的两栏对照：干跑跑的就是完整流水线，产物就在这个临时 store 里，
                # 读它只是顺手的事（见 `_dry_preview`，必须在下面的 rmtree 之前）
                "preview": self._dry_preview(dry, store, target, rows),
            }
        finally:
            shutil.rmtree(store.dir, ignore_errors=True)

    def _dry_config(self, config: CleanTaskConfig, rows: int) -> CleanTaskConfig:
        """干跑用的配置副本：预算压低、提交点变密。

        预算必须压 —— 否则「校验一下配置」会把 `llm_max_calls`（默认 5 万次）全花掉。
        `commit_rows` 也调小：干跑只有几百行，窗口大于行数就等于一次都不提交，中途取消
        （用户可能随时点掉）会连产物都没有。
        """
        payload = config.model_dump()
        calls = int(config.llm.max_calls or 0) or 200
        payload["llm"] = {**payload["llm"], "max_calls": min(calls, max(20, rows * 2))}
        payload["output"] = {**payload["output"], "commit_rows": 64, "durable_commit": False}
        payload["name"] = f"{config.name or '清洗'}（试跑）"
        return CleanTaskConfig.model_validate(payload)

    # ============================================================== 用户函数（编辑器用）

    def check_function(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """静态检查一份源码：纯 AST、不起子进程，编辑器可以按 400ms 防抖来调。

        收的是**源码本身**，不是整份配置：波浪线要跟着打字出来，而那时候配置往往还没
        填完（没选数据来源、没勾字段）。用整份配置做前置的话，用户会在一条「配置不完整」
        的红字底下写代码 —— 而那条红字与被检查的这个函数毫无关系。
        """
        problems = inspect_source(
            str(payload.get("source") or ""), str(payload.get("entry") or "transform")
        )
        return {
            "ok": not any(item.severity == "error" for item in problems),
            "problems": [_problem(item) for item in problems],
        }

    def format_function(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """用 ruff 排版。**失败也是一种结果**（`ok:false` + 原因），不是 4xx/5xx ——
        与 `/llm/endpoints/test` 同一条理由：用户点这个按钮就是想知道「能不能格式化、
        为什么不能」，用状态码表达等于让界面去猜原因（而 ruff 的报错自带插入符图示，
        正是界面要原样显示的那段话）。
        """
        source = str(payload.get("source") or "")
        formatted, error = format_source(source, str(payload.get("name") or ""))
        if error:
            return {"ok": False, "source": "", "error": error, "changed": False}
        return {"ok": True, "source": formatted, "error": "", "changed": formatted != source}

    def test_function(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """拿**真实数据的头几行**试跑一个函数，返回逐行的进/出、报错与 `print()`。

        样本取自文件原值，而不是让用户手填：手填的样本永远是对的 —— 真实文件里的空串、
        超长数字、带前导零的编号、混进来的 `nan`，才是函数真正要面对的东西。

        三处与正式任务**故意不同**，界面上必须说清（见返回里的 `notes`）：样本只有前
        N 行、是文件原值（`phase="post"` 时正式任务看到的是清洗后的值）、以及新起一个
        子进程跑（跑的就是刚写的那份源码，而不是任务池里那份热着的旧代码）。
        """
        body = payload.get("config") if isinstance(payload.get("config"), Mapping) else payload
        functions = list(body.get("functions") or [])
        if not functions:
            raise CleanError("配置里还没有自定义函数")
        index = int(payload.get("index") or 0)
        if not 0 <= index < len(functions):
            raise CleanError(f"函数序号越界：{index}（配置里有 {len(functions)} 个函数）")
        # 只把被测的那一个放进配置：旁边那张刚加上的卡片还没勾写回字段，用整份配置校验
        # 的话，这个按钮会报一句与被测函数无关的错 —— 报错指错地方，比不报还糟。
        config = self.config(dict(body, functions=[functions[index]]))
        fn = config.functions[0]
        rows = max(1, min(int(payload.get("rows") or FUNCTION_TEST_ROWS),
                          FUNCTION_TEST_MAX_ROWS))

        problems = inspect_source(fn.source, fn.entry)
        out: dict[str, Any] = {
            "ok": False,
            "ran": False,
            "error": "",
            "problems": [_problem(item) for item in problems],
            "mode": fn.mode,
            "phase": fn.phase,
            "entry": fn.entry,
            "timeout_s": fn.timeout_s,
            "input_fields": list(fn.input_fields),
            "output_fields": list(fn.output_fields),
            "rows": [],
            "columns": [],
            "printed": [],
            "printed_omitted": 0,
            "notes": [],
            "seconds": 0.0,
            "failure": "",
            "detail": "",
            "sample": {"file": "", "rows": 0, "missing": []},
        }
        if any(item.severity == "error" for item in problems):
            out["notes"].append("源码没有通过静态检查，没有执行（改好后重试）。")
            return out

        try:
            inputs, _info = self.inputs(config)
        except CleanError as exc:
            # 还没选数据来源 / 没探测表头时点这个按钮是正常的：静态检查照样给出来，
            # 只是没有逐行结果可看。
            out["error"] = str(exc)
            out["notes"].append("没有可用的输入数据，这里只有静态检查结果。")
            return out
        target = inputs[0]
        names = [str(name) for name in fn.input_fields]
        from .clean_engine import resolve_mapping
        try:
            reader = _head_reader(Path(target[1]), config.input, rows)
        except (CleanIoError, OSError) as exc:
            out["error"] = f"读取输入失败：{exc}"
            return out
        try:
            mapping = resolve_mapping(list(reader.columns), config.fields)
            positions = [mapping.index.get(name) for name in names]
            missing = [name for name, position in zip(names, positions) if position is None]
            sample = [
                [_cell(row, position) for position in positions]
                for row in _read_head(reader, rows)
            ]
        finally:
            reader.close()
        out["sample"] = {"file": str(target[0]), "rows": len(sample), "missing": missing}
        if missing:
            out["notes"].append(
                f"字段 {'、'.join(missing)} 在这个文件里没有对应列（生成列或新列），"
                "按空值传入。"
            )
        if fn.phase == "post":
            out["notes"].append(
                "样本是文件里的原值；正式任务里它看到的是前面步骤清洗之后的值。"
            )
        if not sample:
            out["error"] = "这个文件里没有数据行，没有可试跑的内容。"
            return out

        printed: list[str] = []
        omitted = 0

        def collect(line: str) -> None:
            nonlocal omitted
            if len(printed) < PRINTED_LIMIT:
                printed.append(line)
            else:
                omitted += 1

        # 每次新起一个 worker，不复用任务池：调试是「跑一下我刚写的东西」，冷启动那
        # 200-400ms 换来的是「跑的就是编辑器里这段源码」这个确定性。
        worker = PythonWorker(
            [fn],
            config.python,
            seed=0,
            on_stderr=collect,
            python_executable=self.python_executable,
        )
        started = self._clock()
        try:
            if fn.mode == "column":
                call = worker.call_columns(
                    fn.name, {names[0]: [row[0] for row in sample]}, timeout=fn.timeout_s
                )
            else:
                call = worker.call_rows(fn.name, names, sample, timeout=fn.timeout_s)
            _settle(lambda: len(printed) + omitted)
        finally:
            worker.close()
        seconds = round(self._clock() - started, 2)

        out.update({
            "ran": True,
            "ok": call.ok,
            "seconds": seconds,
            "failure": call.failure,
            "detail": call.detail,
            "printed": printed,
            "printed_omitted": omitted,
        })
        if not call.ok:
            if call.failure == "timeout":
                # 子进程侧只回一句「没返回结果」，超时值只有这里知道 —— 而面板上最该说清
                # 的恰恰是「等了几秒就放弃了」（与正式任务用的是同一个 timeout_s）。
                out["error"] = (
                    f"函数执行超过 {fn.timeout_s:g} 秒，已强制结束"
                    "（试跑与正式任务用的是同一个超时）。"
                )
            else:
                out["error"] = f"{call.failure}：{call.detail}" if call.detail else call.failure
            return out
        out["notes"].extend(call.notes)
        if fn.mode == "column":
            values = call.columns or {}
            out["columns"] = [
                {
                    "name": name,
                    "input": [row[0] for row in sample[:5]],
                    "output": list(values.get(name) or [])[:5],
                }
                for name in fn.output_fields
            ]
        else:
            errors = dict(call.error_pairs())
            produced = call.rows or []
            out["rows"] = [
                {
                    "index": position,
                    "input": dict(zip(names, values)),
                    # None = 这一行没有改动（返回了 None，或返回的字段都没声明）。
                    # 出错的行也是 None —— 用同一行的 `error` 区分，别让面板猜。
                    "output": produced[position] if position < len(produced) else None,
                    "error": errors.get(position, ""),
                }
                for position, values in enumerate(sample)
            ]
        return out

    # ==================================================================== 任务生命周期

    @staticmethod
    def _new_seed() -> int:
        """新任务的随机种子。

        **种子必须随任务持久化**：随机生成的取值是「(种子, 字段, 行号, 第几次尝试)」的
        纯函数（`clean_generate.deterministic_seed`），所以续跑与重跑只要带着同一个种子，
        同一行的随机值就一模一样。换个种子会让「续跑后的产物与不中断运行逐字节相同」这条
        性质在随机字段上直接失效 —— 那是断点续处理全部可信度的来源。
        """
        return random.SystemRandom().randrange(2**32)

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """建任务并排队。

        请求体里可以带 `seed`（`/validate` 的返回值里有一个）：带上它，正式任务跑出来的
        随机字段就与用户在干跑里确认过的那份**逐值相同**。不带则随机取一个 —— 两种都对，
        但「确认过的预览 == 最终产出」显然更好，所以界面会把干跑那个种子回传。
        """
        config = self.config(payload)
        inputs, info = self.inputs(config)
        try:
            seed = int(payload.get("seed", self._new_seed())) % (2**32)
        except (TypeError, ValueError):
            raise CleanError(f"种子必须是整数：{payload.get('seed')}") from None
        task_id = uuid.uuid4().hex
        now = _iso()
        record = {
            "id": task_id,
            "name": config.name or Path(info["roots"][0]).name or "清洗任务",
            "status": "queued",
            "seed": seed,
            "progress": 0,
            "current": 0,
            "total": 0,
            "message": STATUS_TEXT["queued"],
            "created_at": now,
            "updated_at": now,
            # `request` 里**只能有合法的配置键**：它在每次续跑/重跑/打包时都会被重新
            # `model_validate` 一遍，多一个键（比如解析出来的绝对路径）就会让任务当场
            # 「配置已失效」。解析结果放在它旁边。
            "request": config.model_dump(),
            "source_info": info,
            "result": None,
            "error": "",
            "files_total": len(inputs),
            "llm": self.endpoint_summary(config),
        }
        with self.lock:
            self.tasks[task_id] = record
            self.controls[task_id] = CleanControl(self, task_id)
            self._save()
        self.bus.emit(task_id, "status", STATUS_TEXT["queued"], data={"status": "queued"})
        self.pool.submit(self._run, task_id, inputs, False)
        return self.public_task(record)

    def _submit_resume(self, task_id: str) -> None:
        task = self._record(task_id)
        if not task:
            return
        try:
            config = self.config(task["request"])
            inputs, _ = self.inputs(config)
        except CleanError as exc:
            self._update(task_id, status="interrupted", message=f"无法续跑：{exc}")
            return
        with self.lock:
            self.controls[task_id] = CleanControl(self, task_id)
        self.pool.submit(self._run, task_id, inputs, True)

    def resume_decision(self, task: Mapping[str, Any] | None) -> dict[str, Any]:
        """这个任务现在能不能续跑、为什么不能。界面据此决定给「继续」还是「重跑」。"""
        if not task:
            return {"resumable": False, "code": "missing", "reason": "任务不存在"}
        status = str(task.get("status"))
        if status in ("queued", "running", "paused"):
            return {"resumable": False, "code": "active", "reason": "任务正在运行"}
        if status == "completed":
            return {"resumable": False, "code": "completed", "reason": "任务已完成"}
        try:
            config = self.config(task["request"])
            inputs, _ = self.inputs(config)
            store = self.store(str(task["id"]))
            decision = resolve_resume(store.load(), config, inputs, store=store)
        except (CleanError, CleanEngineError, StateError) as exc:
            return {"resumable": False, "code": "unavailable",
                    "reason": f"无法读取任务状态：{exc}"}
        return {"resumable": bool(decision), "code": decision.code,
                "reason": decision.reason, "notes": list(decision.notes)}

    def control(self, task_id: str, action: str) -> dict[str, Any]:
        task = self._record(task_id)
        if not task:
            raise CleanNotFound("任务不存在")
        status = str(task.get("status"))
        if action == "pause":
            if status not in ("queued", "running"):
                raise CleanConflict(f"当前状态（{STATUS_TEXT.get(status, status)}）不能暂停")
            control = self.controls.get(str(task_id))
            if control is None:
                raise CleanConflict("任务不在运行，无法暂停")
            control.pause()
            self._update(task_id, status="paused", message="正在暂停（等待当前分块完成）")
            self.bus.emit(str(task_id), "status", "正在暂停", data={"status": "paused"})
        elif action == "cancel":
            if status not in ("queued", "running", "paused"):
                raise CleanConflict(f"当前状态（{STATUS_TEXT.get(status, status)}）不能取消")
            control = self.controls.get(str(task_id))
            if control is None:
                raise CleanConflict("任务不在运行，无法取消")
            control.cancel()
            self._update(task_id, status="running", message="正在停止（等待当前分块完成）")
            self.bus.emit(str(task_id), "status", "正在停止", data={"status": "running"})
        elif action == "resume":
            return self._resume(task)
        elif action == "retry":
            return self._restart(task_id)
        else:
            raise CleanConflict(f"未知操作：{action}")
        return self.public_task(self._record(task_id))

    def _resume(self, task: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(task["id"])
        status = str(task.get("status"))
        if status == "paused":
            control = self.controls.get(task_id)
            if control is None:
                raise CleanConflict("任务不在运行，无法继续")
            control.resume()
            self._update(task_id, status="running", message="继续处理")
            self.bus.emit(task_id, "status", "继续处理", data={"status": "running"})
            return self.public_task(self._record(task_id))
        if status == "running":
            raise CleanConflict("任务正在运行")
        decision = self.resume_decision(task)
        if not decision["resumable"]:
            # 让界面拿到机器可读的原因，好把「重跑」按钮摆到用户面前
            raise CleanConflict(f"{decision['reason']}（可选择重跑）")
        self._submit_resume(task_id)
        return self.public_task(self._record(task_id))

    def _restart(self, task_id: str) -> dict[str, Any]:
        """重跑：清空状态与产物，从第一行开始。**与续跑是两件事**，所以是两个动作。"""
        task = self._record(task_id)
        if not task:
            raise CleanNotFound("任务不存在")
        if task.get("status") in ("queued", "running", "paused"):
            raise CleanConflict("任务正在运行：请先取消，再重跑")
        config = self.config(task["request"])
        inputs, _ = self.inputs(config)
        store = self.store(task_id)
        store.delete()
        shutil.rmtree(store.output_dir, ignore_errors=True)
        (self.root / f"{task_id}.zip").unlink(missing_ok=True)
        with self.lock:
            self.controls[task_id] = CleanControl(self, task_id)
            self.tasks[task_id].update(
                status="queued", progress=0, current=0, total=0, result=None, error="",
                message="等待重新处理", updated_at=_iso(), files_total=len(inputs),
            )
            self._save()
        self.bus.emit(task_id, "status", "等待重新处理", data={"status": "queued"})
        self.pool.submit(self._run, task_id, inputs, False)
        return self.public_task(self._record(task_id))

    def delete(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.pop(str(task_id), None)
            if task is None:
                return False
            control = self.controls.pop(str(task_id), None)
            if control is not None:
                control.cancel()
            self._save()
        self.bus.forget(str(task_id))
        shutil.rmtree(self._task_dir(task_id), ignore_errors=True)
        (self.root / f"{task_id}.zip").unlink(missing_ok=True)
        return True

    def cancel_all(self) -> None:
        """关闭钩子：先取消所有活动任务，再等线程池 —— 否则 GB 任务会让关闭最长卡几十秒。"""
        with self.lock:
            controls = list(self.controls.values())
        for control in controls:
            try:
                control.cancel()
            except Exception:  # pragma: no cover - 取消不该失败
                LOGGER.debug("取消任务失败", exc_info=True)

    def shutdown(self) -> None:
        self._closed = True
        self.cancel_all()
        self.pool.shutdown(wait=True)
        self.bus.close()

    # -- 跑 ----------------------------------------------------------------

    def _run(self, task_id: str, inputs: Sequence[tuple[str, str]], resume: bool) -> None:
        task = self._record(task_id)
        if task is None or self._closed:
            return
        control = self.controls.get(task_id) or CleanControl(self, task_id)
        with self.lock:
            self.controls[task_id] = control
        try:
            config = self.config(task["request"])
        except CleanError as exc:
            self._update(task_id, status="failed", message="配置已失效", error=str(exc))
            return
        store = self.store(task_id)
        try:
            # 事件日志要在任务目录存在之后才能挂（`EventLog` flush 时按目录写文件），
            # 而引擎自己的 `ensure()` 要等到 `run()` 里 —— 新任务必须在这里先建出来。
            store.ensure()
        except OSError as exc:
            self._update(task_id, status="failed", message="无法创建任务目录", error=str(exc))
            return
        log = EventLog(store.events_path)
        pool = self._build_pool(config)
        self.bus.attach_log(task_id, log)
        self._record_at[task_id] = 0.0
        try:
            self._update(task_id, status="running", error="",
                         message="继续处理" if resume else "开始处理")
            self.bus.emit(task_id, "status", "开始处理" if not resume else "继续处理",
                          data={"status": "running"})
            engine = CleanEngine(
                task_id,
                config,
                store,
                bus=self.bus,
                control=control,
                llm_pool=pool,
                seed=int(task.get("seed") or 0),
                secrets=self._secrets(),
                python_executable=self.python_executable,
                on_progress=lambda payload: self._on_progress(task_id, payload),
            )
            result = engine.run(inputs, resume=resume)
        except ResumeRefused as exc:
            # 检查过再跑（`_resume` 里做过一次），但输入可能在两者之间被改动，所以这里是
            # 竞态兜底：任务回到可操作状态，让用户去选重跑。
            decision = exc.decision
            self._update(task_id, status="interrupted", message=decision.reason)
            self.bus.emit(task_id, KIND_ERROR, decision.reason,
                          level="warn", data={"status": "interrupted"})
        except CleanEngineError as exc:
            self._update(task_id, status="failed", message="任务失败", error=str(exc))
        except Exception as exc:  # 引擎的契约是不抛异常；这里兜的是它自己的 bug
            LOGGER.exception("清洗任务 %s 异常中止", task_id)
            self._update(task_id, status="failed", message="任务异常中止",
                         error=f"{type(exc).__name__}: {exc}")
        else:
            self._record_result(task_id, result)
        finally:
            self.bus.flush_progress(task_id)
            self.bus.flush_log(task_id)
            self.bus.detach_log(task_id)
            try:
                log.close()
            except Exception:  # pragma: no cover
                LOGGER.debug("关闭事件日志失败", exc_info=True)
            with self.lock:
                self.controls.pop(task_id, None)
            self._persist_latency(pool)

    def _on_progress(self, task_id: str, payload: Mapping[str, Any]) -> None:
        """引擎的进度回调：写任务记录（节流）。SSE 那一路由引擎自己推。"""
        values = {
            "progress": payload.get("pct", 0),
            "current": payload.get("done", 0),
            "total": payload.get("total") or 0,
            "eta_low": payload.get("eta_low"),
            "eta_high": payload.get("eta_high"),
            "rate": payload.get("rate"),
            "unstable": payload.get("unstable", False),
            "files_done": payload.get("files_done", 0),
            "files_total": payload.get("files_total", 0),
        }
        message = str(payload.get("message") or "")
        if message:
            values["message"] = message
        self._update_throttled(task_id, **values)

    def _record_result(self, task_id: str, result: Any) -> None:
        files = [file_entry(item) for item in (result.files or [])]
        rows_in = sum(item["rows_in"] for item in files)
        rows_out = sum(item["rows_out"] for item in files)
        status = str(result.status or "completed")
        values: dict[str, Any] = {
            "status": status,
            "message": STATUS_TEXT.get(status, status),
            "error": result.error or "",
            "result": {
                "files": files,
                "rows_in": rows_in,
                "rows_out": rows_out,
                "dropped": sum(item["dropped"] for item in files),
                "duration": round(float(result.duration or 0.0), 2),
                "report": report_filename(task_id),
                "resumed": bool(result.state and result.state.resume_count),
                "resume_count": int(getattr(result.state, "resume_count", 0) or 0),
            },
            "current": rows_in,
            "total": rows_in,
        }
        if status == "completed":
            values["progress"] = 100
        self._update(task_id, **values)
        self.bus.emit(task_id, "status", STATUS_TEXT.get(status, status),
                      level="info" if status == "completed" else "warn",
                      data={"status": status, "error": result.error or ""})

    # ==================================================================== 模型池

    def endpoints(self) -> dict[str, Any]:
        return {
            "endpoints": [item.public() for item in load_pool(self.pool_file)],
            "sentinel": SENTINEL,
            "file": str(self.pool_file),
        }

    def save_endpoints(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """整表替换端点池。密钥哨兵由 `merge_endpoint` 翻译（前端拿到的就是哨兵）。"""
        items = payload.get("endpoints")
        if not isinstance(items, list):
            raise CleanError("endpoints 必须是数组")
        existing = {item.id: item for item in load_pool(self.pool_file)}
        merged: list[LlmEndpoint] = []
        seen: set[str] = set()
        for raw in items:
            if not isinstance(raw, Mapping):
                raise CleanError("端点必须是对象")
            data = dict(raw)
            endpoint_id = str(data.get("id") or "").strip()
            if not endpoint_id:
                data["id"] = endpoint_id = uuid.uuid4().hex[:12]
            if endpoint_id in seen:
                raise CleanError(f"端点 id 重复：{endpoint_id}")
            seen.add(endpoint_id)
            try:
                merged.append(merge_endpoint(existing.get(endpoint_id), data))
            except Exception as exc:
                raise CleanError(f"端点配置有误（{data.get('name') or endpoint_id}）：{_readable(exc)}") from exc
        save_pool(self.pool_file, merged)
        return {"endpoints": [item.public() for item in merged], "sentinel": SENTINEL}

    def test_endpoint(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """连通性测试：发一条最短的请求。**失败不是异常**，是一个正常结果。"""
        raw = payload.get("endpoint") if isinstance(payload.get("endpoint"), Mapping) else payload
        data = dict(raw or {})
        endpoint_id = str(data.get("id") or "").strip()
        existing = next((item for item in load_pool(self.pool_file) if item.id == endpoint_id), None)
        try:
            endpoint = merge_endpoint(existing, data)
        except Exception as exc:
            raise CleanError(f"端点配置有误：{_readable(exc)}") from exc
        pool = LlmPool([endpoint])
        # 预算必须容得下这次测试自己会发的那几次尝试。一开始写的是 `max_calls=1`，结果
        # 「测试连接」对着一个连不上的地址报的是「调用次数已达上限 1」—— `chat()` 在一次
        # 失败后 `note()` 再 `check()`，预算当场用尽，**真实原因（连接被拒）被这个护栏盖
        # 掉了**。一个把用户想问的事情藏起来的护栏，比没有护栏更糟。
        client = LlmClient(pool, budget=CallBudget(
            max_calls=MAX_FAILOVER_ENDPOINTS + 1, abort_error_rate=1.0,
        ))
        started = self._clock()
        try:
            reply = client.chat("ping", model=endpoint.model, max_tokens=8, temperature=0)
        except Exception as exc:
            return {"ok": False, "endpoint": endpoint.public(),
                    "seconds": round(self._clock() - started, 2),
                    "message": f"{type(exc).__name__}: {exc}"}
        finally:
            client.close()
        return {"ok": True, "endpoint": endpoint.public(),
                "seconds": round(self._clock() - started, 2),
                "latency_ms": round(float(reply.seconds) * 1000, 1),
                "message": (reply.text or "")[:120]}

    def _build_pool(self, config: CleanTaskConfig) -> LlmPool | None:
        """按任务选择的端点建池。**池是每任务一个**：并发上限与熔断状态是运行期状态，
        任务之间共享它们会让一个坏任务把端点打熔断、连累另一个。EWMA 延迟反过来是要
        共享的（越用越准），所以它落在端点池文件里（见 `_persist_latency`）。"""
        selected = set(config.llm.endpoint_ids)
        endpoints = [item for item in load_pool(self.pool_file)
                     if not selected or item.id in selected]
        if not endpoints:
            return None
        return LlmPool(endpoints)

    def _persist_latency(self, pool: LlmPool | None) -> None:
        """把这一轮跑出来的 EWMA 延迟写回端点池文件，让下次预估更准。

        只在**有样本**时改，且整表重读后写回 —— 两个任务同时收尾时不会互相覆盖。
        熔断状态、在途数这些是运行期状态，**不写回**：重启后重新探测比继承一个过期的
        熔断标记更安全。
        """
        if pool is None:
            return
        measured = {item["id"]: item for item in pool.snapshot()}
        if not measured:
            return
        with self.lock:
            endpoints = load_pool(self.pool_file)
            changed = False
            for endpoint in endpoints:
                state = measured.get(endpoint.id)
                if not state:
                    continue
                # 样本数用状态里那个计数器，而不是「成功 + 失败」：EWMA 只在成功时更新，
                # 拿失败数凑出来的样本量会比实际观测多 —— 那个数字是给用户看「估得准不准」
                # 的依据，虚高就等于把「只量了 2 次」说成「量了 40 次」。
                samples = int(state.get("ewma_samples", 0) or 0)
                if samples <= 0 or float(state.get("ewma_ms") or 0.0) <= 0.0:
                    continue
                endpoint.ewma_ms = float(state["ewma_ms"])
                endpoint.ewma_samples = samples
                # 延迟是「一次请求」的耗时，得连「一次几行」一起记下来，下次改批量才算得准
                rows = float(state.get("ewma_rows") or 0.0)
                if rows > 0:
                    endpoint.ewma_rows = rows
                changed = True
            if changed:
                try:
                    save_pool(self.pool_file, endpoints)
                except OSError:
                    LOGGER.warning("写回端点延迟失败", exc_info=True)

    def _secrets(self) -> list[str]:
        """报告与日志脱敏用的密钥清单。**只在这里出现**，不进任何任务记录。"""
        return [item.api_key for item in load_pool(self.pool_file) if item.api_key]

    # ==================================================================== 产物

    def report(self, task_id: str) -> str:
        store = self.store(task_id)
        path = report_path(store)
        if path.is_file():
            return path.read_text("utf-8")
        state = None
        try:
            state = store.load()
        except StateError as exc:
            raise CleanConflict(f"状态文件损坏：{exc}") from exc
        if state is None:
            raise CleanNotFound("任务还没有报告（状态文件不存在）")
        # 没有报告文件但状态在（引擎收尾时报告生成失败）：从状态现场生成一份，总比 404 好
        from .clean_engine import state_to_report

        task = self._record(task_id) or {}
        try:
            config = self.config(task["request"])
        except CleanError as exc:
            raise CleanConflict(f"配置已失效，无法生成报告：{exc}") from exc
        return state_to_report(config, state, task_id=task_id, secrets=self._secrets())

    def stats(self, task_id: str) -> dict[str, Any]:
        """字段统计（清洗前 / 清洗后）。与报告**同源** —— 都读 `state.json` 里那一份
        持久化统计，而不是拿产物重算一遍：重算会得到另一套数字，两份数字对不上时没有
        任何办法判断哪一份是真的。

        能算的边界和报告一致：p50/p95 是蓄水池采样的**近似值**（带上样本量与近似标记），
        中位数/p99/精确去重计数**不报**。报一个用户会相信的错数字，比不报更糟。
        """
        store = self.store(task_id)
        try:
            state = store.load()
        except StateError as exc:
            raise CleanConflict(f"状态文件损坏：{exc}") from exc
        if state is None:
            raise CleanNotFound("任务还没有统计（状态文件不存在）")
        # 统计在 `state.progress` 里（与 `state_to_report` 读的是同一处），不在顶层
        snapshot = state.progress or {}
        task = self._record(task_id) or {}
        seed = int(snapshot.get("seed") or task.get("seed") or 0)
        size = {"limit": int(snapshot.get("limit") or 1000),
                "reservoir_size": int(snapshot.get("reservoir_size") or 2000)}
        # 两个随机流必须与引擎逐字相同（before 用 seed、after 用 seed+1），否则续跑过的
        # 任务在这里读出来的分位数会与报告里的不一样
        before = StatsSet.restore(snapshot.get("before") or {}, seed=seed, **size)
        after = StatsSet.restore(snapshot.get("after") or {}, seed=seed + 1, **size)
        counters = Counters()
        counters.restore(snapshot.get("counters") or {})
        after_counters = Counters()
        after_counters.restore(snapshot.get("after_counters") or {})

        names: list[str] = []
        try:
            names = [spec.dest for spec in self.config(task["request"]).fields]
        except (CleanError, KeyError, TypeError):
            # 配置已失效也要能看统计：顺序退回稳定排序，字段一个都不能少
            names = []
        for name in sorted(set(before.fields) | set(after.fields)):
            if name not in names:
                names.append(name)

        fields = []
        for name in names:
            fields.append({
                "field": name,
                "before": _stat_summary(before.field(name)),
                "after": _stat_summary(after.field(name)),
                "violations": counters.field_violations(name),
                "after_violations": after_counters.field_violations(name),
                "fallbacks": {
                    f"{kind}|{policy}": count
                    for (kind, policy), count in counters.field_fallbacks(name).items()
                },
            })
        return {
            "rows_before": before.rows,
            "rows_after": after.rows,
            "fields": fields,
            "sample": {**size, "top_keep": min(int(size["limit"]), 1000)},
            "notes": [
                "p50 / p95 来自蓄水池采样，是近似值；样本量与是否近似逐字段标注",
                "中位数、p99、精确去重计数不做统计：GB 级文件装不进内存，也没有精确的单遍有界算法",
            ],
        }

    def logs(self, task_id: str, *, after: int = 0, limit: int = 500,
             level: str = "info") -> dict[str, Any]:
        """历史日志。**这是断线补齐与刷新页面用的**，实时通道是 SSE（见 clean_api）。

        先问总线的环形缓冲（O(1)、最热的那一段），命中不了再回落到 `events.jsonl`
        （可能很大，所以只按 `after` 往后读一段）。
        """
        events, gap = self.bus.replay(int(after), str(task_id), limit=int(limit),
                                      min_level=level)
        items = [item.to_dict() for item in events]
        source = "ring"
        if not items and after <= 0:
            store = self.store(task_id)
            raw = EventLog.read(store.events_path, after=int(after), limit=int(limit))
            items = [_trim(item) for item in raw]
            source = "file"
        return {"events": items, "source": source, "gap": gap,
                "last": self.bus.last_seq}

    def preview(self, task_id: str, *, file: str = "", rows: int = 50,
                side: str = "both") -> dict[str, Any]:
        """清洗前 VS 清洗后的对齐数据。

        左右两栏各由 `_aligned_input_head` / `_aligned_output_head` 读出来 —— 干跑预览
        （`validate`）用的是同一对方法，于是「试跑时看到的」与「任务跑起来后看到的」是
        同一段代码量出来的，不会各说各话。
        """
        task = self._record(task_id)
        if not task:
            raise CleanNotFound("任务不存在")
        config = self.config(task["request"])
        inputs, _ = self.inputs(config)
        key, path = inputs[0]
        if file:
            found = [item for item in inputs if item[0] == file]
            if not found:
                raise CleanNotFound(f"输入里没有这个文件：{file}")
            key, path = found[0]
        rows = max(1, min(int(rows or 50), 500))
        dest = config.dest_fields()
        generated = {spec.dest for spec in config.fields if spec.generate is not None}
        before: list[list[Any]] = []
        before_columns: list[str] = []
        source_columns: list[str] = []
        unmapped: list[str] = []
        note = ""
        if side in ("both", "before"):
            head = self._aligned_input_head(config, path, rows)
            source_columns = head["source_columns"]
            # 左表头 = 文件的列名（前 len(dest) 列与目的字段一一对应，末尾是会被丢弃的源列）
            before_columns = head["before_columns"]
            unmapped = head["unmapped"]
            before = head["rows"]
            note = head["note"]
        after: list[list[Any]] = []
        if side in ("both", "after"):
            # 「右侧只有一部分 / 什么都没有」的说法取决于任务走到哪了：正在跑却告诉用户
            # 「没有数据」、已经结束了还说「正在处理中」，两种都是在骗人。
            running = str(task.get("status") or "") not in TERMINAL_STATUS
            partial = ("任务还没跑完，清洗后只显示已提交的部分" if running
                       else "清洗后只显示了已提交的部分")
            after, after_note = self._aligned_output_head(
                self.store(task_id).output_path(key, config.output.format),
                config,
                rows,
                partial=partial,
                empty=partial if running else "没有已提交的数据行",
            )
            # 左侧的读取失败优先显示：两栏都出问题时，先让用户知道输入就读不了
            note = note or after_note
        return {
            "file": key,
            "columns": dest,
            "before_columns": before_columns,
            "generated": sorted(generated),
            "source_columns": source_columns,
            "unmapped": unmapped,
            "before": before,
            "after": after,
            "rows": min(len(before) if before else len(after), rows),
            "note": note,
            "files": [_file_brief(item, path_) for item, path_ in inputs],
        }

    def _aligned_input_head(
        self, config: CleanTaskConfig, path: Path, rows: int
    ) -> dict[str, Any]:
        """输入的前 `rows` 行 → 左栏要的全部内容：表头、行、以及会被丢弃的源列。

        两条不变量，缺一条这个视图就在说谎：

        1. **左表头是文件里的列名**（`before_columns`），不是目的字段名。重命名
           （`source` → `dest`）与「只填目的字段」的新列，都要在这一行上看出来 —— 拿
           目的字段名当左表头，用户看到的就不是他那个文件。
        2. **会被丢弃的源列必须出现在左边**（`before_columns` 末尾那一段，与 `unmapped`
           一一对应），带着它们的原值。这是用户唯一能看见「哪几列不会进产物」的地方。

        前 `len(config.dest_fields())` 列仍**按目的字段对齐**（用引擎同一套
        `resolve_mapping`）—— 左右两栏的逐单元格对比靠的就是它；生成字段在这一段里恒为
        空，于是「新增」这一类差异自然显示出来。末尾那几列只在左栏出现，右栏（产物）本来
        就没有它们。
        """
        from .clean_engine import resolve_mapping

        dest = config.dest_fields()
        out: list[list[Any]] = []
        source_columns: list[str] = []
        before_columns: list[str] = []
        unmapped: list[str] = []
        try:
            reader = _head_reader(path, config.input, rows)
            try:
                source_columns = list(reader.columns)
                mapping = resolve_mapping(source_columns, config.fields)
                unmapped = list(mapping.unmapped)
                spec_source = {spec.dest: spec.source for spec in config.fields}
                used = set(mapping.index.values())
                # 被丢弃的列按**位置**找，不按名字：重复表头时 `columns.index(name)` 会
                # 指到第一次出现的那一列，可能与 `resolve_mapping` 认定的不是同一列。
                dropped = [position for position in range(len(source_columns))
                           if position not in used]
                for name in dest:
                    position = mapping.index.get(name)
                    if position is not None:
                        before_columns.append(str(source_columns[position]))
                    else:
                        # 文件里没有这一列：配了来源就是这个文件缺列（多文件并集才会遇到），
                        # 没配来源就是新列 —— 两种都得显示成目的字段名，别伪装成源列。
                        before_columns.append(spec_source.get(name) or name)
                before_columns += [str(source_columns[position]) for position in dropped]
                for row in _read_head(reader, rows):
                    out.append(
                        ["" if mapping.index.get(name) is None
                         else _cell(row, mapping.index[name]) for name in dest]
                        + [_cell(row, position) for position in dropped]
                    )
            finally:
                reader.close()
        except (CleanIoError, OSError) as exc:
            # 表头都没读出来，左栏给不出列名（行也是空的，不存在错位的单元格）
            return {"source_columns": source_columns, "before_columns": [],
                    "unmapped": unmapped, "rows": out,
                    "note": f"读取输入失败：{exc}"}
        return {"source_columns": source_columns, "before_columns": before_columns,
                "unmapped": unmapped, "rows": out, "note": ""}

    def _aligned_output_head(
        self, output: Path, config: CleanTaskConfig, rows: int, *,
        partial: str, empty: str,
    ) -> tuple[list[list[Any]], str]:
        """产物的前 `rows` 行，按目的字段对齐 → (行, note)。

        `partial` / `empty` 由调用方给：同一句「右侧是空的」在**正在跑的任务**、**跑完的
        任务**、**干跑**三种场合下的含义不一样，措辞也必须是三种。
        """
        dest = config.dest_fields()
        note = ""
        out: list[list[Any]] = []
        if not output.is_file():
            # **追写目标才是磁盘上真实存在的那个文件**：xlsx/parquet 写的是 `.part.csv`
            # spool，其余格式就是最终文件本身。命名规则只有 `append_target` 一处实现
            # （见它的 docstring）—— 这里再算一遍 `output_path` 是个空操作，结果是「跑过
            # 一半的任务永远显示产物还没生成」。
            spool = append_target(output, config.output)
            note = partial if spool != output else "产物还没生成"
            output = spool
        if output.is_file() and not output.stat().st_size:
            # 一个提交点都还没到（刚建好、或取消在第一次提交之前）的空文件。真去读它
            # 只会拿到「表头行 0 超出文件范围」—— 实现细节，不是用户该看到的结论。
            return out, note or empty
        try:
            reader = _head_reader(output, _output_options(config), rows)
            try:
                out_columns = list(reader.columns)
                index = {name: position for position, name in enumerate(out_columns)}
                for row in _read_head(reader, rows):
                    out.append([_cell(row, index[name]) if name in index else ""
                                for name in dest])
            finally:
                reader.close()
        except (CleanIoError, OSError) as exc:
            return out, note or f"读取产物失败：{exc}"
        # 有文件、也没有报错，但一行都没有：只写了表头的产物就是这种（取消正好卡在
        # 第一次提交之前）。右侧空着而不给一句解释，用户只能猜是不是坏了。
        return out, note or ("" if out else empty)

    def _dry_preview(
        self, config: CleanTaskConfig, store: StateStore, target: tuple[str, Path],
        rows: int,
    ) -> dict[str, Any]:
        """干跑产物的一段预览，让用户在**提交任务之前**看到真数据。

        必须在 `validate` 删掉临时状态目录**之前**读：删完就只剩聚合计数，用户看到
        「0 处违规」却看不到「那到底会变成什么样」—— 而后者才是他决定要不要提交的依据。
        """
        key, path = target
        rows = max(1, min(int(rows or DRY_PREVIEW_ROWS), DRY_PREVIEW_ROWS))
        dest = config.dest_fields()
        generated = {spec.dest for spec in config.fields if spec.generate is not None}
        head = self._aligned_input_head(config, path, rows)
        after, after_note = self._aligned_output_head(
            store.output_path(key, config.output.format),
            config,
            rows,
            # 干跑到这里已经结束了，所以用它跑完后的那套说法（与任务预览同一批句子）
            partial="清洗后只显示了已提交的部分",
            empty="没有已提交的数据行",
        )
        return {
            "file": key,
            "columns": dest,
            "before_columns": head["before_columns"],
            "generated": sorted(generated),
            "source_columns": head["source_columns"],
            "unmapped": head["unmapped"],
            "before": head["rows"],
            "after": after,
            "rows": min(len(head["rows"]) if head["rows"] else len(after), rows),
            "note": head["note"] or after_note,
            "files": [_file_brief(key, path)],
        }

    def archive(self, task_id: str) -> Path:
        """打包下载：产物 + 报告 + 两个 jsonl。

        **失败文件的产物不进包**：半截文件混在交付物里，收数据的人不会去核对报告。
        `zip` 用 ZIP_STORED —— 已清洗的文本压缩收益小，而 CPU 代价以分钟计（GB 级）。
        """
        task = self._record(task_id)
        if not task:
            raise CleanNotFound("任务不存在")
        store = self.store(task_id)
        try:
            config = self.config(task["request"])
        except CleanError as exc:
            raise CleanConflict(f"配置已失效，无法打包：{exc}") from exc
        failed = [item.get("key") for item in (task.get("result") or {}).get("files") or []
                  if item.get("status") == "failed"]
        skip = _failed_outputs(store, config.output, failed)
        archive = self.root / f"{task_id}.zip"
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as package:
            for path in sorted(store.output_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(store.output_dir).as_posix()
                if relative in skip:
                    continue
                package.write(path, f"output/{relative}")
            for path, name in ((report_path(store), "report.md"),
                               (store.events_path, "events.jsonl"),
                               (store.fallback_path, "fallback.jsonl")):
                if path.is_file():
                    package.write(path, name)
            if not package.namelist():
                raise CleanNotFound("还没有可下载的内容")
        return archive


def _failed_outputs(store: StateStore, options: OutputOptions,
                    failed: Iterable[str | None]) -> set[str]:
    """失败文件留下的产物名（相对 `output/`），含 xlsx/parquet 的 `.part.csv` 中间产物。

    命名规则只允许有一处实现，所以这里直接问 `append_target` —— 手写一遍「去掉后缀、
    再加后缀」就是「排除错了文件」这类 bug 的开始，而这里排错方向的代价是**把别人的
    数据从交付包里删掉**。
    """
    names: set[str] = set()
    for key in failed:
        if not key:
            continue
        target = store.output_path(str(key), options.format)
        for path in (target, append_target(target, options)):
            try:
                names.add(path.relative_to(store.output_dir).as_posix())
            except ValueError:                  # pragma: no cover - 路径必然在 output 下
                continue
    return names


def _cell(row: Sequence[Any], index: int | None) -> Any:
    if index is None or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else value


def _problem(item: Any) -> dict[str, Any]:
    """`clean_worker.Problem` → 界面用的字典。

    `line` 是**行号**（1 起），不是列偏移 —— 编辑器按整行划波浪线，理由见
    `clean_worker._gate_problems` 的说明（`col_offset` 是字节偏移，中文注释会错位）。
    """
    return {"line": item.line, "text": item.text, "severity": item.severity}


def _settle(count: Callable[[], int]) -> None:
    """回包前等 stderr 安静下来（`STDERR_QUIET_S`）。

    子进程的 stderr 与结果帧走**两条管道**，谁先到没有保证：函数里最后一行 `print`
    可能比结果帧晚几毫秒才被读到。不等这一下，调试面板就会偶尔少显示一行 —— 一个
    「有时会漏输出」的面板，比一个稳定慢 50ms 的难用得多。
    """
    deadline = time.monotonic() + STDERR_DRAIN_MAX_S
    seen = count()
    quiet = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline or now - quiet >= STDERR_QUIET_S:
            return
        time.sleep(0.01)
        if count() != seen:
            seen = count()
            quiet = time.monotonic()


def _head_reader(path: Path, options: InputOptions, rows: int):
    from .clean_io import open_reader

    return open_reader(path, options, max(1, int(rows)))


def _inspect_rows(value: Any) -> int:
    """探测请求要几行样本 → 夹在 [1, INSPECT_MAX_ROWS]。

    这是给人眼看的一屏预览，不是取数接口。夹紧之外还要**容错**：原来直接 `int(...)`，
    界面发来一个非数字就是一个 500，而这里本来有个合理的默认值可用。
    """
    try:
        rows = int(value)
    except (TypeError, ValueError):
        return INSPECT_ROWS
    return min(max(1, rows), INSPECT_MAX_ROWS)


def _output_options(config: CleanTaskConfig) -> InputOptions:
    """读**产物**用的读选项 —— 和读输入的那套不是一回事。

    产物是我们自己写的：UTF-8、`output.csv_delimiter` 分隔、首行即表头、没有公式（写出
    时已经防过注入）。照搬 `config.input` 会带进两个必然读错的字段：用户给输入指定的
    `delimiter`（比如 `;`）会把产物整行读成单列，预览里「清洗后」一栏全空；`sheet` 指的是
    **输入**的工作表名，产物里不存在，于是直接报「找不到工作表」。
    """
    return InputOptions(
        delimiter=config.output.csv_delimiter,
        encoding="utf-8",
        sanitize_formula=False,
    )


def _read_head(reader: Any, rows: int) -> list[list[Any]]:
    """只读第一批就够 —— `open_reader` 的批大小已经按 `rows` 给了，这里再兜一层。

    **空文件返回的是 `None` 而不是空列表**（`read_batch` 的约定）。刚建好、还没提交过任何
    一行的产物就是这种文件（取消在第一次提交之前，或预览一个正在跑的任务），于是这里必须
    容得下 `None` —— 否则预览接口 500，界面上只会说「服务器错误」。
    """
    batch = reader.read_batch() or []
    return [list(row) for row in batch[:rows]]


def file_entry(item: Any) -> dict[str, Any]:
    """一个文件的结果条目，附中文状态。

    **文件级状态与任务级状态不是一套词**：任务是 `completed/failed/cancelled/
    interrupted`，文件是 `done/failed/skipped/pending/running`（见 `FileState`）。这里
    统一给出 `status_text`，界面就不必各自猜一遍 —— 猜错的后果是「已完成的文件」显示成
    「已完成的任务」那种似是而非的标签。
    """
    status = str(getattr(item, "status", "") or "")
    return {
        # `FileReport.name` 与 `FileState.key` 是同一个东西的两个名字（前者在报告里，
        # 后者在状态里），两个来源都要能喂进来
        "key": getattr(item, "name", "") or getattr(item, "key", ""),
        "status": status,
        "status_text": FILE_STATUS_TEXT.get(status, status),
        "rows_in": int(getattr(item, "rows_in", 0) or 0),
        "rows_out": int(getattr(item, "rows_out", 0) or 0),
        "dropped": int(getattr(item, "dropped", 0) or 0),
        "parse_errors": int(getattr(item, "parse_errors", 0) or 0),
        "ragged_rows": int(getattr(item, "ragged_rows", 0) or 0),
        "replacement_chars": int(getattr(item, "replacement_chars", 0) or 0),
        "seconds": round(float(getattr(item, "seconds", 0.0) or 0.0), 3),
        "size": int(getattr(item, "size", 0) or 0),
        "message": getattr(item, "message", "") or "",
        "column_map": dict(getattr(item, "column_map", {}) or {}),
        "unmapped": list(getattr(item, "unmapped", ()) or ()),
    }


def _file_brief(key: str, path: str) -> dict[str, Any]:
    item = Path(path)
    try:
        size = item.stat().st_size
    except OSError:
        size = 0
    return {"key": key, "name": item.name, "size": size,
            "kind": file_kind(item, allow_unknown=True)}


def _stat_summary(stat: Any) -> dict[str, Any] | None:
    """一个字段在一侧的统计摘要。`None` = 这一侧根本没有这个字段（例如只在新列里出现）。

    `p50`/`p95` 报的是**长度**还是**数值**由 `percentile_of` 说明 —— 不写清楚的话，
    「年龄 p50 = 4」会被读成 4 岁而不是 4 个字符。`approximate` 与 `sample_size` 一起
    给出，让界面能明确标注这是采样值。
    """
    if stat is None:
        return None
    return {
        "count": int(stat.count),
        "empty": int(stat.empty),
        "empty_rate": round(float(stat.empty_rate), 4),
        "length_min": stat.length_min,
        "length_max": stat.length_max,
        "length_mean": round(float(stat.length_mean), 2),
        "types": dict(stat.types),
        "p50": stat.percentile(0.5),
        "p95": stat.percentile(0.95),
        "percentile_of": stat.percentile_of(),
        "approximate": bool(stat.percentiles_approximate()),
        "sample_size": int(stat.sample_size),
        "numeric_mean": round(float(stat.num_mean), 4) if stat.numeric else None,
        "numeric_stddev": round(float(stat.numeric_stddev), 4) if stat.numeric else None,
        "top": [[value, count] for value, count in stat.freq.top(8)],
    }


def _trim(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": int(item.get("seq", 0) or 0),
        "at": item.get("at", ""),
        "level": item.get("level", "info"),
        "kind": item.get("kind", "log"),
        "task_id": item.get("task_id", ""),
        "message": item.get("message", ""),
        "data": item.get("data") or None,
    }


def _readable(exc: BaseException) -> str:
    """pydantic 的 ValidationError 太长，取前几条能指出位置的错误。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:  # pragma: no cover
            return str(exc)
        parts = []
        for item in items[:3]:
            location = ".".join(str(part) for part in item.get("loc") or ())
            parts.append(f"{location}：{item.get('msg')}" if location else str(item.get("msg")))
        more = "" if len(items) <= 3 else f"（还有 {len(items) - 3} 处）"
        return "；".join(parts) + more
    return str(exc)
