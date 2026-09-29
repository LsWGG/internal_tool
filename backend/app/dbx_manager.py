"""Local DBX launcher for the bundled browser packages."""
from __future__ import annotations

import os
import platform
import subprocess
import threading
from pathlib import Path

import requests


class DBXManager:
    """Start one architecture-matched DBX process and keep its data outside Git."""

    def __init__(self, root: Path | str, *, port: int = 4224):
        self.root = Path(root)
        self.port = port
        self.data_dir = self.root / "dbx_data"
        self.package_root = self.root / "vendor" / "dbx"
        self.process: subprocess.Popen | None = None
        self.error = ""
        self.lock = threading.RLock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def architecture(self) -> str:
        machine = platform.machine().lower()
        if machine in {"x86_64", "amd64"}:
            return "linux-x64"
        if machine in {"aarch64", "arm64"}:
            return "linux-arm64"
        return ""

    def _package(self) -> Path:
        architecture = self.architecture()
        return self.package_root / architecture if architecture else Path()

    def _reachable(self) -> bool:
        try:
            return requests.get(self.url, timeout=(0.5, 1.5)).ok
        except requests.RequestException:
            return False

    def start(self) -> dict:
        with self.lock:
            if self._reachable():
                return self.status()
            if self.process and self.process.poll() is None:
                return self.status()

            package = self._package()
            launcher = package / "dbx"
            if not self.architecture():
                self.error = f"不支持的 DBX 运行架构：{platform.machine()}"
                return self.status()
            if not launcher.is_file() or not os.access(launcher, os.X_OK):
                self.error = f"未找到 {self.architecture()} 对应的 DBX 安装包"
                return self.status()

            self.data_dir.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment.update({"DBX_PORT": str(self.port), "DBX_DATA_DIR": str(self.data_dir)})
            try:
                self.process = subprocess.Popen(
                    [str(launcher)], cwd=package, env=environment,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                self.error = ""
            except OSError as exc:
                self.process = None
                self.error = f"DBX 启动失败：{exc}"
            return self.status()

    def status(self) -> dict:
        with self.lock:
            reachable = self._reachable()
            if self.process and self.process.poll() is not None and not reachable:
                detail = self.process.stderr.read().strip() if self.process.stderr else ""
                self.error = detail[-600:] or f"DBX 已退出（退出码 {self.process.returncode}）"
            return {
                "running": reachable,
                "starting": bool(self.process and self.process.poll() is None and not reachable),
                "url": self.url,
                "architecture": self.architecture() or platform.machine(),
                "error": self.error,
            }

    def stop(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None
