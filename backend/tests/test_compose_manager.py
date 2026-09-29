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

    def test_validate_accepts_a_readable_compose_file(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            result = manager.validate(
                "services:\n  app:\n    image: nginx:alpine\n    ports:\n      - '8080:80'\n"
                "volumes:\n  data:\nx-note: 随便写\n")
        self.assertEqual(result["problems"], [])

    def test_validate_reports_syntax_errors_with_a_line(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            problems = manager.validate("services:\n  app:\n   image: [\n")["problems"]
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["severity"], "error")
        # 报的是**行号**（1 起），编辑器按行划线。PyYAML 的 mark 落在文末那个空行上（第 4 行），
        # 但空行划不出波浪线、也没法下手，所以退回最后一个有内容的行 —— 也就是该闭合的 `[`。
        self.assertEqual(problems[0]["line"], 3)
        self.assertIn("expected the node content", problems[0]["text"])

    def test_validate_keeps_the_save_path_rules_at_error_level(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            empty = manager.validate("   \n")["problems"]
            missing = manager.validate("version: '3'\n")["problems"]
            big = manager.validate("services:\n  a:\n    image: x\n" + "#" * 500_001)["problems"]
        self.assertEqual([item["severity"] for item in empty], ["error"])
        self.assertEqual([item["severity"] for item in missing], ["error"])
        self.assertIn("services", missing[0]["text"])
        self.assertEqual([item["severity"] for item in big], ["error"])
        for item in (empty + missing + big):
            self.assertEqual(item["line"], 1)
            # 保存那条路会拒的东西，这里必须也是错误级 —— 否则「编辑器不划线但保存失败」。
            self.assertTrue(item["text"])

    def test_validate_warns_but_does_not_block(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            result = manager.validate("services:\n  app:\n    command: echo hi\n"
                                      "  app:\n    image: x\nservies: {}\n")
        severity = {item["severity"] for item in result["problems"]}
        text = " ".join(item["text"] for item in result["problems"])
        self.assertEqual(severity, {"warning"})          # 一条错误都没有：不该拦着不让保存
        self.assertIn("重复", text)                      # 服务重复定义
        self.assertIn("image", text)                     # 服务既没有 image 也没有 build
        self.assertIn("servies", text)                   # 顶层字段拼错

    def test_validate_accepts_compose_only_syntax(self):
        """`!reset` / `!override` 是 compose 自己的标签：组合阶段不构造对象，所以不算错。"""
        with tempfile.TemporaryDirectory() as folder:
            manager = ComposeManager(Path(folder))
            result = manager.validate("services:\n  app:\n    image: nginx\n    ports: !reset []\n")
        self.assertEqual(result["problems"], [])
