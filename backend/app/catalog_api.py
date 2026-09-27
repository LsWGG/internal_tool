"""挂载 `/api/catalog`：工具首页的分类表（排序、改名、换图标、工具换组），设置弹窗在用。

只有两条路由：GET 读、POST 整表替换 —— 与 links / clean 端点池同一套动作。**没有单条
DELETE**：分类没有独立于列表的身份，排序本身就要重写整张列表，再开一条写路径就等于在
同一份状态上维护第二套乐观更新与回滚。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .catalog_manager import CatalogError, CatalogManager


def make_router(root: Path | str) -> APIRouter:
    manager = CatalogManager(root)
    router = APIRouter(prefix="/api/catalog", tags=["工具分类"])

    def http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, CatalogError):
            return HTTPException(400, str(exc))
        return HTTPException(500, str(exc))

    async def call(method, *args):
        """同步的服务层方法放到线程池里跑，异常翻译成 HTTP 状态码。"""
        def invoke():
            with manager.lock:
                return method(*args)

        try:
            return await run_in_threadpool(invoke)
        except CatalogError as exc:
            raise http_error(exc) from exc

    # 路径写 ""：prefix 是 /api/catalog，写 "/" 会变成 /api/catalog/ 并对前者 307 跳转，
    # 而前端只会请求 /api/catalog（`links_api.py` 同处的注释是同一条）。
    @router.get("")
    async def get_catalog() -> dict[str, Any]:
        return await call(manager.get)

    @router.post("")
    async def save_catalog(payload: dict) -> dict[str, Any]:
        return await call(manager.save, payload)

    return router
