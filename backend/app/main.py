from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

import requests
from fastapi import FastAPI, HTTPException, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .docker_manager import DockerTaskManager
from .gjb_manager import GJBTaskManager
from .es_manager import ESClient, ESTaskManager
from .nebula_manager import NebulaTaskManager
from .models import BatchCreate, CreateTask, DockerCreateTask, ESConnection, ESExportTask, NebulaConnection, NebulaExportTask
from .task_manager import TaskManager
from .shp_preview_manager import ShpPreviewManager
from .tile_preview_manager import TilePreviewManager
from .github_trending_manager import GitHubTrendingManager
from .pdf_manager import PDFTaskManager
from .md_word_manager import MarkdownWordManager
from .image_convert_manager import ImageConvertManager
from .database_manager import DatabaseClient, DatabaseTaskManager
from .propzone_manager import PropZoneTaskManager
from .crawler_manager import CrawlerTaskManager
from .word_batch_api import make_router as make_word_batch_router
from .ai_native import chat as ai_chat, capabilities as ai_capabilities
from .ai_capabilities import validate_action
import json
import zipfile

app = FastAPI(title="地图数据下载服务", version="1.0.0")
app.include_router(make_word_batch_router(Path(__file__).resolve().parents[1] / 'word_batch_data'))
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
manager = TaskManager(Path(__file__).resolve().parents[1] / "data")
docker_manager = DockerTaskManager(
    Path(__file__).resolve().parents[1] / "docker_data",
    Path(__file__).resolve().parents[1] / "download_docker_offline.sh",
)
gjb_manager = GJBTaskManager(Path(__file__).resolve().parents[1] / "gjb_data")
es_manager = ESTaskManager(Path(__file__).resolve().parents[1] / "es_data")
nebula_manager = NebulaTaskManager(Path(__file__).resolve().parents[1] / "nebula_data",
                                   Path(__file__).resolve().parent / "nebula_data_util.py")
shp_preview_manager = ShpPreviewManager(Path(__file__).resolve().parents[1] / "shp_preview_data")
tile_preview_manager = TilePreviewManager(Path(__file__).resolve().parents[1] / "tile_preview_data")
github_trending_manager = GitHubTrendingManager(Path(__file__).resolve().parents[1] / "github_trending_data")
github_trending_manager.start_daily()
pdf_manager = PDFTaskManager(Path(__file__).resolve().parents[1] / "pdf_data")
md_word_manager = MarkdownWordManager(
    Path(__file__).resolve().parents[1] / "md_word_data",
    Path(__file__).resolve().parent / "assets" / "md_word_reference.docx",
)
image_convert_manager = ImageConvertManager(
    Path(__file__).resolve().parents[1] / "image_convert_data"
)
database_task_manager = DatabaseTaskManager(
    Path(__file__).resolve().parents[1] / "database_data"
)
propzone_manager = PropZoneTaskManager(Path(__file__).resolve().parents[1] / "propzone_data")
crawler_manager = CrawlerTaskManager(Path(__file__).resolve().parents[1] / "crawler_data")
sqlite_workspace_dir = database_task_manager.data_dir / "sqlite_workspaces"
sqlite_workspace_dir.mkdir(parents=True, exist_ok=True)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/ai/capabilities")
def get_ai_capabilities():
    return ai_capabilities()


