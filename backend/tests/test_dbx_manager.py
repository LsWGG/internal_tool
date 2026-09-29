import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.dbx_manager import DBXManager


class DBXManagerTests(unittest.TestCase):
    def test_resolves_supported_linux_architectures(self):
        manager = DBXManager(Path("/tmp/dbx"))
        with patch("app.dbx_manager.platform.machine", return_value="x86_64"):
            self.assertEqual(manager.architecture(), "linux-x64")
        with patch("app.dbx_manager.platform.machine", return_value="aarch64"):
            self.assertEqual(manager.architecture(), "linux-arm64")

    def test_reports_missing_package_without_spawning_a_process(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = DBXManager(Path(folder))
            with patch.object(manager, "_reachable", return_value=False), patch(
                "app.dbx_manager.platform.machine", return_value="x86_64"
            ):
                state = manager.start()
            self.assertFalse(state["running"])
            self.assertIn("未找到 linux-x64 对应的 DBX 安装包", state["error"])
