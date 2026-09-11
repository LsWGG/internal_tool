import json
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .models import DockerCreateTask


class DockerTaskManager:
    def __init__(self, data_dir: Path, script_path: Path):
        self.data_dir, self.script_path = data_dir, script_path
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.processes = {}
        self.cancelled = set()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="docker-task")
        try:
            self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.tasks = {}
        for task in self.tasks.values():
            if task["status"] in ("queued", "running", "paused"):
                task.update(status="failed", message="服务重启，任务已中断")
        self._save()

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.state_file)

    def _update(self, task_id, **values):
        with self.lock:
            if task_id not in self.tasks:
                return
            values["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.tasks[task_id].update(values)
            self._save()

    def create(self, request: DockerCreateTask):
        task_id, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        docker_ver = request.docker_version or "latest"
        compose_ver = request.compose_version or "latest"
        record = {"id": task_id, "status": "queued", "progress": 0, "message": "等待执行",
                  "created_at": now, "updated_at": now, "request": request.model_dump(),
                  "name": f"Docker-{docker_ver}_Compose-{compose_ver}_{request.arch}",
                  "result": None, "error": None}
        with self.lock:
            self.cancelled.discard(task_id)
            self.tasks[task_id] = record
            self._save()
        self.pool.submit(self._run, task_id, request)
        return record

    def list(self):
        with self.lock:
            return sorted(self.tasks.values(), key=lambda x: x["created_at"], reverse=True)

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    def _run(self, task_id, request):
        with self.lock:
            if task_id in self.cancelled or task_id not in self.tasks:
                self.cancelled.discard(task_id)
                return
        task_dir = self.data_dir / task_id
        package_dir = task_dir / "docker-offline-packages"
        task_dir.mkdir(parents=True, exist_ok=True)
        command = ["bash", str(self.script_path), "-a", request.arch, "-o", str(package_dir)]
        if request.docker_version:
            command += ["-d", request.docker_version]
        if request.compose_version:
            command += ["-c", request.compose_version]
        try:
            self._update(task_id, status="running", progress=3, message="检查网络与版本信息")
            process = subprocess.Popen(command, cwd=task_dir, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
            with self.lock:
                self.processes[task_id] = process
                cancelled_now = task_id in self.cancelled or task_id not in self.tasks
            if cancelled_now and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            recent = []
            for raw in process.stdout or []:
                line = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
                if not line:
                    continue
                recent = (recent + [line])[-15:]
                progress = self.tasks.get(task_id, {}).get("progress", 3)
                if "开始下载 Docker" in line: progress = max(progress, 20)
                elif "Docker 下载成功" in line: progress = max(progress, 55)
                elif "开始下载 docker-compose" in line: progress = max(progress, 60)
                elif "docker-compose 下载成功" in line: progress = max(progress, 88)
                elif "安装脚本已生成" in line: progress = max(progress, 94)
                self._update(task_id, progress=progress, message=line[-180:])
            code = process.wait()
            if task_id not in self.tasks:
                return
            if code:
                raise RuntimeError(" | ".join(recent[-6:]) or f"下载脚本退出码 {code}")
            archive = shutil.make_archive(str(task_dir), "zip", package_dir)
            files = [{"name": str(p.relative_to(package_dir)), "size": p.stat().st_size}
                     for p in package_dir.rglob("*") if p.is_file()]
            self._update(task_id, status="completed", progress=100, message="离线包制作完成",
                         result={"archive": Path(archive).name, "files": files})
        except Exception as exc:
            self._update(task_id, status="failed", message="任务失败", error=str(exc))
        finally:
            with self.lock:
                should_clean = task_id in self.cancelled or task_id not in self.tasks
                self.processes.pop(task_id, None)
                self.cancelled.discard(task_id)
            if should_clean:
                shutil.rmtree(task_dir, ignore_errors=True)
                (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)

    def control(self, task_id, action):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            process = self.processes.get(task_id)
            if action == "pause" and task["status"] == "running":
                if process: os.killpg(process.pid, signal.SIGSTOP)
                task.update(status="paused", message="任务已暂停")
            elif action == "resume" and task["status"] == "paused":
                if process: os.killpg(process.pid, signal.SIGCONT)
                task.update(status="running", message="任务继续执行")
            else:
                raise ValueError("当前状态不支持此操作")
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return task

    def delete(self, task_id):
        with self.lock:
            task = self.tasks.pop(task_id, None)
            process = self.processes.pop(task_id, None)
            if not task: return False
            self.cancelled.add(task_id)
            self._save()
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True)
        (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
        return True

    def retry(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task: return None
            if task["status"] != "failed": raise ValueError("仅失败任务可以重试")
            request = DockerCreateTask.model_validate(task["request"])
            self.cancelled.discard(task_id)
            task.update(status="queued", progress=0, message="等待重新执行", error=None, result=None)
            self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True)
        (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
        self.pool.submit(self._run, task_id, request)
        return task
