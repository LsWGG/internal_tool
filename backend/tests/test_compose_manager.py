import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.compose_manager import ComposeManager


class ComposeManagerTests(unittest.TestCase):
    def test_create_update_and_delete_stored_compose_project(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            project = manager.create("临时测试", "services:\n  app:\n    image: nginx:alpine\n")
            self.assertEqual(project["name"], "临时测试")
            self.assertIn("nginx:alpine", project["content"])
            updated = manager.update(project["id"], "联调环境", "services:\n  redis:\n    image: redis:7-alpine\n")
            self.assertEqual(updated["name"], "联调环境")
            self.assertIn("redis:7-alpine", updated["content"])
            manager.delete(project["id"])
            self.assertEqual(manager.list(), [])

    def test_rejects_compose_without_services(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            with self.assertRaisesRegex(ValueError, "services"):
                manager.create("无效配置", "version: '3'\n")

    def test_reports_when_docker_is_not_installed(self):
        with tempfile.TemporaryDirectory() as folder, patch("app.compose_manager.shutil.which", return_value=None):
            status = ComposeManager(Path(folder)).status()
        self.assertFalse(status["available"])
        self.assertIn("docker", status["message"].lower())

    def test_sudo_password_is_memory_only(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            manager.configure_access(True, "local-secret")
            self.assertTrue(manager.access()["password_set"])
            self.assertFalse((Path(folder) / "projects.json").exists())
            manager.configure_access(False)
            self.assertFalse(manager.access()["password_set"])