@app.post("/api/ai/chat")
def post_ai_chat(payload: dict):
    try:
        return ai_chat(payload)
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/ai/actions/execute")
def execute_ai_action(payload: dict):
    """Validate and dispatch one normalized AI action through a small adapter set."""
    raw = payload.get("action") if isinstance(payload.get("action"), dict) else payload
    try:
        action = validate_action(raw.get("operation"), raw.get("tool_id"), raw.get("parameters") or {})
    except (AttributeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if action["requires_confirmation"] and payload.get("confirmed") is not True:
        raise HTTPException(409, "该操作需要用户确认")

    operation, tool_id, values = action["operation"], action["tool_id"], action["parameters"]
    if operation == "create" and tool_id == "pdf":
        task = pdf_manager.create(values["urls"], values.get("filename_template", "{index}_{host}"))
        return {"message": f"已创建网页转 PDF 任务，共 {len(values['urls'])} 个网址。", "tool_id": tool_id, "task": task}
    if operation == "create" and tool_id == "map":
        regions = search_regions(values["region_query"])
        if not regions:
            raise HTTPException(404, f"没有找到“{values['region_query']}”对应的行政区")
        normalized = re.sub(r"[省市区县州\s]", "", values["region_query"]).lower()
        region = next((item for item in regions if re.sub(r"[省市区县州\s]", "", item["name"].split(",")[0]).lower() == normalized), regions[0])
        map_options = {key: value for key, value in values.items() if key != "region_query"}
        request = CreateTask.model_validate({
            "bounds": region["bounds"], "geometry": region["geometry"],
            "options": {**map_options, "output_name": region["name"].split(",")[0]},
        })
        task = manager.create(request)
        return {"message": f"已创建地图下载任务：{request.options.output_name} · Z{request.options.zoom_min}–Z{request.options.zoom_max}。", "tool_id": tool_id, "task": task}
    if operation == "create" and tool_id == "crawler":
        field_names = values.pop("fields", [])
        defaults = crawler_manager.builtin_fields(
            values.get("source", "generic"), values.get("twitter_mode", "keyword"),
            values.get("tiktok_mode", "keyword"), values.get("telegram_mode", "channel"),
        )
        default_by_name = {field.get("name"): field for field in defaults}
        fields = [default_by_name.get(name, {"name": name, "selector": "", "attribute": "text", "builtin": False}) for name in field_names]
        crawler_payload = {
            "urls": values.get("urls", []), "keyword": values.get("keyword", ""),
            "account_name": values.get("account_name", ""), "source": values.get("source", "generic"),
            "twitter_mode": values.get("twitter_mode", "keyword"), "tiktok_mode": values.get("tiktok_mode", "keyword"),
            "telegram_mode": values.get("telegram_mode", "channel"), "fields": fields or defaults,
            "output_format": values.get("output_format", "xlsx"), "max_items": values.get("max_items", 50),
            "interval_minutes": max(0, int(values.get("interval", 0) or 0)),
            "cron": values.get("cron", "") if values.get("interval") == -1 else "",
            "download_videos": values.get("download_videos", False),
            "youtube_quality": values.get("quality", 720), "delay_seconds": 1, "dynamic": True,
            "proxies": [], "sql_sink": {"enabled": False},
        }
        try:
            task = crawler_manager.create(crawler_payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"message": f"已创建采集任务：{task.get('name')}。", "tool_id": tool_id, "task": crawler_manager.public_task(task)}

    if operation == "query":
        if tool_id == "trending":
            date = values.get("date")
            if date in ("today", "今天", "current"):
                date = None
            if not date:
                reports = github_trending_manager.dates()
                date = reports[0]["date"] if reports else github_trending_manager._today()
            report = github_trending_manager.get(date)
            if not report:
                try:
                    report = github_trending_manager.refresh()
                except Exception as exc:
                    raise HTTPException(502, f"热门榜单暂时无法读取：{exc}") from exc
            repositories = report.get("repositories", [])[:10]
            lines = [f"{item.get('rank', index)}. {item.get('full_name') or item.get('name')} — {item.get('description_zh') or item.get('description') or '暂无简介'}" for index, item in enumerate(repositories, 1)]
            message = f"GitHub 热门榜单 · {report.get('date')}（共 {report.get('repository_count', len(report.get('repositories', [])))} 个）"
            if lines:
                message += "\n" + "\n".join(lines)
            return {"message": message, "tool_id": tool_id, "data": {"date": report.get("date"), "repositories": repositories}}

        query_managers = {
            "map": manager, "pdf": pdf_manager, "docker": docker_manager,
            "gjb": gjb_manager, "es": es_manager, "nebula": nebula_manager,
            "database": database_task_manager, "image-convert": image_convert_manager,
            "md-word": md_word_manager, "propzone": propzone_manager, "crawler": crawler_manager,
        }
        query_manager = query_managers.get(tool_id)
        if query_manager:
            task_id = values.get("task_id")
            if task_id:
                task = query_manager.get(task_id)
                tasks = [task] if task else []
            else:
                tasks = query_manager.list()[:10]
            if not tasks:
                return {"message": "当前没有可展示的任务记录。", "tool_id": tool_id, "tasks": []}
            summaries = []
            for task in tasks:
                request = task.get("request") or {}
                name = task.get("name") or (request.get("options") or {}).get("output_name") or f"任务 {task.get('id', '')[:8]}"
                progress = task.get("progress")
                summaries.append({"id": task.get("id"), "name": name, "status": task.get("status"), "progress": progress, "message": task.get("message"), "error": task.get("error")})

            task_lines = []
            for item in summaries:
                progress_text = f" · {item['progress']}%" if item["progress"] is not None else ""
                task_lines.append(f"{item['name']}（{item['id'][:8]}）— {item['status']}{progress_text} · {item.get('message') or ''}")
            message = "最近任务：\n" + "\n".join(task_lines)
            return {"message": message, "tool_id": tool_id, "tasks": summaries}
        if tool_id == "shp":
            layers = shp_preview_manager.list()
            return {"message": f"当前已加载 {len(layers)} 个 SHP 图层。", "tool_id": tool_id, "layers": layers}
        return {"message": "该工具的数据保存在浏览器当前页面中，已为你打开对应页面。", "tool_id": tool_id}

    task_manager = manager if tool_id == "map" else pdf_manager if tool_id == "pdf" else None
    if task_manager is None:
        raise HTTPException(400, "该工具尚未接入 AI 执行适配器")
    task_id = values["task_id"]
    if operation == "export":
        task = task_manager.get(task_id)
        if not task or task.get("status") != "completed":
            raise HTTPException(409, "任务尚未完成，暂时不能下载")
        prefix = "/api/tasks" if tool_id == "map" else "/api/pdf/tasks"
        return {"message": "结果已准备好，可以下载。", "tool_id": tool_id, "task": task, "download_url": f"{prefix}/{task_id}/export"}
    if operation == "control":
        control = values["action"]
        try:
            if control == "delete":
                if not task_manager.delete(task_id):
                    raise HTTPException(404, "任务不存在")
                return {"message": "任务及其结果文件已删除。", "tool_id": tool_id, "deleted": True}
            if tool_id == "pdf":
                if control != "retry":
                    raise HTTPException(400, "网页转 PDF 仅支持重试或删除")
                task = task_manager.retry(task_id)
            else:
                task = getattr(task_manager, control)(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not task:
            raise HTTPException(404, "任务不存在")
        return {"message": "任务操作已执行。", "tool_id": tool_id, "task": task}
    raise HTTPException(400, "当前操作无需调用执行接口")


def _database_config(config_text):
    try:
        config = json.loads(config_text) if isinstance(config_text, str) else config_text
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "数据库配置不是有效 JSON") from exc
    if not isinstance(config, dict):
        raise HTTPException(400, "数据库配置格式错误")
    return config


def _sqlite_workspace_file(workspace_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", workspace_id):
        raise HTTPException(404, "SQLite 工作副本不存在")
    workspace = sqlite_workspace_dir / workspace_id
    files = [path for path in workspace.iterdir()] if workspace.is_dir() else []
    file = next((path for path in files if path.is_file()), None)
    if not file:
        raise HTTPException(404, "SQLite 工作副本不存在")
    return file


def _validate_sqlite_file(path: Path):
    path = path.expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        raise HTTPException(400, "请选择 .db、.sqlite 或 .sqlite3 文件")
    try:
        with path.open("rb") as source:
            header = source.read(16)
    except OSError as exc:
        raise HTTPException(400, f"无法读取 SQLite 文件：{exc}") from exc
    if header != b"SQLite format 3\x00":
        raise HTTPException(400, "所选文件不是有效的 SQLite 数据库")
    return path


@app.post("/api/database/sqlite/select")
def select_sqlite_database():
    """Open a native picker on the machine running this local web service."""
    try:
        if sys.platform == "darwin":
            command = ["/usr/bin/osascript", "-e", 'POSIX path of (choose file with prompt "选择 SQLite 数据库文件")']
        elif sys.platform.startswith("win"):
            script = "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Title='选择 SQLite 数据库文件'; $d.Filter='SQLite (*.db;*.sqlite;*.sqlite3)|*.db;*.sqlite;*.sqlite3'; if($d.ShowDialog() -eq 'OK'){$d.FileName}else{exit 2}"
            command = ["powershell", "-NoProfile", "-Command", script]
        elif shutil.which("zenity"):
            command = ["zenity", "--file-selection", "--title=选择 SQLite 数据库文件", "--file-filter=SQLite | *.db *.sqlite *.sqlite3"]
        else:
            raise HTTPException(501, "当前系统没有可用的原生文件选择器")
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(408, "文件选择已超时") from exc
    except OSError as exc:
        raise HTTPException(501, f"无法打开系统文件选择器：{exc}") from exc
    if result.returncode != 0:
        raise HTTPException(409, "已取消选择文件")
    path = _validate_sqlite_file(Path(result.stdout.strip()))
    return {"name": path.name, "path": str(path), "size": path.stat().st_size, "direct": True}


@app.post("/api/database/sqlite/upload", status_code=201)
async def upload_sqlite_database(file: UploadFile = File(...)):
    filename = Path(file.filename or "database.sqlite").name
    if Path(filename).suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        raise HTTPException(400, "请选择 .db、.sqlite 或 .sqlite3 文件")
    workspace_id = __import__("uuid").uuid4().hex
    workspace = sqlite_workspace_dir / workspace_id
    workspace.mkdir(parents=True)
    target = workspace / filename
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "SQLite 文件不能超过 2GB")
                output.write(chunk)
        _validate_sqlite_file(target)
        connection = __import__("sqlite3").connect(target)
        try:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        finally:
            connection.close()
        return {"id": workspace_id, "name": filename, "path": str(target.resolve()), "size": size,
                "export_url": f"/api/database/sqlite/files/{workspace_id}/export"}
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


@app.get("/api/database/sqlite/files/{workspace_id}/export")
def export_sqlite_database(workspace_id: str):
    path = _sqlite_workspace_file(workspace_id)
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name)


