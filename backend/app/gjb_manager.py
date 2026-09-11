import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .gjb_converter import convert


class GJBTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir; data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"; self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gjb-task")
        self.cancel_events = {}
        try: self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError): self.tasks = {}
        for task in self.tasks.values():
            if task["status"] in ("queued", "running"): task.update(status="failed", message="服务重启，任务已中断")
        self._save()

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

    def _run(self, task_id):
        task_dir = self.data_dir / task_id; output_dir = task_dir / "output"
        cancel_event = None
        try:
            with self.lock:
                task = self.tasks.get(task_id)
                cancel_event = self.cancel_events.get(task_id)
                if not task or not cancel_event: return
                crs = task["request"]["crs"]
            def report(a, b, message):
                if cancel_event.is_set(): raise RuntimeError("任务已取消")
                self._update(task_id, progress=round(a/b*95,1) if b else 0, message=message)
            self._update(task_id, status="running", message="正在解析 GJB5068 数据")
            result = convert(task_dir / "input", output_dir, crs, report)
            with self.lock:
                if cancel_event.is_set() or task_id not in self.tasks: return
                archive = shutil.make_archive(str(task_dir), "zip", output_dir)
                result["archive"] = Path(archive).name
            self._update(task_id, status="completed", progress=100, message="转换完成", result=result)
        except Exception as exc: self._update(task_id, status="failed", message="转换失败", error=str(exc))
        finally:
            if cancel_event and cancel_event.is_set():
                shutil.rmtree(task_dir, ignore_errors=True)
                (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
            with self.lock: self.cancel_events.pop(task_id, None)

    def list(self):
        with self.lock: return sorted(self.tasks.values(), key=lambda x:x["created_at"], reverse=True)
    def get(self, task_id):
        with self.lock: return self.tasks.get(task_id)
    def delete(self, task_id):
        with self.lock:
            event = self.cancel_events.get(task_id)
            if event: event.set()
            if not self.tasks.pop(task_id, None): return False
            self._save()
        shutil.rmtree(self.data_dir/task_id, ignore_errors=True); (self.data_dir/f"{task_id}.zip").unlink(missing_ok=True); return True
    def retry(self, task_id):
        with self.lock:
            task=self.tasks.get(task_id)
            if not task: return None
            if task["status"]!="failed": raise ValueError("仅失败任务可以重试")
            task["request"]["crs"] = ""
            task.update(status="queued",progress=0,message="等待重新转换",error=None,result=None); self._save()
            self.cancel_events[task_id] = threading.Event()
        shutil.rmtree(self.data_dir/task_id/"output",ignore_errors=True)
        (self.data_dir/task_id/"preview.geojson").unlink(missing_ok=True)
        (self.data_dir/f"{task_id}.zip").unlink(missing_ok=True)
        self.pool.submit(self._run,task_id); return task
