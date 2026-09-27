"""挂载 `/api/links`：工具首页那些外链工具的 CRUD（设置弹窗在用）。

与 clean 端点池同一套动作与语义（`clean_api.py` 的 `/llm/endpoints`）：
GET 列表、POST 整表替换、DELETE 单条 —— 编辑的场景就是弹窗里那张小表。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .links_manager import LinksError, LinksManager, LinksNotFound


def make_router(root: Path | str, probe=None) -> APIRouter:
    manager = LinksManager(root, probe=probe)
    router = APIRouter(prefix="/api/links", tags=["外链工具"])

    def http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, LinksNotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, LinksError):
            return HTTPException(400, str(exc))
        return HTTPException(500, str(exc))

    async def call(method, *args):
        """同步的服务层方法放到线程池里跑，异常翻译成 HTTP 状态码。"""
        def invoke():
            with manager.lock:
                return method(*args)

        try:
            return await run_in_threadpool(invoke)
        except (LinksError, LinksNotFound) as exc:
            raise http_error(exc) from exc

    # 路径写 ""：prefix 是 /api/links，写 "/" 会变成 /api/links/ 并对前者 307 跳转，
    # 而前端（还有别人的 curl）只会请求 /api/links。
    @router.get("")
    async def list_links() -> dict[str, Any]:
        return await call(manager.list)

    @router.post("")
    async def save_links(payload: dict) -> dict[str, Any]:
        return await call(manager.save, payload)

    @router.delete("/{link_id}")
    async def delete_link(link_id: str) -> dict[str, Any]:
        return await call(manager.delete, link_id)

    # 单独一条的重新检测。探测走的也是线程池（`call` 里的 run_in_threadpool），
    # 所以这条请求最多占住一个工作线程 4 秒，不会卡住事件循环。
    @router.post("/{link_id}/probe")
    async def probe_link(link_id: str) -> dict[str, Any]:
        return await call(manager.probe_one, link_id)

    return router
