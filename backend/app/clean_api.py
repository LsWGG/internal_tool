"""`/api/clean` 路由：上传、探测、干跑、预估、任务 CRUD、SSE、报告、下载、端点池。

约定与 `word_batch_api.make_router` 一致（`manager.lock` + `run_in_threadpool`），但有两处
**故意不同**，都是这个工具的量级逼出来的：

1. **SSE 端点是 `async def` + `EventSourceResponse`，不走 `run_in_threadpool`。**
   FastAPI 把同步端点丢进 40 个线程的默认线程池里跑，一个 SSE 端点会占住一个线程直到任务
   结束 —— 40 个观察者就能把整个 API 卡死。异步端点不占线程。
2. **上传是流式落盘的，不在内存里攒。** 兄弟工具读进内存再交给 manager（几十 MB 合理），
   这里一个目录可以到 GB，攒进内存等于把服务打死。落盘后再让 manager 登记。

时间与规模的口径：上传上限、行数上限这些**数字**都在 `clean_manager` 里定义，路由只用，
不再各写一份 —— 两份上限迟早会不一致，而不一致的那一半是「绕过护栏」。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from .clean_format import ruff_command
from .clean_generate import SUPPORTED_LOCALES, generator_catalog
from .clean_manager import (
    FILE_STATUS_TEXT,
    INPUT_EXTENSIONS,
    STATUS_TEXT,
    CleanConflict,
    CleanError,
    CleanManager,
    CleanNotFound,
    ensure_disk_space,
    free_bytes,
    safe_relpath,
)
from .clean_worker import vocabulary

CHUNK = 1024 * 1024
# 每写这么多个块问一次磁盘剩余空间。不是上限（上限在 clean_manager），是询问的**频次**：
# 每块都问是一次多余的 statfs，而这个粒度漏掉的量（约 64MB）远小于水位线本身。
DISK_CHECK_CHUNKS = 64


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "是")


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in str(os.environ.get(name, "")).split(os.pathsep)
            if item.strip()]


def make_router(root):
    """挂载 `/api/clean`。

    几项与部署有关的开关走环境变量（`CLEAN_*`），因为它们是**机器级**的：允许读哪些目录、
    用户 Python 用哪个解释器、默认要不要自动续跑。任务级的配置仍然全在请求体里。
    """
    # 数据目录也是机器级的：GB 级输入与产物不该和代码挤在同一个分区，换盘就得能换根。
    # 顺带让「同一份代码、两个独立数据根」成为可能 —— 端到端测试需要它才能互不干扰。
    root = Path(os.environ.get("CLEAN_DATA_DIR") or root)
    manager = CleanManager(
        root,
        # 用户函数跑在与服务同一个解释器里 —— venv 里装了什么，函数就能用什么
        python_executable=os.environ.get("CLEAN_PYTHON") or sys.executable,
        auto_resume=_env_flag("CLEAN_AUTO_RESUME", False),
        allowed_roots=_env_list("CLEAN_ALLOWED_ROOTS") or None,
        max_tasks=int(os.environ.get("CLEAN_MAX_TASKS") or 2),
    )
    router = APIRouter(prefix="/api/clean", tags=["文件清洗"])
    router.add_event_handler("shutdown", manager.shutdown)

    def http_error(exc: Exception) -> HTTPException:
        """服务层异常 → HTTP 状态码。

        这层翻译必须只有一处：路由里凡是**直接**调用服务层的地方（上传那条最典型 —— 它
        边收边写盘，没法塞进 `call`），漏了翻译的结果是「路径不安全」这种明确的用户错误
        变成 500，界面于是只会说「服务器错误」。
        """
        if isinstance(exc, CleanNotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, CleanConflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, CleanError):
            return HTTPException(400, str(exc))
        return HTTPException(500, str(exc))

    async def call(method, *args, **kwargs):
        """把同步的服务层方法放到线程池里跑，并把异常翻译成 HTTP 状态码。

        `with manager.lock` 与兄弟工具一致：服务层的 `lock` 是 `RLock`，且它只在改任务
        记录与端点池时持有，不会跨整个任务运行 —— 一个跑 40 分钟的任务不会让列表接口
        跟着卡住。
        """
        def invoke():
            with manager.lock:
                return method(*args, **kwargs)

        try:
            return await run_in_threadpool(invoke)
        except (CleanError, CleanNotFound, CleanConflict) as exc:
            raise http_error(exc) from exc

    async def call_slow(method, *args, **kwargs):
        """**不持锁**的长操作（打 zip 可能跑几分钟）。

        持锁打 GB 级压缩包的后果是：这几分钟里列表、日志、暂停按钮全部排队等待，界面看起来
        是「服务死了」。服务层自己在需要时读一次记录，不需要外面替它持锁。
        """
        try:
            return await run_in_threadpool(lambda: method(*args, **kwargs))
        except (CleanError, CleanNotFound, CleanConflict) as exc:
            raise http_error(exc) from exc

    # ==================================================================== 元信息

    @router.get("/meta")
    async def meta():
        """界面要用的静态表：状态文案、支持的格式、数据盘剩余空间。

        放在服务端是为了让**同一套文案**同时出现在任务列表、报告与日志里。前端各写一份
        中文映射，最后一定会出现「已完成」与「完成」两种说法。
        """
        return {
            "status_text": dict(STATUS_TEXT),
            "file_status_text": dict(FILE_STATUS_TEXT),
            "extensions": list(INPUT_EXTENSIONS),
            # 上传**没有大小上限**（见 clean_manager 里那段注释），所以这里给的是一句实话：
            # 还能写多少。用户要传几百 GB 之前，先看得见盘还剩多少。
            "storage": {
                "root": str(manager.uploads_dir),
                "free_bytes": free_bytes(manager.uploads_dir),
            },
            "allowed_roots": [str(item) for item in manager.allowed_roots()],
            "endpoints_file": str(manager.pool_file),
            # 生成器目录也走这里，理由和状态文案一样：界面上能选的和后端能跑的必须是同一份
            "generators": generator_catalog(),
            "locales": list(SUPPORTED_LOCALES),
            # 用户函数的词表同理，而且这里更要紧：补全列表要是与运行时的白名单各写一份，
            # 界面就会教用户写 `open` —— 一个必然被闸门拒掉的函数。所以 builtins/modules
            # 直接取自子进程那套 `_ALLOWED_BUILTINS` / `WHITELIST_MODULES`。
            "python": {
                "entry": "transform",
                "formatter": {"available": ruff_command() is not None},
                **vocabulary(),
            },
        }

    # ==================================================================== 数据来源

    @router.get("/browse")
    async def browse(path: str = Query("")):
        return await call(manager.browse, path)

    @router.post("/uploads")
    async def upload(
        files: list[UploadFile] = File(...),
        relative_paths: str = Form("[]"),
    ):
        """上传目录。`relative_paths` 与文件列表一一对应（`webkitdirectory` 给的相对路径）。

        **不设大小上限**（用户传的就是 GB 级目录），改成逐块写盘、边写边看磁盘还剩多少：
        等收完再判，那一次已经把盘写满了。粒度是 `DISK_CHECK_CHUNKS` 个块 —— 每块都问一次
        是没必要的系统调用，漏掉的那点量（约 64MB）相对水位线可以忽略。
        """
        try:
            names = json.loads(relative_paths or "[]")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"relative_paths 不是合法 JSON：{exc}") from exc
        if not isinstance(names, list):
            raise HTTPException(400, "relative_paths 必须是数组")
        upload_id, folder = manager.new_upload()
        entries: list[tuple[str, Path]] = []
        try:
            # 先问一次：盘早就满了的话，哪怕传一个 1KB 的文件也该立刻说清楚，而不是写进去一半
            ensure_disk_space(folder)
            for position, item in enumerate(files):
                raw = str(names[position]) if position < len(names) else (item.filename or "")
                if not raw:
                    raise HTTPException(400, "某个文件既没有相对路径也没有文件名")
                relative = safe_relpath(raw)
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                chunks = 0
                with open(target, "wb") as handle:
                    while chunk := await item.read(CHUNK):
                        chunks += 1
                        if chunks % DISK_CHECK_CHUNKS == 0:
                            ensure_disk_space(folder)
                        handle.write(chunk)
                await item.close()
                entries.append((relative, target))
            if not entries:
                raise HTTPException(400, "没有收到任何文件")
            return await call(manager.save_upload, upload_id, entries)
        except Exception as exc:
            # 半截的上传目录没有价值，而且会一直占着盘
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(exc, (CleanError, CleanNotFound, CleanConflict)):
                raise http_error(exc) from exc
            raise

    @router.get("/uploads/{upload_id}")
    async def upload_info(upload_id: str):
        return await call(manager.upload_info, upload_id)

    @router.delete("/uploads/{upload_id}")
    async def delete_upload(upload_id: str):
        await call(manager.delete_upload, upload_id)
        return {"ok": True}

    # ==================================================================== 探测 / 干跑 / 预估

    @router.post("/inspect")
    async def inspect(payload: dict):
        return await call(manager.inspect, payload)

    @router.post("/validate")
    async def validate(payload: dict):
        """配置干跑：跑前 N 行，返回违规、回退与逐文件明细。

        返回里带 `seed`：提交任务时把它回传，正式任务跑出来的随机字段就与这里确认过的
        逐个相同（见 `CleanManager.create`）。
        """
        return await call(manager.validate, payload)

    @router.post("/estimate")
    async def estimate(payload: dict):
        return await call(manager.estimate, payload)

    # ================================================================ 用户函数（编辑器）

    @router.post("/functions/check")
    async def check_function(payload: dict):
        """源码的静态检查。纯 AST、不起子进程，编辑器按 400ms 防抖来调它也不会打服务。"""
        return await call(manager.check_function, payload)

    @router.post("/functions/format")
    async def format_function(payload: dict):
        """用 ruff 排版。`ok:false` + 原因是**正常返回**（见 manager 里的说明）。"""
        return await call(manager.format_function, payload)

    @router.post("/functions/test")
    async def test_function(payload: dict):
        """拿真实数据的前 N 行试跑一个函数。

        走 `call_slow`（**不持锁**）：它会真起一个子进程跑用户代码，持着锁的后果是这几百
        毫秒里列表、日志、暂停按钮全部排队。调试不改任何任务记录，服务层也不需要外面替它
        持锁。
        """
        return await call_slow(manager.test_function, payload)

    # ==================================================================== 任务

    @router.post("/tasks")
    async def create(payload: dict):
        return await call(manager.create, payload)

    @router.get("/tasks")
    async def tasks():
        return {"tasks": await call(manager.list)}

    @router.get("/tasks/{task_id}")
    async def task(task_id: str):
        item = await call(manager.get, task_id)
        if item is None:
            raise HTTPException(404, "任务不存在")
        return item

    @router.post("/tasks/{task_id}/{action}")
    async def control(task_id: str, action: str):
        if action not in ("pause", "resume", "retry", "cancel"):
            raise HTTPException(404, f"未知操作：{action}")
        return await call(manager.control, task_id, action)

    @router.delete("/tasks/{task_id}")
    async def delete(task_id: str):
        ok = await call(manager.delete, task_id)
        if not ok:
            raise HTTPException(404, "任务不存在")
        return {"ok": True}

    # ==================================================================== 日志与预览

    @router.get("/tasks/{task_id}/logs")
    async def logs(task_id: str, after: int = Query(0, ge=0),
                   limit: int = Query(500, ge=1, le=5000), level: str = Query("info")):
        """历史日志与断线补齐。

        **不是主通道** —— 实时日志走 `/tasks/{id}/events` 的 SSE。这个接口只在两种时候被
        调用：刷新页面后补齐已有日志，以及 SSE 报了 `gap`（漏了 N 条）之后把中间那段捞回。
        """
        return await call(manager.logs, task_id, after=after, limit=limit, level=level)

    @router.get("/tasks/{task_id}/preview")
    async def preview(task_id: str, file: str = Query(""), rows: int = Query(50, ge=1, le=500),
                      side: str = Query("both")):
        if side not in ("both", "before", "after"):
            raise HTTPException(400, "side 只能是 both / before / after")
        return await call(manager.preview, task_id, file=file, rows=rows, side=side)

    @router.get("/tasks/{task_id}/report")
    async def report(task_id: str):
        text = await call(manager.report, task_id)
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @router.get("/tasks/{task_id}/stats")
    async def stats(task_id: str):
        """字段统计（清洗前 / 清洗后）的 JSON —— 报告是给人读的，这个是给表格用的。"""
        return await call(manager.stats, task_id)

    @router.get("/tasks/{task_id}/download")
    async def download(task_id: str):
        item = await call(manager.get, task_id)
        if item is None:
            raise HTTPException(404, "任务不存在")
        path = await call_slow(manager.archive, task_id)
        name = f"{item.get('name') or '清洗结果'}-{task_id[:8]}.zip"
        return FileResponse(path, media_type="application/zip", filename=name)

    # ==================================================================== SSE

    def _cursor(request: Request, after: int) -> int:
        """断点续传的起点：`Last-Event-ID`（浏览器自动带）优先于查询参数。

        浏览器重连时只会带这个头，所以它是**唯一**能让重连不丢事件的东西；`?after=` 是给
        初始订阅用的。
        """
        header = request.headers.get("last-event-id") or ""
        if header.strip().isdigit():
            return int(header.strip())
        return int(after or 0)

    async def _stream(request: Request, task_id: str, after: int, level: str):
        bus = manager.bus
        sub = bus.subscribe(task_id, min_level=level)
        try:
            cursor = _cursor(request, after)
            if cursor:
                history, gap = bus.replay(cursor, task_id, limit=1000, min_level=level)
                for event in history:
                    yield event.sse()
                if gap:
                    # 环形缓冲已经被绕过去了：告诉前端「你漏了 N 条，去 /logs 补齐」，
                    # 而不是让它以为中间什么都没发生。
                    yield {
                        "event": "resync",
                        "id": str(cursor),
                        "data": json.dumps({"gap": gap}, ensure_ascii=False),
                    }
            while True:
                yield (await sub.get()).sse()
        finally:
            # 客户端断开时 sse-starlette 会取消这个生成器，`finally` 负责退订 ——
            # 不退订的话每关一个页面就留下一个永远没人读的队列。
            sub.close()

    @router.get("/events")
    async def events(request: Request, after: int = Query(0, ge=0), level: str = Query("info")):
        """全局流：所有任务的状态变化与进度（每任务节流 ~2/s）。

        工具页常驻这一条，用来驱动任务列表与进度条 —— 于是它**不需要轮询**。
        """
        return EventSourceResponse(_stream(request, "", after, level), ping=15)

    @router.get("/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request, after: int = Query(0, ge=0),
                          level: str = Query("info")):
        """单任务流：日志行 + 详细进度 + ETA + 每端点指标。打开某个任务时才订阅。"""
        return EventSourceResponse(_stream(request, task_id, after, level), ping=15)

    # ==================================================================== 端点池

    @router.get("/llm/endpoints")
    async def endpoints():
        return await call(manager.endpoints)

    @router.post("/llm/endpoints")
    async def save_endpoints(payload: dict):
        return await call(manager.save_endpoints, payload)

    @router.delete("/llm/endpoints/{endpoint_id}")
    async def delete_endpoint(endpoint_id: str):
        current = await call(manager.endpoints)
        keep = [item for item in current["endpoints"] if item.get("id") != endpoint_id]
        if len(keep) == len(current["endpoints"]):
            raise HTTPException(404, "端点不存在")
        await call(manager.save_endpoints, {"endpoints": keep})
        return {"ok": True}

    @router.post("/llm/endpoints/test")
    async def test_endpoint(payload: dict):
        """连通性测试。**失败也是一种结果**（返回 `ok: false`），不是 HTTP 错误 ——
        用户点这个按钮就是想知道「通不通」，用 4xx/5xx 表达等于让界面去猜原因。"""
        return await call(manager.test_endpoint, payload)

    @router.post("/llm/endpoints/{endpoint_id}/test")
    async def test_saved_endpoint(endpoint_id: str):
        current = await call(manager.endpoints)
        found = next((item for item in current["endpoints"]
                      if item.get("id") == endpoint_id), None)
        if found is None:
            raise HTTPException(404, "端点不存在")
        return await call(manager.test_endpoint, {"endpoint": found})

    return router


__all__ = ["make_router"]