@app.delete("/api/database/sqlite/files/{workspace_id}", status_code=204)
def delete_sqlite_database(workspace_id: str):
    path = _sqlite_workspace_file(workspace_id)
    shutil.rmtree(path.parent, ignore_errors=True)


@app.post("/api/database/test")
def database_test(payload: dict):
    client = None
    try:
        client = DatabaseClient(payload.get("config", payload))
        return {"connection": client.ping(), "tables": client.tables()}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if client:
            client.close()


@app.post("/api/database/schema")
def database_schema(payload: dict):
    client = None
    try:
        client = DatabaseClient(payload.get("config", {}))
        table = payload.get("table")
        return client.schema(table) if table else {"tables": client.tables()}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if client:
            client.close()


@app.post("/api/database/rows")
def database_rows(payload: dict):
    client = None
    try:
        client = DatabaseClient(payload.get("config", {}))
        table = payload.get("table", "")
        return {"rows": client.rows(table, payload.get("limit", 50), payload.get("offset", 0)),
                "total": client.count(table)}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if client:
            client.close()


@app.post("/api/database/crud")
def database_crud(payload: dict):
    action = payload.get("action", "read")
    client = None
    try:
        client = DatabaseClient(payload.get("config", {}))
        return client.crud(action, payload.get("table", ""), payload.get("values"), payload.get("where"))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if client:
            client.close()


@app.get("/api/database/tasks")
def list_database_tasks():
    return database_task_manager.list()


@app.post("/api/database/tasks/export", status_code=202)
def create_database_export_task(payload: dict):
    config = payload.get("config", {})
    try:
        DatabaseClient(config)._validate(config)
        tables = payload.get("tables") or []
        return database_task_manager.create("export", config, tables)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/database/tasks/import", status_code=202)
