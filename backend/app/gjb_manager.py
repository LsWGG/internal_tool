import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .gjb_converter import convert, discover
from .local_path import resolve_directory


class GJBTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir; data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"; self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gjb-task")
        self.cancel_events = {}
        try: self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError): self.tasks = {}
        resume_ids = []
        for task in self.tasks.values():
            if task["status"] in ("queued", "running"):
                task.update(status="queued", message="服务重启，从已转换内容继续", error=None)
                resume_ids.append(task["id"])
        self._save()
        for task_id in resume_ids:
            self.cancel_events[task_id] = threading.Event()
            self.pool.submit(self._run, task_id)

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp"); temp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8"); temp.replace(self.state_file)

    def _update(self, task_id, **values):
        with self.lock:
            if task_id not in self.tasks: return
            values["updated_at"] = datetime.now(timezone.utc).isoformat(); self.tasks[task_id].update(values); self._save()

    def create(self, upload_dir: Path, name: str, crs: str):
        task_id, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        task_dir = self.data_dir / task_id; task_dir.mkdir(parents=True); shutil.move(str(upload_dir), task_dir / "input")
        record = {"id": task_id, "name": name, "status": "queued", "progress": 0, "message": "等待转换",
                  "created_at": now, "updated_at": now, "request": {"crs": crs}, "result": None, "error": None}
        with self.lock: self.tasks[task_id] = record; self._save()
        with self.lock: self.cancel_events[task_id] = threading.Event()
        self.pool.submit(self._run, task_id); return record

    def open_directory(self, source_raw: str, output_raw: str = "", in_place: bool = False):
        source = resolve_directory(source_raw, writable=bool(in_place))
        discover(source)
        if in_place and str(output_raw or "").strip():
            raise ValueError("原目录转换时不需要另填输出路径")
        task_id, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        if in_place:
            output = source
        elif str(output_raw or "").strip():
            output = resolve_directory(output_raw, writable=True) / task_id
        else:
            output = self.data_dir / task_id / "output"
        (self.data_dir / task_id).mkdir(parents=True, exist_ok=True)
        record = {"id": task_id, "name": source.name, "status": "queued", "progress": 0, "message": "等待转换",
                  "created_at": now, "updated_at": now, "output_dir": str(output),
                  "request": {"crs": "", "source_path": str(source), "in_place": bool(in_place)},
                  "result": None, "error": None}
        with self.lock: self.tasks[task_id] = record; self._save()
        with self.lock: self.cancel_events[task_id] = threading.Event()
        self.pool.submit(self._run, task_id); return record

    def _locations(self, task):
        task_id = task["id"]
        request = task.get("request") or {}
        source = Path(request["source_path"]) if request.get("source_path") else self.data_dir / task_id / "input"
        output = Path(task["output_dir"]) if task.get("output_dir") else self.data_dir / task_id / "output"
        return source, output, bool(request.get("in_place"))

    def _discard(self, task_id, task):
        request = (task or {}).get("request") or {}
        source = Path(request["source_path"]) if request.get("source_path") else None
        output = Path(task["output_dir"]) if task and task.get("output_dir") else None
        in_place = bool(request.get("in_place"))
        if output and output.name == task_id and output != source and not in_place:
            shutil.rmtree(output, ignore_errors=True)
        sidecar = self.data_dir / task_id
        if sidecar.name == task_id and sidecar != source:
            shutil.rmtree(sidecar, ignore_errors=True)
        for archive in ((output.parent / f"{task_id}.zip") if output else None, self.data_dir / f"{task_id}.zip"):
            if archive and archive.name == f"{task_id}.zip":
                archive.unlink(missing_ok=True)

    def _run(self, task_id):
        cancel_event = None
        task_snapshot = None
        try:
            with self.lock:
                task = self.tasks.get(task_id)
                cancel_event = self.cancel_events.get(task_id)
                if not task or not cancel_event: return
                task_snapshot = json.loads(json.dumps(task))
                crs = task["request"]["crs"]
                source, output, in_place = self._locations(task)
            def report(a, b, message):
                if cancel_event.is_set(): raise RuntimeError("任务已取消")
                self._update(task_id, progress=round(a/b*95,1) if b else 0, message=message)
            self._update(task_id, status="running", message="正在解析 GJB5068 数据")
            preview = self.data_dir / task_id / "preview.geojson"
            result = convert(source, output, crs, report, preview_path=preview)
            with self.lock:
                if cancel_event.is_set() or task_id not in self.tasks: return
                if not in_place:
                    if output.name == task_id:
                        archive = shutil.make_archive(str(output.parent / task_id), "zip", output)
                    else:
                        archive = shutil.make_archive(str(self.data_dir / task_id), "zip", output)
                    result["archive"] = Path(archive).name
                    result["archive_path"] = str(Path(archive).resolve())
                else:
                    result["in_place"] = True
                    result["output_dir"] = str(output)
            self._update(task_id, status="completed", progress=100, message="转换完成" if not in_place else "已在原目录完成转换", result=result)
        except Exception as exc: self._update(task_id, status="failed", message="转换失败", error=str(exc))
        finally:
            if cancel_event and cancel_event.is_set() and task_snapshot:
                self._discard(task_id, task_snapshot)
            with self.lock: self.cancel_events.pop(task_id, None)

    def list(self):
        with self.lock: return sorted(self.tasks.values(), key=lambda x:x["created_at"], reverse=True)
    def get(self, task_id):
        with self.lock: return self.tasks.get(task_id)
    def delete(self, task_id):
        with self.lock:
            event = self.cancel_events.get(task_id)
            if event: event.set()
            task = self.tasks.pop(task_id, None)
            if not task: return False
            self._save()
        self._discard(task_id, task); return True
    def retry(self, task_id):
        with self.lock:
            task=self.tasks.get(task_id)
            if not task: return None
            if task["status"]!="failed": raise ValueError("仅失败任务可以重试")
            task["request"]["crs"] = ""
            task.update(status="queued",progress=0,message="等待重新转换",error=None,result=None); self._save()
            self.cancel_events[task_id] = threading.Event()
            _, output, in_place = self._locations(task)
        if not in_place:
            for archive in (output.parent / f"{task_id}.zip" if output.name == task_id else None, self.data_dir / f"{task_id}.zip"):
                if archive and archive.name == f"{task_id}.zip":
                    archive.unlink(missing_ok=True)
        self.pool.submit(self._run,task_id); return task
