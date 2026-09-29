"""Safe local management of user-owned Docker Compose projects."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ComposeManager:
    """Stores compose files locally and only executes commands for stored projects."""

    def __init__(self, root: Path):
        self.root = root
        self.projects_dir = root / "projects"
        self.state_file = root / "projects.json"
        self.lock = threading.RLock()
        # Deliberately memory-only: a local sudo password must never be written
        # to compose files, metadata, logs, or the project repository.
        self.use_sudo = False
        self.sudo_password = ""
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.projects = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.projects = {}

    def _save(self) -> None:
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.projects, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.state_file)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _clean_name(value: str) -> str:
        value = re.sub(r"\s+", " ", (value or "").strip())
        if not value or len(value) > 80:
            raise ValueError("项目名称需为 1–80 个字符")
        return value

    @staticmethod
    def _validate_content(value: str) -> str:
        value = (value or "").replace("\r\n", "\n").strip() + "\n"
        if len(value) > 500_000:
            raise ValueError("compose 文件不能超过 500 KB")
        if not re.search(r"^\s*services\s*:", value, re.MULTILINE):
            raise ValueError("compose 文件必须包含 services: 配置")
        return value

    def configure_access(self, use_sudo: bool, password: str = "") -> dict:
        with self.lock:
            self.use_sudo = bool(use_sudo)
            self.sudo_password = password if self.use_sudo else ""
        return self.access()

    def access(self) -> dict:
        with self.lock:
            return {"use_sudo": self.use_sudo, "password_set": bool(self.sudo_password)}

    def _run(self, command: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
        with self.lock:
            use_sudo, password = self.use_sudo, self.sudo_password
        if use_sudo:
            if not shutil.which("sudo"):
                raise RuntimeError("未找到 sudo 命令")
            if not password:
                raise RuntimeError("已启用 sudo，请先在页面填写 sudo 密码")
            command = ["sudo", "-S", "-p", "", *command]
        return subprocess.run(command, cwd=cwd, input=f"{password}\n" if use_sudo else None,
                              capture_output=True, text=True, timeout=timeout)

    def _compose_prefix(self) -> list[str] | None:
        if shutil.which("docker"):
            probe = self._run(["docker", "compose", "version"], timeout=8)
            if probe.returncode == 0:
                return ["docker", "compose"]
        if shutil.which("docker-compose"):
            return ["docker-compose"]
        return None

    def status(self) -> dict:
        docker_bin = shutil.which("docker")
        try:
            compose = self._compose_prefix()
        except RuntimeError as exc:
            result = {"available": False, "docker_version": "", "compose_version": "", "message": str(exc), "compose_command": ""}
            return {**result, **self.access()}
        result = {"available": False, "docker_version": "", "compose_version": "", "message": "未检测到 Docker 服务", "compose_command": ""}
        if not docker_bin:
            result["message"] = "未找到 docker 命令，请先安装并启动 Docker。"
            return {**result, **self.access()}
        try:
            version = self._run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=8)
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            result["message"] = "无法连接 Docker 守护进程。"
            return {**result, **self.access()}
        if version.returncode != 0:
            result["message"] = (version.stderr.strip() or "Docker 守护进程未运行。")[-300:]
            return {**result, **self.access()}
        result["docker_version"] = version.stdout.strip()
        if not compose:
            result["message"] = "Docker 已运行，但未检测到 Docker Compose 插件。"
            return {**result, **self.access()}
        try:
            compose_version = self._run([*compose, "version", "--short"], timeout=8)
            result["compose_version"] = compose_version.stdout.strip() if compose_version.returncode == 0 else "已安装"
        except (OSError, subprocess.TimeoutExpired):
            result["compose_version"] = "已安装"
        result.update(available=True, message="Docker 与 Compose 已就绪", compose_command=" ".join(compose))
        return {**result, **self.access()}

    def _project_dir(self, project_id: str) -> Path:
        project = self.projects.get(project_id)
        if not project:
            raise KeyError(project_id)
        return self.projects_dir / project_id

    def _compose_file(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "compose.yaml"

    def create(self, name: str, content: str) -> dict:
        name, content = self._clean_name(name), self._validate_content(content)
        project_id, now = uuid.uuid4().hex, self._now()
        record = {"id": project_id, "name": name, "created_at": now, "updated_at": now}
        with self.lock:
            folder = self.projects_dir / project_id
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "compose.yaml").write_text(content, "utf-8")
            self.projects[project_id] = record
            self._save()
        return self.get(project_id)

    def update(self, project_id: str, name: str, content: str) -> dict:
        name, content = self._clean_name(name), self._validate_content(content)
        with self.lock:
            if project_id not in self.projects:
                raise KeyError(project_id)
            self._compose_file(project_id).write_text(content, "utf-8")
            self.projects[project_id].update(name=name, updated_at=self._now())
            self._save()
        return self.get(project_id)

    def _services(self, project_id: str) -> list[dict]:
        prefix = self._compose_prefix()
        if not prefix:
            return []
        command = [*prefix, "-f", str(self._compose_file(project_id)), "ps", "--format", "json"]
        try:
            outcome = self._run(command, timeout=15)
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            return []
        if outcome.returncode != 0 or not outcome.stdout.strip():
            return []
        services = []
        for line in outcome.stdout.splitlines():
            try:
                item = json.loads(line)
                services.append({"name": item.get("Service") or item.get("Name") or "服务", "state": item.get("State", "unknown"), "status": item.get("Status", "")})
            except json.JSONDecodeError:
                continue
        return services

    def get(self, project_id: str) -> dict:
        with self.lock:
            project = self.projects.get(project_id)
            if not project:
                raise KeyError(project_id)
            result = dict(project)
            result["content"] = self._compose_file(project_id).read_text("utf-8")
        result["services"] = self._services(project_id)
        return result

    def list(self) -> list[dict]:
        with self.lock:
            ids = sorted(self.projects, key=lambda key: self.projects[key]["updated_at"], reverse=True)
        return [self.get(project_id) for project_id in ids]

    def _run_action(self, project_id: str, action: str) -> dict:
        prefix = self._compose_prefix()
        if not prefix:
            raise RuntimeError("Docker Compose 不可用，请先检查 Docker 服务。")
        actions = {
            "start": ["up", "-d", "--remove-orphans"],
            "stop": ["stop"],
            "restart": ["restart"],
            "down": ["down"],
        }
        if action not in actions:
            raise ValueError("不支持的服务操作")
        try:
            outcome = self._run([*prefix, "-f", str(self._compose_file(project_id)), *actions[action]], cwd=self._project_dir(project_id), timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("操作超时，请查看 Docker 状态。") from exc
        if outcome.returncode != 0:
            raise RuntimeError((outcome.stderr.strip() or outcome.stdout.strip() or "Docker Compose 操作失败")[-1000:])
        with self.lock:
            self.projects[project_id]["updated_at"] = self._now()
            self._save()
        return self.get(project_id)

    def action(self, project_id: str, action: str) -> dict:
        self._project_dir(project_id)
        return self._run_action(project_id, action)

    def logs(self, project_id: str, service: str = "", tail: int = 200) -> str:
        prefix = self._compose_prefix()
        if not prefix:
            raise RuntimeError("Docker Compose 不可用，请先检查 Docker 服务。")
        tail = max(20, min(int(tail), 2000))
        command = [*prefix, "-f", str(self._compose_file(project_id)), "logs", "--tail", str(tail), "--no-color"]
        if service:
            command.append(service)
        try:
            outcome = self._run(command, cwd=self._project_dir(project_id), timeout=40)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("日志读取超时") from exc
        if outcome.returncode != 0:
            raise RuntimeError((outcome.stderr.strip() or "无法读取日志")[-1000:])
        return outcome.stdout[-200_000:]

    def delete(self, project_id: str) -> None:
        with self.lock:
            if project_id not in self.projects:
                raise KeyError(project_id)
            del self.projects[project_id]
            self._save()
        shutil.rmtree(self.projects_dir / project_id, ignore_errors=True)
