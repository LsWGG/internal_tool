import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .downloader import download_bounds, validate_result_files
from .models import CreateTask


class TaskCancelled(Exception):
    pass


class TaskControl:
    def __init__(self):
        self.condition = threading.Condition()
        self.paused = False
        self.cancelled = False

    def checkpoint(self):
        with self.condition:
            while self.paused and not self.cancelled:
                self.condition.wait()
            if self.cancelled:
                raise TaskCancelled()

    def pause(self):
        with self.condition:
            self.paused = True

    def resume(self):
        with self.condition:
            self.paused = False
            self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.paused = False
            self.condition.notify_all()


class TaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.tasks = self._load()
        self.controls = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="map-task")
        for item in self.tasks.values():
            if item["status"] in ("queued", "running", "paused"):
                item.update(status="failed", message="服务重启，任务已中断")
        self._save()

    def _load(self):
        try:
            return json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.state_file)

    def create(self, request: CreateTask):
        task_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        if not request.options.output_name.strip():
            bounds = request.bounds
            source_names={"google_satellite":"Google卫星","google_hybrid":"Google混合影像","bing_aerial":"Bing航拍","amap_satellite":"高德卫星","amap_hybrid":"高德卫星混合","amap_standard":"高德标准地图","osm_standard":"OpenStreetMap","opentopomap":"OpenTopoMap","aws_terrarium":"AWS高程"}
            content_name = source_names.get(request.options.tile_source, "DEM高程" if request.options.data_type == "dem" else "卫星影像")
            request.options.output_name = (
                f"{content_name}_E{bounds.west:.4f}-{bounds.east:.4f}_"
                f"N{bounds.south:.4f}-{bounds.north:.4f}_"
                f"Z{request.options.zoom_min}-{request.options.zoom_max}"
            )
        record = {"id": task_id, "status": "queued", "progress": 0, "current": 0, "total": 0,
                  "message": "等待执行", "created_at": now, "updated_at": now,
                  "request": request.model_dump(), "result": None, "error": None}
        with self.lock:
            self.tasks[task_id] = record
            self.controls[task_id] = TaskControl()
            self._save()
        self.pool.submit(self._run, task_id, request)
        return record

    def list(self):
        with self.lock:
            return sorted(self.tasks.values(), key=lambda x: x["created_at"], reverse=True)

    def get(self, task_id: str):
        with self.lock:
            return self.tasks.get(task_id)

    def _update(self, task_id: str, **values):
        with self.lock:
            if task_id not in self.tasks:
                return
            values["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.tasks[task_id].update(values)
            self._save()

    def _progress(self, task_id, current, total, message):
        pct = round(current / total * 100, 1) if total else 0
        self._update(task_id, current=current, total=total, progress=pct, message=message)

    def _run(self, task_id: str, request: CreateTask):
        control = self.controls[task_id]
        output_dir = self.data_dir / task_id
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            control.checkpoint()
            self._update(task_id, status="running", message="任务已开始")
            result = download_bounds(request.bounds, request.geometry, request.options, output_dir,
                                     lambda a, b, c: self._progress(task_id, a, b, c), control.checkpoint)
            control.checkpoint()
            self._update(task_id,message="正在校验下载结果完整性")
            validate_result_files(output_dir,result)
            shutil.rmtree(output_dir/".tiles",ignore_errors=True)
            (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
            archive = shutil.make_archive(str(output_dir), "zip", output_dir)
            control.checkpoint()
            result["archive"] = Path(archive).name
            self._update(task_id, status="completed", progress=100, message="下载完成", result=result)
        except TaskCancelled:
            shutil.rmtree(output_dir, ignore_errors=True)
            (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
            self._update(task_id, status="cancelled", message="任务已取消")
        except Exception as exc:
            self._update(task_id, status="failed", message="任务失败", error=str(exc))
        finally:
            if control.cancelled:
                shutil.rmtree(output_dir, ignore_errors=True)
                (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
            with self.lock:
                if self.controls.get(task_id) is control:
                    self.controls.pop(task_id, None)

    def pause(self, task_id: str):
        with self.lock:
            task, control = self.tasks.get(task_id), self.controls.get(task_id)
            if not task:
                return None
            if task["status"] not in ("queued", "running") or not control:
                raise ValueError("当前状态无法暂停")
            control.pause()
            task.update(status="paused", message="任务已暂停", updated_at=datetime.now(timezone.utc).isoformat())
            self._save()
            return task

    def resume(self, task_id: str):
        with self.lock:
            task, control = self.tasks.get(task_id), self.controls.get(task_id)
            if not task:
                return None
            if task["status"] != "paused" or not control:
                raise ValueError("当前状态无法继续")
            task.update(status="running", message="任务继续执行", updated_at=datetime.now(timezone.utc).isoformat())
            control.resume()
            self._save()
            return task

    def delete(self, task_id: str):
        with self.lock:
            task = self.tasks.pop(task_id, None)
            if not task:
                return False
            control = self.controls.get(task_id)
            if control:
                control.cancel()
            self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True)
        archive = self.data_dir / f"{task_id}.zip"
        archive.unlink(missing_ok=True)
        return True

    def retry(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if task["status"] not in ("failed", "cancelled"):
                raise ValueError("仅失败或已取消的任务可以重试")
            request = CreateTask.model_validate(task["request"])
            control = TaskControl()
            self.controls[task_id] = control
            now = datetime.now(timezone.utc).isoformat()
            task.update(status="queued", progress=0, current=0, total=0,
                        message="等待重新执行", updated_at=now, result=None, error=None)
            self._save()
        # Preserve verified tile cache and partial output so retry can resume
        # from the missing tiles instead of downloading the whole task again.
        (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
        self.pool.submit(self._run, task_id, request)
        return task
