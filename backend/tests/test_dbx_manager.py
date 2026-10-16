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
            self.assertEqual(state["embed_path"], "/dbx/")
            self.assertIn("未找到 linux-x64 对应的 DBX 安装包", state["error"])

    def test_launch_serves_dbx_on_the_same_origin_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "vendor" / "dbx" / "linux-x64"
            package.mkdir(parents=True)
            launcher = package / "dbx"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            manager = DBXManager(root, port=4291)
            with patch.object(manager, "_reachable", return_value=False), patch.object(
                manager, "_stop_stale_dbx"
            ), patch("app.dbx_manager.platform.machine", return_value="x86_64"), patch(
                "app.dbx_manager.subprocess.Popen"
            ) as popen:
                popen.return_value.poll.return_value = None
                popen.return_value.stderr = None
                manager.start()
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["DBX_PUBLIC_BASE_PATH"], "/dbx")
            self.assertEqual(environment["DBX_PORT"], "4291")
