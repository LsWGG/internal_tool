import copy
import json
import re
import shutil
import subprocess
import threading
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote


class MermaidExportManager:
    """Parse immediately; render individual previews and full exports in a worker."""

    THEMES = {"default", "neutral", "forest", "monochrome", "custom"}
    MONOCHROME_VARIABLES = {
        "primaryColor": "#ffffff", "primaryTextColor": "#111111", "primaryBorderColor": "#111111",
        "lineColor": "#111111", "secondaryColor": "#f5f5f5", "tertiaryColor": "#ffffff",
        "textColor": "#111111", "mainBkg": "#ffffff", "nodeBorder": "#111111",
        "clusterBkg": "#ffffff", "clusterBorder": "#111111",
    }
    FENCE = re.compile(r"```\s*mermaid\s*\n(.*?)```", re.I | re.S)
    HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tasks = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mermaid-render")

    @staticmethod
    def _name(value, fallback):
        # Use one separator and a byte limit so Chinese filenames remain portable.
        value = unicodedata.normalize("NFKC", str(value or ""))
        value = re.sub(r"[^\w\-\u4e00-\u9fff ]+", "_", value, flags=re.UNICODE)
        value = re.sub(r"[\s_]+", "_", value).strip("._- ")
        return value[:120].encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip("_-") or fallback

    @staticmethod
    def _title(value, fallback):
        value = unicodedata.normalize("NFKC", str(value or ""))
        # Warning annotations describe review feedback, not the diagram itself.
        value = re.sub(r"\s*[_*（(\[]*\s*[⚠△].*$", "", value)
        value = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"[*`~]|(?<!\w)_|_(?!\w)", "", value)
        value = re.sub(r"^\s*\d+(?:\.\d+)*[.、)\s]+", "", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value or fallback

    @classmethod
    def _validate_theme(cls, theme, custom):
        if theme not in cls.THEMES:
            raise ValueError("不支持的 Mermaid 主题")
        if custom is not None and not isinstance(custom, dict):
            raise ValueError("自定义主题必须为颜色配置对象")
        colors = custom or {}
        allowed = {"primaryColor", "lineColor", "textColor", "mainBkg"}
        if any(key not in allowed or not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value)
               for key, value in colors.items()):
            raise ValueError("自定义主题仅支持六位十六进制颜色")
        return copy.deepcopy(colors) if theme == "custom" else {}

    @staticmethod
    def _image_records(blocks):
        return [{"index": b["index"], "title": b["title"], "name": b["name"], "status": "idle"}
                for b in blocks]

    @staticmethod
    def _public(task):
        return copy.deepcopy({k: v for k, v in task.items() if k not in ("dir", "blocks")})

    def preview(self, filename, content, theme="neutral", custom=None):
        """Only extract metadata; this must not invoke Mermaid or require its CLI."""
        if not str(filename).lower().endswith((".md", ".markdown")):
            raise ValueError("请选择 .md 或 .markdown 文件")
        colors = self._validate_theme(theme, custom)
        markdown = content.decode("utf-8-sig")
        matches = list(self.FENCE.finditer(markdown))
        if not matches:
            raise ValueError("Markdown 中没有找到 Mermaid 代码块")
        task_id = uuid.uuid4().hex
        task_dir = self.data_dir / task_id
        task_dir.mkdir()
        blocks = []
        for index, match in enumerate(matches, 1):
            headings = [m.group(1) for line in markdown[:match.start()].splitlines()
                        if (m := self.HEADING.match(line))]
            title = self._title(headings[-1] if headings else "", f"Mermaid 图 {index}")
            # The extraction index prevents collisions even for identical headings.
            stem = f"{index:02d}_{self._name(title, 'Mermaid')}"
            blocks.append({"index": index, "title": title,
                           "name": f"{stem}.png", "code": match.group(1).strip()})
        task = {"id": task_id, "name": self._name(Path(filename).stem, "mermaid"),
                "theme": theme, "custom": colors, "revision": 1, "blocks": blocks,
                "images": self._image_records(blocks), "count": len(blocks), "status": "preview",
                "progress": 0, "message": "图片列表已就绪，点击图片生成预览", "dir": str(task_dir)}
        with self.lock:
            self.tasks[task_id] = task
            return self._public(task)

    def status(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            return self._public(task) if task else None

    def delete(self, task_id):
        """Forget an export task and remove its generated previews/archive."""
        with self.lock:
            task = self.tasks.pop(task_id, None)
        if not task:
            return False
        shutil.rmtree(task["dir"], ignore_errors=True)
        return True

    def update_theme(self, task_id, theme, custom=None):
        colors = self._validate_theme(theme, custom)
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if task["status"] == "running":
                raise ValueError("正在导出图片，请等待导出完成后再修改主题")
            if theme != task["theme"] or colors != task["custom"]:
                task_dir = Path(task["dir"])
                # Previous revisions cannot be shown or exported after a theme switch.
                shutil.rmtree(task_dir / "images", ignore_errors=True)
                for path in task_dir.glob("export-r*"):
                    shutil.rmtree(path, ignore_errors=True)
                for path in task_dir.glob("r*-*.zip"):
                    path.unlink(missing_ok=True)
                task.update(theme=theme, custom=colors, revision=task["revision"] + 1,
                            images=self._image_records(task["blocks"]), status="preview", progress=0,
                            message="主题已更新，点击图片生成预览")
                task.pop("archive", None)
                task.pop("error", None)
            return self._public(task)

    def start_preview_render(self, task_id, index):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if index < 1 or index > task["count"]:
                raise ValueError("图片序号不存在")
            record = task["images"][index - 1]
            if record["status"] not in ("queued", "rendering", "ready"):
                record.update(status="queued")
                record.pop("error", None)
                self.pool.submit(self._render_preview, copy.deepcopy(task), index)
            return self._public(task)

    def _render_png(self, task, block, output, scale):
        mmdc = shutil.which("mmdc")
        if not mmdc:
            raise RuntimeError("未找到 Mermaid CLI（mmdc），请先安装 @mermaid-js/mermaid-cli")
        output.parent.mkdir(parents=True, exist_ok=True)
        source = output.with_suffix(".mmd")
        config = None
        try:
            source.write_text(block["code"] + "\n", "utf-8")
            background = task["custom"].get("mainBkg", "transparent") if task["theme"] == "custom" else ("#ffffff" if task["theme"] == "monochrome" else "transparent")
            command = [mmdc, "-i", str(source), "-o", str(output), "-b", background, "-s", str(scale)]
            if task["theme"] == "custom":
                config = output.with_suffix(".json")
                variables = {k: v for k, v in task["custom"].items() if k != "mainBkg"}
                if "primaryColor" in variables:
                    variables["mainBkg"] = variables["primaryColor"]
                if "textColor" in variables:
                    variables["primaryTextColor"] = variables["textColor"]
                config.write_text(json.dumps({"theme": "base", "themeVariables": variables}), "utf-8")
                command += ["-c", str(config)]
            elif task["theme"] == "monochrome":
                config = output.with_suffix(".json")
                config.write_text(json.dumps({"theme": "base", "themeVariables": self.MONOCHROME_VARIABLES}), "utf-8")
                command += ["-c", str(config)]
            else:
                command += ["-t", task["theme"]]
            completed = subprocess.run(command, cwd=task["dir"], capture_output=True, text=True, timeout=120)
            if completed.returncode or not output.is_file():
                detail = (completed.stderr or completed.stdout or "未知错误").strip()[-800:]
                raise RuntimeError(f"第 {block['index']} 张图片渲染失败：{detail}")
        finally:
            source.unlink(missing_ok=True)
            if config:
                config.unlink(missing_ok=True)

    def _render_preview(self, snapshot, index):
        task_id, revision = snapshot["id"], snapshot["revision"]
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task["revision"] != revision:
                return
            task["images"][index - 1]["status"] = "rendering"
        try:
            # Each theme revision has distinct URLs; stale jobs cannot overwrite new images.
            output = Path(snapshot["dir"]) / "images" / f"r{revision}-preview-{index}.png"
            # A 2x lazy preview stays crisp when users zoom without paying the
            # cost of rendering every diagram up front. Exports remain 3x.
            self._render_png(snapshot, snapshot["blocks"][index - 1], output, 2)
            changes = {"status": "ready", "url": f"/api/mermaid/tasks/{task_id}/images/{quote(output.name)}"}
        except Exception as exc:
            changes = {"status": "failed", "error": str(exc)}
        with self.lock:
            task = self.tasks.get(task_id)
            if task and task["revision"] == revision:
                task["images"][index - 1].update(changes)

    def start_render(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if task["status"] not in ("running", "completed"):
                task.update(status="running", progress=1, message="正在排队，准备高清导出…")
                task.pop("error", None)
                self.pool.submit(self._render_task, copy.deepcopy(task))
            return self._public(task)

    def _render_task(self, snapshot):
        task_id = snapshot["id"]
        directory = None
        try:
            directory = Path(snapshot["dir"]) / f"export-r{snapshot['revision']}"
            for offset, block in enumerate(snapshot["blocks"]):
                with self.lock:
                    task = self.tasks.get(task_id)
                    if not task:
                        return
                    task.update(progress=5 + round(offset / snapshot["count"] * 85),
                                message=f"正在渲染第 {offset + 1}/{snapshot['count']} 张高清图片")
                self._render_png(snapshot, block, directory / block["name"], 3)
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                task.update(progress=95, message="图片已生成，正在打包 ZIP…")
            archive = Path(snapshot["dir"]) / f"r{snapshot['revision']}-{snapshot['name']}.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for block in snapshot["blocks"]:
                    bundle.write(directory / block["name"], block["name"])
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                task.update(status="completed", progress=100, message="压缩包已就绪", archive=archive.name)
                (Path(task["dir"]) / "result.json").write_text(json.dumps(task, ensure_ascii=False), "utf-8")
        except Exception as exc:
            with self.lock:
                task = self.tasks.get(task_id)
                if task:
                    task.update(status="failed", message="导出失败，请重试", error=str(exc))
        finally:
            if directory:
                shutil.rmtree(directory, ignore_errors=True)

    def create(self, filename, content, theme="neutral", custom=None):
        """Compatibility for the original synchronous export endpoint."""
        result = self.preview(filename, content, theme, custom)
        with self.lock:
            snapshot = copy.deepcopy(self.tasks[result["id"]])
        self._render_task(snapshot)
        result = self.status(result["id"])
        if result["status"] == "failed":
            raise RuntimeError(result["error"])
        return result

    def image_path(self, task_id, name):
        if not re.fullmatch(r"[0-9a-f]{32}", task_id) or Path(name).name != name:
            return None
        task_dir = self.data_dir / task_id
        path = (task_dir / "images" / name).resolve()
        return path if task_dir in path.parents and path.is_file() else None

    def archive_path(self, task_id):
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            return None
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                metadata = self.data_dir / task_id / "result.json"
                if not metadata.is_file():
                    return None
                task = json.loads(metadata.read_text("utf-8"))
            if task.get("status") != "completed" or not task.get("archive"):
                return None
            task_dir = self.data_dir / task_id
            path = (task_dir / task["archive"]).resolve()
            return path if task_dir in path.parents and path.is_file() else None