async def create_database_import_task(
    config: str = Form(...), archive: UploadFile = File(...), overwrite: bool = Form(False),
):
    parsed = _database_config(config)
    if not archive.filename or not archive.filename.lower().endswith((".zip", ".json")):
        raise HTTPException(400, "请选择数据库备份 ZIP 或 JSON 文件")
    upload_root = database_task_manager.data_dir / f"upload-{__import__('uuid').uuid4().hex}"
    upload_root.mkdir(parents=True)
    try:
        target = upload_root / Path(archive.filename).name
        size = 0
        with target.open("wb") as output:
            while chunk := await archive.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "备份文件不能超过 2GB")
                output.write(chunk)
        return database_task_manager.create("import", parsed, [], overwrite, upload_root)
    except Exception as exc:
        shutil.rmtree(upload_root, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/database/tasks/{task_id}/retry", status_code=202)
def retry_database_task(task_id: str):
    try:
        task = database_task_manager.retry(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/database/tasks/{task_id}", status_code=204)
def delete_database_task(task_id: str):
    if not database_task_manager.delete(task_id):
        raise HTTPException(404, "任务不存在")


@app.get("/api/database/tasks/{task_id}/export")
def export_database_task(task_id: str):
    task = database_task_manager.get(task_id)
    if not task or task.get("status") != "completed" or not task.get("result", {}).get("archive"):
        raise HTTPException(404, "导出任务未完成或不存在")
    path = database_task_manager.data_dir / task["result"]["archive"]
    if not path.is_file():
        raise HTTPException(404, "备份文件不存在")
    return FileResponse(path, media_type="application/zip", filename=f"{task['name']}.zip")


@app.get("/api/image-convert/tasks")
def list_image_convert_tasks():
    return image_convert_manager.list()


@app.post("/api/image-convert/tasks", status_code=202)
async def create_image_convert_task(
    files: list[UploadFile] = File(...),
    output_format: str = Form("jpeg"),
    quality: int = Form(92),
):
    if not files:
        raise HTTPException(400, "请选择需要转换的图片")
    if len(files) > 5000:
        raise HTTPException(413, "单次最多上传 5000 张图片")
    upload_root = image_convert_manager.data_dir / f"upload-{__import__('uuid').uuid4().hex}"
    upload_root.mkdir(parents=True)
    relative_files = []
    total_size = 0
    try:
        for upload in files:
            raw_name = (upload.filename or "").replace("\\", "/")
            path = PurePosixPath(raw_name)
            parts = path.parts
            if (not raw_name or raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name)
                    or any(part in ("", ".", "..") for part in parts)):
                raise HTTPException(400, f"文件路径不安全：{raw_name or '未命名文件'}")
            if path.suffix.lower() not in image_convert_manager.INPUT_EXTENSIONS:
                raise HTTPException(400, f"不支持的图片格式：{path.name}")
            relative = PurePosixPath(*parts)
            target = upload_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            duplicate_index = 2
            while target.exists():
                target = target.with_name(f"{target.stem}_{duplicate_index}{target.suffix}")
                duplicate_index += 1
            file_size = 0
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if file_size > 500 * 1024 * 1024:
                        raise HTTPException(413, f"单个图片不能超过 500MB：{path.name}")
                    if total_size > 2 * 1024 * 1024 * 1024:
                        raise HTTPException(413, "单次上传总大小不能超过 2GB")
                    output.write(chunk)
            if not file_size:
                raise HTTPException(400, f"图片文件为空：{path.name}")
            relative_files.append(target.relative_to(upload_root).as_posix())
        try:
            return image_convert_manager.create(upload_root, relative_files, output_format, quality)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        shutil.rmtree(upload_root, ignore_errors=True)


@app.post("/api/image-convert/tasks/{task_id}/retry", status_code=202)
def retry_image_convert_task(task_id: str):
    try:
        task = image_convert_manager.retry(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/image-convert/tasks/{task_id}", status_code=204)
def delete_image_convert_task(task_id: str):
    if not image_convert_manager.delete(task_id):
        raise HTTPException(404, "任务不存在")


@app.get("/api/image-convert/tasks/{task_id}/export")
def export_image_convert_task(task_id: str):
    task = image_convert_manager.get(task_id)
    if not task or task.get("status") != "completed" or not task.get("result"):
        raise HTTPException(404, "任务未完成或不存在")
    archive = image_convert_manager.data_dir / task["result"]["archive"]
    if not archive.is_file():
        raise HTTPException(404, "转换结果不存在")
    return FileResponse(archive, media_type="application/zip", filename=f"{task['name']}.zip")


@app.get("/api/md-word/tasks")
def list_md_word_tasks():
    return md_word_manager.list()


@app.post("/api/md-word/tasks", status_code=202)
async def create_md_word_task(
    file: UploadFile = File(...),
    title: str = Form(""),
    toc: bool = Form(True),
    mermaid_theme: str = Form("neutral"),
):
    try:
        content = await file.read()
        return md_word_manager.create(file.filename or "document.md", content, title, toc, mermaid_theme)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"创建转换任务失败：{exc}") from exc


@app.post("/api/md-word/tasks/{task_id}/retry", status_code=202)
def retry_md_word_task(task_id: str):
    try:
        task = md_word_manager.retry(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.delete("/api/md-word/tasks/{task_id}", status_code=204)
def delete_md_word_task(task_id: str):
    if not md_word_manager.delete(task_id):
        raise HTTPException(404, "任务不存在")


@app.get("/api/md-word/tasks/{task_id}/preview")
def preview_md_word_task(task_id: str):
    task = md_word_manager.get(task_id)
    if not task or task.get("status") != "completed" or not task.get("result"):
        raise HTTPException(404, "任务未完成或不存在")
    path = md_word_manager.data_dir / task_id / task["result"]["preview"]
    if not path.is_file():
        raise HTTPException(404, "预览文件不存在")
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/md-word/tasks/{task_id}/export")
def export_md_word_task(task_id: str):
    task = md_word_manager.get(task_id)
    if not task or task.get("status") != "completed" or not task.get("result"):
        raise HTTPException(404, "任务未完成或不存在")
    path = md_word_manager.data_dir / task_id / task["result"]["docx"]
    if not path.is_file():
        raise HTTPException(404, "Word 文件不存在")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )

@app.get('/api/pdf/tasks')
def list_pdf_tasks(): return pdf_manager.list()

@app.post('/api/pdf/tasks', status_code=202)
async def create_pdf_task(urls: str = Form(''), excel: UploadFile | None = File(None), url_column: str = Form(''), filename_template: str = Form('{index}_{host}')):
    upload = None
    try:
        if excel and excel.filename:
            upload = pdf_manager.data_dir / f'upload-{__import__("uuid").uuid4().hex}.xlsx'
            upload.write_bytes(await excel.read())
            return pdf_manager.create_from_excel(upload, url_column or None, filename_template)
        return pdf_manager.create(urls.splitlines(), filename_template)
    except Exception as exc: raise HTTPException(400, str(exc)) from exc
    finally:
        if upload: upload.unlink(missing_ok=True)

@app.delete('/api/pdf/tasks/{task_id}', status_code=204)
def delete_pdf_task(task_id: str):
    if not pdf_manager.delete(task_id): raise HTTPException(404, '任务不存在')

@app.post('/api/pdf/tasks/{task_id}/retry', status_code=202)
def retry_pdf_task(task_id: str):
    try:
        task=pdf_manager.retry(task_id)
        if not task: raise HTTPException(404,'任务不存在')
        return task
    except ValueError as exc: raise HTTPException(409,str(exc)) from exc

@app.get('/api/pdf/tasks/{task_id}/preview/{filename}')
def preview_pdf_file(task_id: str, filename: str):
    task=pdf_manager.get(task_id);path=(pdf_manager.data_dir/task_id/filename).resolve()
    if not task or task['status']!='completed' or path.parent!=(pdf_manager.data_dir/task_id).resolve() or not path.exists(): raise HTTPException(404,'文件不存在')
    return FileResponse(path,media_type='application/pdf')

@app.get('/api/pdf/tasks/{task_id}/export')
def export_pdf_task(task_id: str):
    task=pdf_manager.get(task_id)
    if not task or task['status']!='completed': raise HTTPException(404,'任务未完成或不存在')
    return FileResponse(pdf_manager.data_dir/f'{task_id}.zip',filename=f'{task["name"]}.zip')


@app.get("/api/github-trending/reports")
def list_github_trending_reports(): return github_trending_manager.dates()


@app.get("/api/github-trending/favorites")
def list_github_trending_favorites(): return github_trending_manager.favorites()


@app.post("/api/github-trending/favorites", status_code=201)
def create_github_trending_favorite(payload: dict):
    try: return github_trending_manager.save_favorite(payload.get("repository"), payload.get("source_date", ""))
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc


@app.delete("/api/github-trending/favorites/{owner}/{name}", status_code=204)
def delete_github_trending_favorite(owner: str, name: str):
    if not github_trending_manager.remove_favorite(owner, name):
        raise HTTPException(404, "收藏记录不存在")


@app.get("/api/github-trending/reports/{date}")
def get_github_trending_report(date: str):
    report = github_trending_manager.get(date)
    if not report: raise HTTPException(404, "指定日期的热门报告不存在")
    return report


@app.post("/api/github-trending/refresh")
def refresh_github_trending_report():
    try: return github_trending_manager.refresh()
    except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc
    except Exception as exc: raise HTTPException(502, f"GitHub 热门榜单获取失败：{exc}") from exc


@app.get("/api/shp-preview/layers")
def list_shp_preview_layers(): return shp_preview_manager.list()


@app.post("/api/shp-preview/layers", status_code=201)
async def create_shp_preview_layers(files: list[UploadFile] = File(...)):
    upload_root = shp_preview_manager.data_dir / f"upload-{__import__('uuid').uuid4().hex}"
    upload_root.mkdir(parents=True)
    groups, total = {}, 0
    try:
        for upload in files:
            name = Path(upload.filename or "").name
            suffix = Path(name).suffix.lower()
            if suffix not in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                continue
            stem = Path(name).stem
            group = groups.setdefault(stem, upload_root / stem)
            group.mkdir(exist_ok=True)
            target = group / f"{stem}{suffix}"
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > 300 * 1024 * 1024: raise HTTPException(413, "单次上传不能超过 300MB")
                    output.write(chunk)
        if not groups: raise HTTPException(400, "请选择 SHP 文件组")
        missing = [stem for stem, path in groups.items()
                   if not all((path / f"{stem}{suffix}").exists() for suffix in (".shp", ".shx", ".dbf", ".prj"))]
        if missing: raise HTTPException(400, "以下文件组不完整（需要 SHP/SHX/DBF/PRJ）：" + "、".join(missing))
        created = []
        try:
            for stem, path in groups.items():
                created.append(shp_preview_manager.create(path, stem))
            return created
        except Exception:
            for item in created: shp_preview_manager.delete(item["id"])
            raise
    finally:
        shutil.rmtree(upload_root, ignore_errors=True)


@app.get("/api/shp-preview/layers/{layer_id}/geojson")
def get_shp_preview_geojson(layer_id: str):
    if not shp_preview_manager.get(layer_id): raise HTTPException(404, "图层不存在")
    return FileResponse(shp_preview_manager.data_dir/layer_id/"preview.geojson", media_type="application/geo+json")


@app.delete("/api/shp-preview/layers/{layer_id}", status_code=204)
def delete_shp_preview_layer(layer_id: str):
    if not shp_preview_manager.delete(layer_id): raise HTTPException(404, "图层不存在")


@app.get("/api/tile-preview/layers")
def list_tile_preview_layers():
    return tile_preview_manager.list()


@app.post("/api/tile-preview/layers", status_code=201)
async def create_tile_preview_layer(files: list[UploadFile] = File(...), name: str = Form("")):
    upload_root = tile_preview_manager.data_dir / f"upload-{__import__('uuid').uuid4().hex}"
    upload_root.mkdir(parents=True)
    relative_files, total = [], 0
    try:
        for upload in files:
            relative = PurePosixPath((upload.filename or "").replace("\\", "/"))
            parts = [part for part in relative.parts if part not in ("", ".", "..")]
            if not parts or Path(parts[-1]).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            target = upload_root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > 2 * 1024 * 1024 * 1024:
                        raise HTTPException(413, "瓦片目录不能超过 2GB")
                    output.write(chunk)
            relative_files.append("/".join(parts))
        try:
            return tile_preview_manager.create(upload_root, relative_files, name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    except Exception:
        shutil.rmtree(upload_root, ignore_errors=True)
        raise


@app.get("/api/tile-preview/layers/{layer_id}/{z}/{x}/{y}")
def get_tile_preview_tile(layer_id: str, z: int, x: int, y: int):
    path = tile_preview_manager.tile(layer_id, z, x, y)
    if not path:
        raise HTTPException(404, "瓦片不存在")
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
    return FileResponse(path, media_type=media)


@app.delete("/api/tile-preview/layers/{layer_id}", status_code=204)
def delete_tile_preview_layer(layer_id: str):
    if not tile_preview_manager.delete(layer_id):
        raise HTTPException(404, "瓦片图层不存在")


@app.get("/api/regions/search")
def search_regions(q: str):
    if len(q.strip()) < 2:
        raise HTTPException(400, "请输入至少两个字符")
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q.strip(), "format": "jsonv2", "polygon_geojson": 1,
                    "addressdetails": 1, "limit": 8},
            headers={"User-Agent": "SatelliteDownloadWeb/1.0 (local application)"}, timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(502, f"行政区划服务请求失败：{exc}") from exc
    results = []
    for item in response.json():
        bbox = item.get("boundingbox")
        geometry = item.get("geojson")
        if not bbox or not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        results.append({"id": f"{item.get('osm_type')}-{item.get('osm_id')}",
                        "name": item.get("display_name"), "type": item.get("type"),
                        "bounds": {"south": float(bbox[0]), "north": float(bbox[1]),
                                   "west": float(bbox[2]), "east": float(bbox[3])},
                        "geometry": geometry})
    return results


@app.get("/api/propzone/search")
def search_propzone_places(q: str = "", level: str = "all"):
    level = level if level in ("all", "state", "county", "city") else "all"
    query = q.strip()
    if len(query) < 2:
        # 只有州级支持空关键词加载内置全量目录；County/城市必须给出关键词，
        # 避免 Nominatim 对 “counties in United States” 返回无关候选。
        if level == "state":
            # 空关键词直接走内置州目录，不要把泛化英文句子提交给地理搜索服务。
            query = ""
        else:
            query = ""
    if not query and level != "state":
        raise HTTPException(400, "请输入区域名称或选择行政层级")
    try:
        return propzone_manager.cached_search(query, level)
    except requests.RequestException as exc:
        raise HTTPException(502, f"全美行政区搜索失败：{exc}") from exc


@app.get("/api/propzone/reverse")
def reverse_propzone_place(lat: float, lon: float):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "坐标范围无效")
    try:
        area = propzone_manager.client.reverse_place(lat, lon)
        if not area:
            raise HTTPException(404, "该位置附近没有可识别的美国行政区域")
        return area
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(502, f"位置识别失败：{exc}") from exc


