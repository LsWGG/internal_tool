"""Pause, resume and retry for path-based jobs.

Subclasses decide what a job writes. This base class never deletes a directory
unless the subclass asks, and a subclass may only remove a directory whose
final component is the task id it created.
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


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


def remove_owned_directory(path: Path, task_id: str) -> None:
    """Delete a directory only when its final component is the task id."""
    if not task_id or not path or path.name != task_id:
        return
    resolved = path.resolve()
    if resolved.name != task_id or resolved.parent == resolved:
        return
    if resolved.is_dir():
        shutil.rmtree(resolved)
    zip_path = resolved.parent / f"{task_id}.zip"
    if zip_path.is_file() and zip_path.name == f"{task_id}.zip":
        zip_path.unlink()


def remove_owned_file(path: Path, task_id: str) -> None:
    if not task_id or path.name != f"{task_id}.mbtiles":
        return
    resolved = path.resolve()
    if resolved.name != f"{task_id}.mbtiles" or resolved.parent == resolved:
        return
    if resolved.is_file():
        resolved.unlink()


class PathJobManager:
    def __init__(self, data_dir: Path, state_name: str = "tasks.json"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / state_name
        self.lock = threading.RLock()
        self.tasks = self._load()
        self.controls = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="path-job")
        resume_ids = []
        for item in self.tasks.values():
            if item.get("status") in ("queued", "running"):
                item.update(status="queued", message="服务重启，从已完成的内容继续", error=None)
                resume_ids.append(item["id"])
        self._save()
        for task_id in resume_ids:
            self._start(task_id)

    def _load(self):
        try:
            return json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.state_file)

    def _start(self, task_id: str):
        with self.lock:
            if task_id not in self.tasks:
                return
            self.controls[task_id] = TaskControl()
        self.pool.submit(self._run, task_id)

    def validate(self, payload: dict) -> dict:
        raise NotImplementedError

    def assign_output(self, task_id: str, spec: dict) -> dict:
        raise NotImplementedError

    def execute(self, task_id: str, spec: dict, checkpoint, progress) -> dict:
        raise NotImplementedError

    def discard(self, task_id: str, record: dict) -> None:
        raise NotImplementedError

    def create(self, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError("请求无效")
        spec = self.validate(payload)
        task_id = uuid.uuid4().hex
        spec = self.assign_output(task_id, spec)
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": task_id, "status": "queued", "progress": 0, "current": 0, "total": 0,
            "message": "等待执行", "created_at": now, "updated_at": now,
            "output_path": spec.get("output_path") or "",
            "request": spec, "result": None, "error": None,
        }
        with self.lock:
            self.tasks[task_id] = record
            self._save()
        self._start(task_id)
        return self.get(task_id) or record

    def list(self):
        with self.lock:
            return sorted(self.tasks.values(), key=lambda item: item["created_at"], reverse=True)

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

    def _run(self, task_id: str):
        control = self.controls.get(task_id)
        if not control:
            return
        try:
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                spec = json.loads(json.dumps(task.get("request") or {}))
            control.checkpoint()
            self._update(task_id, status="running", message="任务已开始")
            result = self.execute(task_id, spec, control.checkpoint, lambda a, b, c: self._progress(task_id, a, b, c))
            control.checkpoint()
            self._update(task_id, status="completed", progress=100, message="已完成", result=result, error=None)
        except TaskCancelled:
            self._update(task_id, status="cancelled", message="任务已取消")
        except Exception as exc:
            self._update(task_id, status="failed", message="任务失败", error=str(exc))
        finally:
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
            if task["status"] != "paused":
                raise ValueError("当前状态无法继续")
            if control:
                task.update(status="running", message="任务继续执行", updated_at=datetime.now(timezone.utc).isoformat())
                control.resume()
                self._save()
                return task
            task.update(status="queued", message="服务重启，从已完成的内容继续", error=None,
                        updated_at=datetime.now(timezone.utc).isoformat())
            self._save()
        self._start(task_id)
        return self.get(task_id)

    def retry(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if task["status"] not in ("failed", "cancelled"):
                raise ValueError("仅失败或已取消的任务可以重试")
            task.update(status="queued", progress=0, current=0, total=0,
                        message="等待重新执行", updated_at=datetime.now(timezone.utc).isoformat(),
                        result=None, error=None)
            self._save()
        self._start(task_id)
        return self.get(task_id)

    def delete(self, task_id: str):
        with self.lock:
            task = self.tasks.pop(task_id, None)
            if not task:
                return False
            control = self.controls.get(task_id)
            if control:
                control.cancel()
            self._save()
        self.discard(task_id, task)
        return True
