import json
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


class MarkdownWordManager:
    MERMAID_SCALE = 3
    MERMAID_MAX_WIDTH_IN = 6.2
    MERMAID_MAX_HEIGHT_IN = 8.4

    def __init__(self, data_dir: Path, reference_doc: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reference_doc = Path(reference_doc).resolve()
        if not self.reference_doc.is_file():
            raise RuntimeError(f"Word 参考模板不存在：{self.reference_doc}")
        self.state_file = self.data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="md-word")
        try:
            self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            self.tasks = {}
        for task in self.tasks.values():
            if task.get("status") in ("queued", "running"):
                task.update(status="failed", message="服务重启，转换已中断", error="可点击重新转换")
        self._save()

    def _save(self):
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.state_file)

    def _update(self, task_id, **values):
        with self.lock:
            if task_id not in self.tasks:
                return
            values["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.tasks[task_id].update(values)
            self._save()

    @staticmethod
    def _binary(name, fallbacks):
        found = shutil.which(name)
        if found:
            return found
        for value in fallbacks:
            if Path(value).exists():
                return value
        raise RuntimeError(f"未找到 {name}，请安装后重启服务")

    def create(self, filename: str, content: bytes, title: str = "", toc: bool = True,
               mermaid_theme: str = "neutral"):
        if not filename.lower().endswith((".md", ".markdown")):
            raise ValueError("请选择 .md 或 .markdown 文件")
        if not content:
            raise ValueError("Markdown 文件为空")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("Markdown 文件不能超过 20MB")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown 文件必须使用 UTF-8 编码") from exc
        if mermaid_theme not in ("default", "neutral", "forest"):
            raise ValueError("不支持的 Mermaid 主题")
        task_id = uuid.uuid4().hex
        task_dir = self.data_dir / task_id
        task_dir.mkdir(parents=True)
        source_name = Path(filename).stem + ".md"
        (task_dir / source_name).write_bytes(content)
        now = datetime.now(timezone.utc).isoformat()
        display_name = title.strip() or Path(filename).stem
        output_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", display_name).strip(" .")[:120]
        output_name = output_name or "Markdown 文档"
        record = {"id": task_id, "name": display_name, "output_name": output_name,
                  "source_name": source_name, "status": "queued", "progress": 0,
                  "message": "等待转换", "created_at": now, "updated_at": now,
                  "options": {"title": title.strip(), "toc": bool(toc),
                              "mermaid_theme": mermaid_theme},
                  "result": None, "error": None}
        with self.lock:
            self.tasks[task_id] = record
            self._save()
        self.pool.submit(self._run, task_id)
        return record

    def _render_mermaid(self, task_id: str, markdown: str, task_dir: Path, theme: str):
        pattern = re.compile(r"```mermaid\s*\n(.*?)```", re.I | re.S)
        blocks = list(pattern.finditer(markdown))
        if not blocks:
            return markdown, []
        # Resolve Mermaid CLI from PATH so the project has no machine-specific dependency.
        mmdc = self._binary("mmdc", [])
        diagram_dir = task_dir / "mermaid"
        diagram_dir.mkdir(exist_ok=True)
        replacements = []
        diagrams = []
        for index, match in enumerate(blocks, 1):
            self._update(task_id, progress=10 + round(index / len(blocks) * 35),
                         message=f"正在渲染 Mermaid 图 {index}/{len(blocks)}")
            source = diagram_dir / f"diagram-{index:03d}.mmd"
            image = diagram_dir / f"diagram-{index:03d}.png"
            source.write_text(match.group(1).strip() + "\n", "utf-8")
            command = [mmdc, "-i", str(source), "-o", str(image), "-t", theme,
                       "-b", "transparent", "-s", str(self.MERMAID_SCALE), "-w", "1600"]
            result = subprocess.run(command, cwd=task_dir, capture_output=True, text=True, timeout=120)
            if result.returncode or not image.exists():
                detail = (result.stderr or result.stdout or "未知错误").strip()[-1200:]
                raise RuntimeError(f"Mermaid 图 {index} 渲染失败：{detail}")
            with Image.open(image) as rendered:
                pixel_width, pixel_height = rendered.size
            natural_width = pixel_width / (96 * self.MERMAID_SCALE)
            natural_height = pixel_height / (96 * self.MERMAID_SCALE)
            ratio = min(1.0, self.MERMAID_MAX_WIDTH_IN / natural_width,
                        self.MERMAID_MAX_HEIGHT_IN / natural_height)
            word_width = natural_width * ratio
            word_height = natural_height * ratio
            attributes = f"{{width={word_width:.3f}in height={word_height:.3f}in}}"
            replacements.append((match.start(), match.end(),
                                 f"\n![Mermaid 图 {index}](mermaid/{image.name}){attributes}\n"))
            diagrams.append({"index": index, "pixel_width": pixel_width,
                             "pixel_height": pixel_height,
                             "word_width_in": round(word_width, 3),
                             "word_height_in": round(word_height, 3),
                             "adaptively_scaled": ratio < 0.999})
        for start, end, value in reversed(replacements):
            markdown = markdown[:start] + value + markdown[end:]
        return markdown, diagrams

    @staticmethod
    def _center_docx_images(docx: Path):
        """Center every inline/floating image paragraph without changing image size."""
        temp = docx.with_suffix(".centered.tmp")
        centered = 0
        try:
            with zipfile.ZipFile(docx, "r") as source, zipfile.ZipFile(temp, "w") as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == "word/document.xml":
                        xml = data.decode("utf-8")

                        def center_paragraph(match):
                            nonlocal centered
                            paragraph = match.group(0)
                            if "<w:drawing" not in paragraph and "<w:pict" not in paragraph:
                                return paragraph
                            centered += 1
                            paragraph = re.sub(r"<w:jc\b[^>]*/>", "", paragraph)
                            if "<w:pPr" in paragraph:
                                return paragraph.replace("</w:pPr>", '<w:jc w:val="center"/></w:pPr>', 1)
                            opening = re.match(r"<w:p(?:\s[^>]*)?>", paragraph)
                            if not opening:
                                return paragraph
                            return (paragraph[:opening.end()] + '<w:pPr><w:jc w:val="center"/></w:pPr>'
                                    + paragraph[opening.end():])

                        xml = re.sub(r"<w:p(?:\s[^>]*)?>.*?</w:p>", center_paragraph, xml,
                                     flags=re.DOTALL)
                        data = xml.encode("utf-8")
                    target.writestr(info, data)
            temp.replace(docx)
            return centered
        finally:
            temp.unlink(missing_ok=True)

    def _create_preview(self, prepared: Path, docx: Path, pdf: Path, task_dir: Path, pandoc: str):
        candidates = [shutil.which("soffice"), "/opt/homebrew/bin/soffice"]
        errors = []
        seen = set()
        for candidate in candidates:
            if not candidate or candidate in seen or not Path(candidate).is_file():
                continue
            seen.add(candidate)
            pdf.unlink(missing_ok=True)
            try:
                result = subprocess.run(
                    [candidate, "--headless", "--convert-to", "pdf", "--outdir", str(task_dir), str(docx)],
                    cwd=task_dir, capture_output=True, text=True, timeout=300,
                )
                if result.returncode == 0 and pdf.is_file():
                    return
                errors.append((result.stderr or result.stdout or candidate).strip()[-500:])
            except Exception as exc:
                errors.append(str(exc))

        # LibreOffice is optional for the web preview. When it is unavailable,
        # render the same prepared Markdown to paged HTML and print it with Chromium.
        html = task_dir / "preview.html"
        css = task_dir / "preview.css"
        css.write_text(
            "@page{size:A4;margin:20mm 22mm}body{font-family:Aptos,'PingFang SC',sans-serif;"
            "color:#26364d;line-height:1.55;font-size:14px}h1,h2,h3{color:#245fa8;page-break-after:avoid}"
            "h1{font-size:30px}h2{font-size:22px}h3{font-size:17px}table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #b9c7d8;padding:7px 9px}th{background:#e9f1fb}"
            "img{display:block;max-width:100%;height:auto;margin-left:auto;margin-right:auto}"
            "pre{background:#f1f4f8;padding:12px;border-radius:6px;white-space:pre-wrap;word-break:break-word}"
            "code{font-family:'Cascadia Mono',monospace}blockquote{border-left:4px solid #7aa3d5;margin-left:0;padding-left:14px}",
            "utf-8",
        )
        result = subprocess.run(
            [pandoc, str(prepared), "--from=markdown+autolink_bare_uris", "--to=html5", "--standalone",
             f"--resource-path={task_dir}", f"--css={css.name}", "-o", str(html)],
            cwd=task_dir, capture_output=True, text=True, timeout=300,
        )
        if result.returncode or not html.is_file():
            raise RuntimeError("页面预览生成失败：" + (result.stderr or result.stdout or "; ".join(errors))[-1000:])
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(html.as_uri(), wait_until="load")
                page.pdf(path=str(pdf), format="A4", print_background=True,
                         margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
                browser.close()
        except Exception as exc:
            raise RuntimeError("页面预览生成失败，请安装 LibreOffice 或 Playwright Chromium：" + str(exc)) from exc

    def _run(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return
        task_dir = self.data_dir / task_id
        try:
            self._update(task_id, status="running", progress=5, message="正在解析 Markdown", error=None)
            source = task_dir / task["source_name"]
            markdown = source.read_text("utf-8")
            title = task["options"].get("title")
            if title and not re.match(r"^#\s+", markdown.lstrip()):
                markdown = f"# {title}\n\n" + markdown
            markdown, diagrams = self._render_mermaid(
                task_id, markdown, task_dir, task["options"].get("mermaid_theme", "neutral"))
            prepared = task_dir / "prepared.md"
            prepared.write_text(markdown, "utf-8")
            self._update(task_id, progress=52, message="正在生成高质量 Word 文档")
            pandoc = self._binary("pandoc", ["/opt/homebrew/bin/pandoc"])
            docx = task_dir / f"{task.get('output_name') or task['name']}.docx"
            command = [pandoc, str(prepared), "--from=markdown+autolink_bare_uris", "--to=docx", "--standalone",
                       f"--reference-doc={self.reference_doc}", f"--resource-path={task_dir}",
                       "--metadata=lang:zh-CN", "--metadata=toc-title:目录",
                       "--highlight-style=tango", "--wrap=none", "-o", str(docx)]
            if task["options"].get("toc"):
                command.extend(["--toc", "--toc-depth=3"])
            result = subprocess.run(command, cwd=task_dir, capture_output=True, text=True, timeout=300)
            if result.returncode or not docx.exists():
                raise RuntimeError((result.stderr or result.stdout or "Pandoc 转换失败").strip()[-1600:])
            centered_image_count = self._center_docx_images(docx)
            self._update(task_id, progress=78, message="正在生成页面预览")
            pdf = task_dir / f"{task.get('output_name') or task['name']}.pdf"
            self._create_preview(prepared, docx, pdf, task_dir, pandoc)
            self._update(task_id, status="completed", progress=100, message="转换完成",
                         result={"docx": docx.name, "preview": pdf.name,
                                 "docx_size": docx.stat().st_size,
                                 "mermaid_count": len(diagrams),
                                 "mermaid_images": diagrams,
                                 "centered_image_count": centered_image_count})
        except Exception as exc:
            self._update(task_id, status="failed", message="转换失败", error=str(exc))

    def list(self):
        with self.lock:
            return sorted(self.tasks.values(), key=lambda item: item["created_at"], reverse=True)

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    def delete(self, task_id):
        with self.lock:
            if not self.tasks.pop(task_id, None):
                return False
            self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True)
        return True

    def retry(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if task["status"] != "failed":
                raise ValueError("只有失败任务可以重新转换")
            task.update(status="queued", progress=0, message="等待重新转换", error=None,
                        result=None, updated_at=datetime.now(timezone.utc).isoformat())
            self._save()
        task_dir = self.data_dir / task_id
        for path in task_dir.iterdir():
            if path.name != task["source_name"]:
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
        self.pool.submit(self._run, task_id)
        return task