@app.get("/api/propzone/children")
def propzone_children(area_id: str = "", name: str = "", state: str = ""):
    try:
        return propzone_manager.cached_children({"id": area_id, "short_name": name, "state": state})
    except requests.RequestException as exc:
        raise HTTPException(502, f"下级行政区获取失败：{exc}") from exc
    except Exception as exc:
        raise HTTPException(502, f"下级行政区获取失败：{exc}") from exc


@app.post("/api/propzone/resolve")
def resolve_propzone_area(payload: dict):
    """按 PropZone 官方 URL 解析地点并返回官方边界/zoning 图层。"""
    url = str(payload.get("url") or payload.get("propzone_url") or "").strip()
    state = str(payload.get("state") or "").strip().lower()
    if not url:
        raise HTTPException(400, "请输入 PropZone 区域路径或 URL")
    if url.startswith("http"):
        from urllib.parse import urlparse
        url = urlparse(url).path
    if not state:
        parts = [part for part in url.strip("/").split("/") if part]
        state = parts[2].lower() if len(parts) >= 3 and parts[0] in ("city", "unincorporated", "county", "state") else ""
    try:
        return propzone_manager.client.resolve_area({**payload, "propzone_url": url, "state": state})
    except requests.RequestException as exc:
        raise HTTPException(502, f"PropZone 官方区域解析失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"PropZone 官方区域解析失败：{exc}") from exc


@app.get("/api/propzone/tasks")
def list_propzone_tasks():
    return propzone_manager.list()


@app.post("/api/propzone/tasks", status_code=202)
def create_propzone_task(payload: dict):
    try:
        return propzone_manager.create(payload.get("areas") or [], payload.get("level", "all"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/propzone/tasks/{task_id}/{action}")
def control_propzone_task(task_id: str, action: str):
    if action not in ("pause", "resume", "retry"):
        raise HTTPException(404, "未知操作")
    try:
        task = propzone_manager.retry(task_id) if action == "retry" else propzone_manager.control(task_id, action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/propzone/tasks/{task_id}")
def get_propzone_task(task_id: str):
    task = propzone_manager.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.delete("/api/propzone/tasks/{task_id}", status_code=204)
def delete_propzone_task(task_id: str):
    if not propzone_manager.delete(task_id):
        raise HTTPException(404, "任务不存在")


@app.get("/api/propzone/tasks/{task_id}/preview")
def preview_propzone_task(task_id: str):
    task = propzone_manager.get(task_id)
    if not task or task.get("status") != "completed":
        raise HTTPException(404, "任务未完成或不存在")
    path = propzone_manager.data_dir / task_id / "preview.geojson"
    if not path.is_file():
        raise HTTPException(404, "预览文件不存在")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/propzone/tasks/{task_id}/export")
def export_propzone_task(task_id: str):
    task = propzone_manager.get(task_id)
    if not task or task.get("status") != "completed" or not task.get("result", {}).get("archive"):
        raise HTTPException(404, "任务未完成或不存在")
    path = propzone_manager.data_dir / task_id / task["result"]["archive"]
    if not path.is_file():
        path = propzone_manager.data_dir / task["result"]["archive"]
    if not path.is_file():
        raise HTTPException(404, "SHP 文件不存在")
    return FileResponse(path, media_type="application/zip", filename=f"{task['name']}_{task_id[:8]}.zip")


@app.get("/api/tasks")
def list_tasks():
    return manager.list()


@app.post("/api/tasks", status_code=202)
def create_task(request: CreateTask):
    return manager.create(request)


@app.post("/api/tasks/batch", status_code=202)
def create_batch(request: BatchCreate):
    return [manager.create(item) for item in request.tasks]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = manager.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/tasks/{task_id}/pause")
def pause_task(task_id: str):
    try:
        task = manager.pause(task_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str):
    try:
        task = manager.resume(task_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    try:
        task = manager.retry(task_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str):
    if not manager.delete(task_id):
        raise HTTPException(404, "任务不存在")


@app.get("/api/tasks/{task_id}/export")
def export_task(task_id: str):
    task = manager.get(task_id)
    if not task or task["status"] != "completed":
        raise HTTPException(404, "任务未完成或不存在")
    path = manager.data_dir / f"{task_id}.zip"
    return FileResponse(path, filename=f"{task['request']['options']['output_name']}_{task_id[:8]}.zip")


@app.get("/api/docker/versions")
def docker_versions(arch: str = "x86_64"):
    arch_path = {"x86_64": "x86_64", "aarch64": "aarch64", "armv7l": "armhf"}.get(arch)
    if not arch_path:
        raise HTTPException(400, "不支持的架构")
    try:
        docker_response = requests.get(
            f"https://download.docker.com/linux/static/stable/{arch_path}/", timeout=20,
            headers={"User-Agent": "OfflinePackageWeb/1.0"})
        docker_response.raise_for_status()
        docker_items = sorted(set(re.findall(r'docker-([0-9]+(?:\.[0-9]+)+)\.tgz', docker_response.text)),
                              key=lambda value: tuple(map(int, value.split('.'))), reverse=True)[:50]
        compose_response = requests.get("https://api.github.com/repos/docker/compose/releases",
                                        params={"per_page": 50}, timeout=20,
                                        headers={"User-Agent": "OfflinePackageWeb/1.0"})
        compose_response.raise_for_status()
        compose_items = [item["tag_name"].removeprefix("v") for item in compose_response.json()
                         if not item.get("prerelease")]
        return {"docker": docker_items, "compose": compose_items}
    except requests.RequestException as exc:
        raise HTTPException(502, f"版本信息获取失败：{exc}") from exc


@app.get("/api/docker/tasks")
def list_docker_tasks(): return docker_manager.list()


@app.post("/api/docker/tasks", status_code=202)
def create_docker_task(request: DockerCreateTask): return docker_manager.create(request)


@app.post("/api/docker/tasks/{task_id}/{action}")
def control_docker_task(task_id: str, action: str):
    if action not in ("pause", "resume", "retry"):
        raise HTTPException(404, "未知操作")
    try:
        task = docker_manager.retry(task_id) if action == "retry" else docker_manager.control(task_id, action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task: raise HTTPException(404, "任务不存在")
    return crawler_manager.public_task(task)


@app.delete("/api/docker/tasks/{task_id}", status_code=204)
def delete_docker_task(task_id: str):
    if not docker_manager.delete(task_id): raise HTTPException(404, "任务不存在")


@app.get("/api/docker/tasks/{task_id}/export")
def export_docker_task(task_id: str):
    task = docker_manager.get(task_id)
    if not task or task["status"] != "completed": raise HTTPException(404, "任务未完成或不存在")
    return FileResponse(docker_manager.data_dir / f"{task_id}.zip",
                        filename=f"{task['name']}_{task_id[:8]}.zip")


@app.get("/api/gjb/tasks")
def list_gjb_tasks(): return gjb_manager.list()


@app.post("/api/gjb/tasks", status_code=202)
async def create_gjb_task(files: list[UploadFile] = File(...), crs: str = Form("auto")):
    if crs != "auto": raise HTTPException(400, "GJB 坐标系必须根据 SMS 自动识别，暂不支持仅修改坐标系声明")
    upload_dir = gjb_manager.data_dir / f"upload-{__import__('uuid').uuid4().hex}"
    upload_dir.mkdir(parents=True)
    total_size, stems, group_files = 0, set(), {}
    try:
        for upload in files:
            name = Path(upload.filename or "").name
            suffix = Path(name).suffix.upper()
            if not name or not re.fullmatch(r"\.(SMS|[ABCDEFIJKLR](MS|SX|TP|ZB))", suffix):
                continue
            target = upload_dir / name
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total_size += len(chunk)
                    if total_size > 200 * 1024 * 1024: raise HTTPException(413, "上传目录不能超过 200MB")
                    output.write(chunk)
            stems.add(target.stem)
            if suffix != ".SMS":
                group_files.setdefault(suffix[1], set()).add(suffix[2:])
        if len(stems) != 1 or not any("MS" in kinds for kinds in group_files.values()):
            raise HTTPException(400, "目录必须包含同一图幅前缀的 .XMS/.XSX/.XTP/.XZB 文件组")
        incomplete = [letter for letter, kinds in group_files.items()
                      if "MS" in kinds and not {"SX", "ZB"}.issubset(kinds)]
        if incomplete:
            raise HTTPException(400, "以下图层组缺少 SX 或 ZB 文件：" + "、".join(sorted(incomplete)))
        if not any({"MS", "SX", "ZB"}.issubset(kinds) for kinds in group_files.values()):
            raise HTTPException(400, "至少需要一组完整的 XMS、XSX、XZB 文件；XTP 可选")
        stem = next(iter(stems))
        return gjb_manager.create(upload_dir, stem, "")
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise


@app.post("/api/gjb/tasks/{task_id}/retry")
def retry_gjb_task(task_id: str):
    try: task = gjb_manager.retry(task_id)
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc
    if not task: raise HTTPException(404, "任务不存在")
    return task


@app.delete("/api/gjb/tasks/{task_id}", status_code=204)
def delete_gjb_task(task_id: str):
    if not gjb_manager.delete(task_id): raise HTTPException(404, "任务不存在")


@app.get("/api/gjb/tasks/{task_id}/preview")
def preview_gjb_task(task_id: str):
    task = gjb_manager.get(task_id)
    if not task or task["status"] != "completed": raise HTTPException(404, "任务未完成或不存在")
    return FileResponse(gjb_manager.data_dir/task_id/"preview.geojson", media_type="application/geo+json")


@app.get("/api/gjb/tasks/{task_id}/export")
def export_gjb_task(task_id: str):
    task = gjb_manager.get(task_id)
    if not task or task["status"] != "completed": raise HTTPException(404, "任务未完成或不存在")
    return FileResponse(gjb_manager.data_dir/f"{task_id}.zip", filename=f"{task['name']}_shapefile.zip")


@app.post("/api/es/connect")
def connect_es(connection: ESConnection):
    try:
        client = ESClient(connection); info = client.info(); indices = client.indices()
        return {"cluster_name": info.get("cluster_name"), "version": info.get("version", {}).get("number"),
                "indices": sorted(indices, key=lambda item: item.get("index", ""))}
    except Exception as exc: raise HTTPException(502, f"Elasticsearch 连接失败：{exc}") from exc


@app.get("/api/es/tasks")
def list_es_tasks(): return es_manager.list()


@app.post("/api/es/tasks/export", status_code=202)
def create_es_export(request: ESExportTask): return es_manager.create_export(request)


@app.post("/api/es/tasks/import", status_code=202)
async def create_es_import(files: list[UploadFile] = File(...), host: str = Form(...), username: str = Form(""),
                           password: str = Form(""), verify_certs: bool = Form(True), overwrite: bool = Form(False),
                           ):
    try: connection = ESConnection(host=host, username=username, password=password, verify_certs=verify_certs)
    except Exception as exc: raise HTTPException(400, str(exc)) from exc
    upload_dir = es_manager.data_dir / f"upload-{__import__('uuid').uuid4().hex}"; upload_dir.mkdir(parents=True); total = 0
    try:
        for upload in files:
            relative = Path(upload.filename or ""); parts = [part for part in relative.parts if part not in ("", ".", "..")]
            if not parts: continue
            target = upload_dir.joinpath(*parts); target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > 2 * 1024 * 1024 * 1024: raise HTTPException(413, "导入文件不能超过 2GB")
                    output.write(chunk)
        if not any(upload_dir.rglob("*.zip")) and not any(upload_dir.rglob("*.json")):
            raise HTTPException(400, "请上传 ES 导出 ZIP 或包含 mapping/data 的目录")
        return es_manager.create_import(connection, upload_dir, overwrite)
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True); raise


@app.post("/api/es/tasks/{task_id}/{action}")
def control_es_task(task_id: str, action: str):
    if action not in ("pause", "resume"): raise HTTPException(404, "未知操作")
    try: task = es_manager.control(task_id, action)
    except ValueError as exc: raise HTTPException(409, str(exc)) from exc
    if not task: raise HTTPException(404, "任务不存在")
    return task


@app.delete("/api/es/tasks/{task_id}", status_code=204)
def delete_es_task(task_id: str):
    if not es_manager.delete(task_id): raise HTTPException(404, "任务不存在")


@app.get("/api/es/tasks/{task_id}/export")
def export_es_task(task_id: str):
    task=es_manager.get(task_id)
    if not task or task["mode"]!="export" or task["status"]!="completed":raise HTTPException(404,"导出任务未完成或不存在")
    return FileResponse(es_manager.data_dir/f"{task_id}.zip",filename=f"{task['name']}.zip")


@app.post("/api/nebula/connect")
def connect_nebula(connection: NebulaConnection):
    try:return {"spaces":nebula_manager.connect(connection)}
    except Exception as exc:raise HTTPException(502,f"NebulaGraph 连接失败：{exc}") from exc


@app.get("/api/nebula/tasks")
def list_nebula_tasks():return nebula_manager.list()


@app.post("/api/nebula/tasks/export",status_code=202)
def create_nebula_export(request:NebulaExportTask):return nebula_manager.create_export(request)


@app.post("/api/nebula/tasks/import",status_code=202)
async def create_nebula_import(files:list[UploadFile]=File(...),host:str=Form(...),port:int=Form(9669),
                               username:str=Form("root"),password:str=Form("nebula")):
    try:connection=NebulaConnection(host=host,port=port,username=username,password=password)
    except Exception as exc:raise HTTPException(400,str(exc)) from exc
    upload_dir=nebula_manager.data_dir/f"upload-{__import__('uuid').uuid4().hex}";upload_dir.mkdir(parents=True);total=0
    try:
        for upload in files:
            relative=Path(upload.filename or "");parts=[part for part in relative.parts if part not in ("",".","..")]
            if not parts:continue
            target=upload_dir.joinpath(*parts);target.parent.mkdir(parents=True,exist_ok=True)
            with target.open("wb") as output:
                while chunk:=await upload.read(1024*1024):
                    total+=len(chunk)
                    if total>2*1024*1024*1024:raise HTTPException(413,"导入文件不能超过 2GB")
                    output.write(chunk)
        if not any(upload_dir.rglob("*.zip")) and not any(upload_dir.rglob("*_base.txt")):raise HTTPException(400,"请选择 Nebula 导出 ZIP 或备份目录")
        return nebula_manager.create_import(connection,upload_dir)
    except Exception:shutil.rmtree(upload_dir,ignore_errors=True);raise


@app.delete("/api/nebula/tasks/{task_id}",status_code=204)
def delete_nebula_task(task_id:str):
    if not nebula_manager.delete(task_id):raise HTTPException(404,"任务不存在")


@app.get("/api/nebula/tasks/{task_id}/export")
def export_nebula_task(task_id:str):
    task=nebula_manager.get(task_id)
    if not task or task["mode"]!="export" or task["status"]!="completed":raise HTTPException(404,"导出任务未完成或不存在")
    return FileResponse(nebula_manager.data_dir/f"{task_id}.zip",filename=f"{task['name']}.zip")


# 智能网页采集
@app.post("/api/crawler/schedule-preview")
def crawler_schedule_preview(payload: dict):
    from .crawler_schedule import describe
    try:
        return describe(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/crawler/sql/test")
def crawler_sql_test(payload: dict):
    from .crawler_sql_sink import test_connection
    try:
        return test_connection(payload)
    except Exception as exc:
        raise HTTPException(400, f"PostgreSQL 连接失败：{exc}") from exc


@app.get("/api/crawler/fields")
def crawler_builtin_fields(source: str = "generic", twitter_mode: str = "keyword",
                           tiktok_mode: str = "keyword", telegram_mode: str = "channel"):
    return crawler_manager.builtin_fields(source, twitter_mode, tiktok_mode, telegram_mode)


@app.get("/api/crawler/capabilities")
def crawler_capabilities():
    return crawler_manager.capabilities()


@app.post("/api/crawler/preview")
def crawler_preview(payload: dict):
    try:
        return crawler_manager.preview(payload)
    except Exception as exc:
        raise HTTPException(400, f"页面预览失败：{exc}") from exc


@app.post("/api/crawler/data-preview")
def crawler_data_preview(payload: dict):
    """创建任务前预览少量真实采集样本。"""
    try:
        return crawler_manager.data_preview(payload)
    except Exception as exc:
        raise HTTPException(400, f"采集数据预览失败：{exc}") from exc


@app.post("/api/crawler/analyze")
def crawler_analyze(payload: dict):
    try:
        return crawler_manager.suggest(payload.get("url", ""), payload.get("source", "generic"))
    except Exception as exc:
        raise HTTPException(400, f"页面解析失败：{exc}") from exc


@app.get("/api/crawler/tasks")
def list_crawler_tasks():
    return crawler_manager.list()


@app.post("/api/crawler/tasks", status_code=202)
def create_crawler_task(payload: dict):
    try:
        return crawler_manager.create(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/crawler/tasks/{task_id}/{action}")
def control_crawler_task(task_id: str, action: str):
    try:
        if action == "retry": task = crawler_manager.retry(task_id)
        elif action in ("pause", "resume", "pause_schedule", "resume_schedule"): task = crawler_manager.control(task_id, action)
        else: raise HTTPException(404, "未知操作")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task: raise HTTPException(404, "任务不存在")
    return task


@app.delete("/api/crawler/tasks/{task_id}", status_code=204)
def delete_crawler_task(task_id: str):
    if not crawler_manager.delete(task_id): raise HTTPException(404, "任务不存在")


@app.get("/api/crawler/tasks/{task_id}/export")
def export_crawler_task(task_id: str):
    task = crawler_manager.get(task_id)
    if not task:
        raise HTTPException(404, "采集任务不存在")
    try:
        request = task.get("request", {})
        result = task.get("result") or {}
        # 单次视频任务直接交付本次生成的 ZIP；累计导出逻辑只用于定时任务。
        # 否则即使勾选了下载视频，点击任务下载仍会被转换成纯数据表。
        scheduled = bool(request.get("cron") or int(request.get("interval_minutes") or 0) > 0)
        # 视频任务的结果文件本身就是 ZIP（兼容旧任务中未持久化 download_videos 标记的情况）。
        # 只要任务结果指向现存 ZIP，就直接交付该压缩包，避免回退到累计 Excel 导出。
        result_file = str(result.get("file") or "")
        video_task = request.get("source") in ("youtube", "tiktok")
        if not scheduled and (request.get("download_videos") or (video_task and result_file.lower().endswith(".zip"))) and result_file:
            candidate = crawler_manager.data_dir / task_id / result_file
            if candidate.is_file() and candidate.suffix.lower() == ".zip":
                return FileResponse(candidate, filename=f"{task['name']}.zip")
        # 兼容早期任务：前端已勾选但任务记录只保存了 Excel 文件名。
        # 若任务目录中存在已下载视频，导出时即时重新打包为 ZIP。
        if not scheduled and video_task:
            videos_dir = crawler_manager.data_dir / task_id / "videos"
            if videos_dir.is_dir() and any(p.is_file() and p.stat().st_size > 0 for p in videos_dir.iterdir()):
                data_name = str(result.get("data_file") or result_file)
                data_path = crawler_manager.data_dir / task_id / data_name
                if data_path.is_file():
                    archive = crawler_manager.data_dir / task_id / "videos_export.zip"
                    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as package:
                        package.write(data_path, data_path.name)
                        for video in videos_dir.iterdir():
                            if video.is_file() and video.stat().st_size > 0 and not video.name.endswith(".part"):
                                package.write(video, f"videos/{video.name}")
                    return FileResponse(archive, filename=f"{task['name']}.zip")
        path, count = crawler_manager.export_cumulative(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=f"{task['name']} · 全部{count}条{path.suffix}")


@app.get("/api/crawler/tasks/{task_id}/runs/{run_id}/export")
def export_crawler_run(task_id: str, run_id: str):
    task = crawler_manager.get(task_id)
    if not task:
        raise HTTPException(404, "采集任务不存在")
    run = next((item for item in task.get("runs", []) if item.get("id") == run_id), None)
    filename = str((run or {}).get("result", {}).get("file") or "")
    if not run or run.get("status") != "completed" or not filename:
        raise HTTPException(404, "本次执行没有可下载的结果")
    task_dir = (crawler_manager.data_dir / task_id).resolve()
    path = (task_dir / filename).resolve()
    if task_dir not in path.parents or not path.is_file():
        raise HTTPException(404, "本次执行的导出文件不存在")
    return FileResponse(path, filename=f"{task['name']} · 第{run.get('sequence')}次{path.suffix}")
