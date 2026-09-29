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

import yaml


class ComposeManager:
    """Stores compose files locally and only executes commands for stored projects."""

    # compose 规范里的顶层字段；`x-` 前缀是规范留给扩展的，不算错。
    TOP_LEVEL_FIELDS = frozenset(
        {"name", "version", "services", "networks", "volumes", "configs", "secrets", "include"})

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

    @staticmethod
    def _key_name(node) -> str:
        """映射键的文本。键不是标量（`? [a, b]`）时给空串：这里只做提示，不替用户解释。"""
        return node.value if isinstance(node, yaml.ScalarNode) else ""

    def validate(self, content: str) -> dict:
        """只读地检查一段 compose 文本，返回 {"problems": [{"line", "text", "severity"}]}。

        编辑器用它划波浪线，因此**不落盘、不碰 docker、不抛异常**。

        用 `yaml.compose_all` 拿节点树而不是 `safe_load`：节点的组合阶段不会构造对象，
        于是 `!reset` / `!override` 这类 compose 自己的标签不会被判成「不认识的标签」
        （构造阶段才会），而我们照样能拿到每个键的行号、看出重复的键。
        报出来的行号是 1 起、且一定落在有内容的行上 —— 编辑器按行划线。

        检查口径和 `_validate_content` 对齐：保存那条路会拒的（太大、没有 services），
        这里必须也是错误级，否则会出现「编辑器不划线但保存失败」。其余（没写 image、
        顶层字段不认识、键重复）是警告：compose 多半跑不起来，但不是这个文件语法上的错。
        """
        problems: list[dict] = []
        source = (content or "").replace("\r\n", "\n")
        rows = source.split("\n")

        def note(line: int, message: str, severity: str = "error") -> None:
            """行号一律落在**有内容**的行上。

            空白行既划不出波浪线（零长度的标记，编辑器画不出来），用户看着也不知道该改什么。
            PyYAML 遇到「写到一半就断了」的文件时，problem_mark 指的是 EOF —— 那往往正是文末
            那个空行，往回退到最后一个有内容的行，才是他该下手的地方。
            """
            line = max(1, int(line))
            while line > 1 and line <= len(rows) and not rows[line - 1].strip():
                line -= 1
            problems.append({"line": line, "text": message, "severity": severity})

        if not source.strip():
            note(1, "compose 文件是空的")
            return {"problems": problems}
        if len(source) > 500_000:
            note(1, "compose 文件不能超过 500 KB")
            return {"problems": problems}
        try:
            documents = list(yaml.compose_all(source))
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
            note(mark.line + 1 if mark else 1, getattr(error, "problem", None) or "YAML 语法错误")
            return {"problems": problems}
        if not documents or documents[0] is None:
            note(1, "compose 文件是空的")
            return {"problems": problems}
        if len(documents) > 1:
            note(1, "文件里有多个 YAML 文档，Docker Compose 只读第一个", "warning")
        root = documents[0]
        if not isinstance(root, yaml.MappingNode):
            note(root.start_mark.line + 1, "顶层必须是一组键值（services: …）")
            return {"problems": problems}

        fields: dict[str, object] = {}
        lines: dict[str, int] = {}
        for key_node, value_node in root.value:
            name = self._key_name(key_node)
            line = key_node.start_mark.line + 1
            if name in fields:
                note(line, f"顶层字段 “{name}” 重复定义，只有最后一个生效")
            fields[name], lines[name] = value_node, line
        if "services" not in fields:
            note(1, "缺少 services: —— compose 文件至少要有一个服务")
            return {"problems": problems}

        services = fields["services"]
        if not isinstance(services, yaml.MappingNode) or not services.value:
            note(services.start_mark.line + 1, "services: 下面还没有服务")
            return {"problems": problems}
        # 服务名重复要在**同一层**里看：下面那条只查服务内部的字段。划在最后一次出现的行上。
        service_lines: dict[str, list[int]] = {}
        for item, _ in services.value:
            service_lines.setdefault(self._key_name(item), []).append(item.start_mark.line + 1)
        for name, seen in service_lines.items():
            if len(seen) > 1:
                note(seen[-1], f"服务 “{name}” 重复定义，只有最后一个生效", "warning")
        for key_node, service in services.value:
            name, line = self._key_name(key_node), key_node.start_mark.line + 1
            if not isinstance(service, yaml.MappingNode):
                note(line, f"服务 “{name}” 必须是一组键值（image: …）")
                continue
            keys = [self._key_name(item) for item, _ in service.value]
            if "image" not in keys and "build" not in keys:
                note(line, f"服务 “{name}” 既没有 image 也没有 build", "warning")
            for field in dict.fromkeys(keys):
                if keys.count(field) > 1:
                    note(line, f"服务 “{name}” 里的 “{field}” 重复定义，只有最后一个生效")
        for name, line in lines.items():
            if name not in self.TOP_LEVEL_FIELDS and not name.startswith("x-"):
                note(line, f"顶层字段 “{name}” 不是 compose 认识的字段", "warning")
        return {"problems": problems}

    def configure_access(self, use_sudo: bool, password: str = "") -> dict:
        with self.lock:
            self.use_sudo = bool(use_sudo)
            self.sudo_password = (password or self.sudo_password) if self.use_sudo else ""
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
