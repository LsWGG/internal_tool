"""Vector conversion jobs. In-place delete removes only files this task still owns."""
from __future__ import annotations

import shutil
from pathlib import Path

from .directory_upload import claimed_upload, remove_upload
from .local_path import resolve_directory
from .path_jobs import PathJobManager, remove_owned_directory
from .vector_convert import DRIVERS, convert_directory, list_vectors, load_manifest, remove_output


class VectorConvertManager(PathJobManager):
    def validate(self, payload: dict) -> dict:
        target = str(payload.get("target_format") or "").strip().lower()
        if target not in DRIVERS:
            raise ValueError("目标格式无效")
        in_place = bool(payload.get("in_place"))
        output_raw = str(payload.get("output_path") or "").strip()
        if in_place and output_raw:
            raise ValueError("原目录转换时不需要另填输出路径")
        source = resolve_directory(str(payload.get("source_path") or ""), writable=in_place)
        list_vectors(source)
        return {
            "source_path": str(source), "output_path": output_raw,
            "target_format": target, "in_place": in_place,
            "upload_dir": claimed_upload(self.data_dir, str(payload.get("upload_dir") or "")),
        }

    def assign_output(self, task_id: str, spec: dict) -> dict:
        source = Path(spec["source_path"])
        if spec["in_place"]:
            spec["output_path"] = str(source)
            return spec
        raw = str(spec.get("output_path") or "").strip()
        root = resolve_directory(raw, writable=True) if raw else self.data_dir
        spec["output_path"] = str(root / task_id)
        return spec

    def _manifest(self, task_id: str) -> Path:
        return self.data_dir / f"{task_id}.manifest.json"

    def execute(self, task_id: str, spec: dict, checkpoint, progress) -> dict:
        output = Path(spec["output_path"])
        result = convert_directory(
            spec["source_path"], output, spec["target_format"], task_id, self._manifest(task_id),
            checkpoint, progress,
        )
        if not spec["in_place"] and output.is_dir() and output.name == task_id and output.parent.resolve() == self.data_dir.resolve():
            archive = shutil.make_archive(str(self.data_dir / task_id), "zip", output)
            result["archive"] = Path(archive).name
            result["archive_path"] = str(Path(archive).resolve())
        result["in_place"] = bool(spec["in_place"])
        return result

    def discard(self, task_id: str, record: dict) -> None:
        spec = record.get("request") or {}
        output = Path(spec.get("output_path") or record.get("output_path") or "")
        manifest = self._manifest(task_id)
        if not spec.get("in_place"):
            remove_owned_directory(output, task_id)
        else:
            source = Path(spec.get("source_path") or "").resolve()
            recorded = load_manifest(manifest)
            protected = {Path(item).resolve() for item in recorded["inputs"]}
            for raw in recorded["outputs"]:
                path = Path(raw)
                marker = Path(str(path) + ".taskid")
                if not marker.is_file() or marker.read_text("utf-8").strip() != task_id:
                    continue
                resolved = path.resolve()
                if resolved in protected or resolved == source:
                    continue
                if not source.is_dir() or resolved.parent != source:
                    continue
                remove_output(path, protected)
        manifest.unlink(missing_ok=True)
        remove_upload(self.data_dir, spec.get("upload_dir") or "")
