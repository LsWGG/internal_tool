import json
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import Response
from urllib.parse import quote
from starlette.concurrency import run_in_threadpool

from .word_batch_manager import WordBatchManager
from .word_batch_examples import example_file

DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def make_router(root):
    manager = WordBatchManager(root)
    router = APIRouter(prefix='/api/word-batch', tags=['Word 批量生成'])
    router.add_event_handler('shutdown', lambda: manager.pool.shutdown(wait=True))

    @router.get('/examples/{kind}')
    async def example(kind: str):
        formats = {'word': ('Word示例模板.docx', DOCX_MIME),
                   'excel': ('Excel示例数据.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
        if kind not in formats:
            raise HTTPException(404, '示例不存在')
        filename, media_type = formats[kind]
        content = await run_in_threadpool(example_file, kind)
        return Response(content, media_type=media_type, headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename)})

    async def call(method, *args):
        try:
            def invoke():
                with manager.lock:
                    return method(*args)
            return await run_in_threadpool(invoke)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc

    async def read(upload, maximum):
        content = bytearray()
        try:
            while chunk := await upload.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > maximum:
                    raise HTTPException(413, f'{upload.filename} 超过大小限制')
            return bytes(content)
        finally:
            await upload.close()

    @router.post('/workspaces')
    async def prepare(template: UploadFile = File(...), excel: UploadFile = File(...), images: list[UploadFile] = File(default=[])):
        if len(images) > 1000:
            raise HTTPException(413, '最多上传 1000 张图片')
        template_data = await read(template, 20 * 1024 * 1024)
        excel_data = await read(excel, 30 * 1024 * 1024)
        files, total = [], 0
        for image in images:
            content = await read(image, 100 * 1024 * 1024)
            total += len(content)
            if total > 200 * 1024 * 1024:
                raise HTTPException(413, '图片上传合计不能超过 200MB')
            files.append((image.filename or '', content))
        return await call(manager.prepare, template.filename or '', template_data, excel.filename or '', excel_data, files)

    @router.delete('/workspaces/{ident}')
    async def delete_workspace(ident: str):
        await call(manager.delete, ident, True)
        return {'ok': True}

    @router.post('/workspaces/{ident}/preview')
    async def preview(ident: str, config: dict):
        name = await call(manager.preview, ident, config)
        return {'url': f'/api/word-batch/workspaces/{ident}/preview/{name}'}

    @router.get('/workspaces/{ident}/preview/{name}')
    async def preview_file(ident: str, name: str):
        if not re.fullmatch(r'preview_[0-9a-f]{32}\.docx', name):
            raise HTTPException(404, '预览不存在')
        folder = await call(manager.path, ident)
        path = folder / name
        if not path.is_file():
            raise HTTPException(404, '预览已删除')
        return FileResponse(path, media_type=DOCX_MIME)

    @router.post('/workspaces/{ident}/tasks')
    async def create(ident: str, config: dict):
        return await call(manager.create, ident, config)

    @router.get('/tasks')
    async def tasks():
        return await call(manager.list_tasks)

    @router.get('/tasks/{ident}/results')
    async def results(ident: str, page: int = Query(1, ge=1)):
        return await call(manager.results, ident, page)

    @router.get('/tasks/{ident}/export')
    async def export(ident: str):
        path = await call(manager.result_file, ident)
        return FileResponse(path, media_type='application/zip', filename='Word批量生成.zip')

    @router.get('/tasks/{ident}/files/{index}')
    async def result_file(ident: str, index: int):
        path = await call(manager.result_file, ident, index)
        return FileResponse(path, media_type=DOCX_MIME, filename=path.name)

    @router.delete('/tasks/{ident}')
    async def delete(ident: str):
        await call(manager.delete, ident)
        return {'ok': True}

    @router.post('/tasks/{ident}/retry')
    async def retry(ident: str):
        return await call(manager.retry, ident)

    return router
