import json
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo
from pillow_heif import register_heif_opener


register_heif_opener()


class ImageConvertManager:
    INPUT_EXTENSIONS = {
        ".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp",
        ".tif", ".tiff", ".bmp",
    }
    OUTPUTS = {
        "heic": {"extension": ".heic", "pillow": "HEIF", "label": "HEIC"},
        "jpeg": {"extension": ".jpg", "pillow": "JPEG", "label": "JPEG"},
        "png": {"extension": ".png", "pillow": "PNG", "label": "PNG"},
        "webp": {"extension": ".webp", "pillow": "WEBP", "label": "WebP"},
        "tiff": {"extension": ".tif", "pillow": "TIFF", "label": "TIFF"},
    }

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.cancelled = set()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="image-convert")
        try:
            self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            self.tasks = {}
        for task in self.tasks.values():
            if task.get("status") in ("queued", "running"):
                task.update(
                    status="failed",
                    message="服务重启，转换已中断",
                    error="原始图片仍保留，可点击重新转换",
                )
        self._save()

    def _save(self):
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.state_file)

    def _update(self, task_id: str, **values):
        with self.lock:
            if task_id not in self.tasks or task_id in self.cancelled:
                return False
            values["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.tasks[task_id].update(values)
            self._save()
            return True

    @staticmethod
    def _safe_display_name(value: str):
        return "".join("_" if char in '\\/:*?\"<>|\x00\r\n' else char for char in value).strip(" .")[:120]

    def create(self, upload_root: Path, relative_files: list[str], output_format: str, quality: int):
        output_format = output_format.lower().strip()
        if output_format not in self.OUTPUTS:
            raise ValueError("不支持的输出格式")
        quality = int(quality)
        if not 1 <= quality <= 100:
            raise ValueError("图片质量必须在 1～100 之间")
        if not relative_files:
            raise ValueError("请选择需要转换的图片")

        task_id = uuid.uuid4().hex
        task_dir = self.data_dir / task_id
        input_dir = task_dir / "input"
        task_dir.mkdir(parents=True)
        try:
            shutil.move(str(upload_root), str(input_dir))
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        label = self.OUTPUTS[output_format]["label"]
        first_name = Path(relative_files[0]).stem
        name = f"{first_name} → {label}" if len(relative_files) == 1 else f"{len(relative_files)} 张图片 → {label}"
        name = self._safe_display_name(name) or f"图片转换 → {label}"
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "id": task_id,
            "name": name,
            "status": "queued",
            "progress": 0,
            "current": 0,
            "total": len(relative_files),
            "message": "等待转换",
            "created_at": now,
            "updated_at": now,
            "options": {"output_format": output_format, "quality": quality},
            "source_files": relative_files,
            "result": None,
            "error": None,
        }
        with self.lock:
            self.cancelled.discard(task_id)
            self.tasks[task_id] = record
            self._save()
        self.pool.submit(self._run, task_id)
        return record

    @staticmethod
    def _metadata(image: Image.Image):
        metadata = {
            "exif": image.info.get("exif"),
            "icc_profile": image.info.get("icc_profile"),
            "xmp": image.info.get("xmp") or image.info.get("XML:com.adobe.xmp"),
        }
        try:
            exif = image.getexif()
            if exif:
                if not metadata.get("xmp") and exif.get(700):
                    metadata["xmp"] = exif.get(700)
                exif[274] = 1
                metadata["exif"] = exif.tobytes()
        except Exception:
            pass
        return {key: value for key, value in metadata.items() if value}

    @staticmethod
    def _prepare_pixels(image: Image.Image, output_format: str):
        image = ImageOps.exif_transpose(image)
        if output_format == "jpeg":
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                return background
            if image.mode not in ("RGB", "L", "CMYK"):
                return image.convert("RGB")
        if output_format in ("heic", "webp") and image.mode not in ("RGB", "RGBA", "L"):
            return image.convert("RGBA" if "A" in image.getbands() else "RGB")
        return image

    @staticmethod
    def _save_image(image: Image.Image, target: Path, output_format: str, quality: int, metadata: dict):
        common = {}
        for key in ("exif", "icc_profile"):
            if metadata.get(key):
                common[key] = metadata[key]
        if output_format in ("jpeg", "webp", "heic") and metadata.get("xmp"):
            common["xmp"] = metadata["xmp"]

        if output_format == "jpeg":
            image.save(target, "JPEG", quality=quality, optimize=True, **common)
        elif output_format == "webp":
            image.save(target, "WEBP", quality=quality, method=6, **common)
        elif output_format == "heic":
            image.save(target, "HEIF", quality=quality, **common)
        elif output_format == "png":
            pnginfo = None
            if metadata.get("xmp"):
                pnginfo = PngInfo()
                value = metadata["xmp"]
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                pnginfo.add_itxt("XML:com.adobe.xmp", value)
            image.save(target, "PNG", optimize=True, pnginfo=pnginfo, **common)
        elif output_format == "tiff":
            tiff_options = dict(common)
            if metadata.get("xmp"):
                exif = Image.Exif()
                if metadata.get("exif"):
                    exif.load(metadata["exif"])
                xmp = metadata["xmp"]
                exif[700] = xmp.encode("utf-8") if isinstance(xmp, str) else xmp
                tiff_options["exif"] = exif.tobytes()
            image.save(target, "TIFF", compression="tiff_deflate", **tiff_options)

    @staticmethod
    def _unique_target(output_dir: Path, relative_source: str, extension: str):
        relative = Path(relative_source)
        target = output_dir / relative.parent / f"{relative.stem}{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            return target
        index = 2
        while True:
            candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
            if not candidate.exists():
                return candidate
            index += 1

    def _run(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task_id in self.cancelled:
                return
            source_files = list(task["source_files"])
            output_format = task["options"]["output_format"]
            quality = task["options"]["quality"]
        task_dir = self.data_dir / task_id
        input_dir = task_dir / "input"
        output_dir = task_dir / "output"
        archive = self.data_dir / f"{task_id}.zip"
        shutil.rmtree(output_dir, ignore_errors=True)
        archive.unlink(missing_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._update(task_id, status="running", progress=1, current=0, message="正在读取图片", error=None, result=None)
        converted = []
        failures = []
        try:
            config = self.OUTPUTS[output_format]
            for index, relative_name in enumerate(source_files, 1):
                with self.lock:
                    if task_id in self.cancelled or task_id not in self.tasks:
                        return
                source = input_dir / Path(relative_name)
                self._update(
                    task_id,
                    current=index - 1,
                    progress=max(1, round((index - 1) / len(source_files) * 88)),
                    message=f"正在转换 {index}/{len(source_files)}：{Path(relative_name).name}",
                )
                try:
                    with Image.open(source) as original:
                        original.load()
                        metadata = self._metadata(original)
                        pixels = self._prepare_pixels(original, output_format)
                        target = self._unique_target(output_dir, relative_name, config["extension"])
                        self._save_image(pixels, target, output_format, quality, metadata)
                        relative_output = target.relative_to(output_dir).as_posix()
                        converted.append({
                            "source": relative_name,
                            "output": relative_output,
                            "width": pixels.width,
                            "height": pixels.height,
                            "metadata": sorted(metadata),
                        })
                except Exception as exc:
                    failures.append({"source": relative_name, "error": str(exc)[:500]})
                self._update(
                    task_id,
                    current=index,
                    progress=max(2, round(index / len(source_files) * 88)),
                )

            if not converted:
                details = "；".join(f"{Path(item['source']).name}：{item['error']}" for item in failures[:3])
                raise RuntimeError(f"全部图片转换失败。{details}")
            self._update(task_id, progress=92, message="正在打包转换结果")
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
                for item in converted:
                    path = output_dir / item["output"]
                    package.write(path, item["output"])
            result = {
                "archive": archive.name,
                "converted": len(converted),
                "failed": len(failures),
                "metadata_preserved": sum(bool(item["metadata"]) for item in converted),
                "files": converted,
                "warnings": failures,
            }
            message = f"已转换 {len(converted)} 张图片"
            if failures:
                message += f"，{len(failures)} 张失败"
            self._update(
                task_id,
                status="completed",
                progress=100,
                current=len(source_files),
                message=message,
                result=result,
                error=None,
            )
        except Exception as exc:
            self._update(task_id, status="failed", message="转换失败", error=str(exc), result=None)
        finally:
            with self.lock:
                was_cancelled = task_id in self.cancelled
                if was_cancelled:
                    self.cancelled.discard(task_id)
            if was_cancelled:
                shutil.rmtree(task_dir, ignore_errors=True)
                archive.unlink(missing_ok=True)

    def list(self):
        with self.lock:
            return sorted((dict(task) for task in self.tasks.values()), key=lambda item: item["created_at"], reverse=True)

    def get(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    def retry(self, task_id: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            if task.get("status") != "failed":
                raise ValueError("仅失败任务可以重新转换")
            if not (self.data_dir / task_id / "input").is_dir():
                raise ValueError("原始图片已丢失，无法重新转换")
            self.cancelled.discard(task_id)
            task.update(
                status="queued", progress=0, current=0, message="等待重新转换",
                result=None, error=None, updated_at=datetime.now(timezone.utc).isoformat(),
            )
            self._save()
            result = dict(task)
        self.pool.submit(self._run, task_id)
        return result

    def delete(self, task_id: str):
        with self.lock:
            if task_id not in self.tasks:
                return False
            self.cancelled.add(task_id)
            self.tasks.pop(task_id)
            self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True)
        (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True)
        return True
