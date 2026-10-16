"""Pack, unpack and scheme jobs for the tile viewer.

An in-place scheme conversion is not undone on delete: the journal has to stay
so a half-finished Y flip can resume.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .local_path import resolve_directory, resolve_file
from .mbtiles import mbtiles_task_id, pack_directory, safe_mbtiles_name, unpack_mbtiles
from .path_jobs import PathJobManager, remove_owned_directory, remove_owned_file
from .tile_scheme import SCHEMES, convert_scheme


class TileJobManager(PathJobManager):
    def __init__(self, data_dir: Path, on_directory_changed=None):
        super().__init__(data_dir, state_name="jobs.json")
        self.on_directory_changed = on_directory_changed

    def validate(self, payload: dict) -> dict:
        kind = str(payload.get("kind") or "").strip().lower()
        if kind not in ("pack", "unpack", "scheme"):
            raise ValueError("任务类型无效")
        in_place = bool(payload.get("in_place"))
        output_raw = str(payload.get("output_path") or "").strip()
        if kind == "unpack":
            source = resolve_file(str(payload.get("source") or ""), suffixes=(".mbtiles",))
            if in_place:
                raise ValueError("拆包需要写到目录，不能在原文件上拆")
            target = ""
        elif kind == "pack":
            source = resolve_directory(str(payload.get("source") or ""), writable=in_place)
            if in_place and output_raw:
                raise ValueError("原目录打包时不需要另填输出路径")
            target = ""
        else:
            source = resolve_directory(str(payload.get("source") or ""), writable=in_place)
            target = str(payload.get("target") or "").strip().lower()
            if target not in SCHEMES:
                raise ValueError("目标方案无效")
            if in_place and output_raw:
                raise ValueError("原目录转换时不需要另填输出路径")
        return {
            "kind": kind, "source": str(source), "in_place": in_place,
            "output_path": output_raw, "name": str(payload.get("name") or "")[:120],
            "target": target,
        }

    def assign_output(self, task_id: str, spec: dict) -> dict:
        source = Path(spec["source"])
        raw = str(spec.get("output_path") or "").strip()
        if spec["kind"] == "pack" and spec["in_place"]:
            spec["output_path"] = str(source / safe_mbtiles_name(source.name))
            return spec
        if spec["kind"] == "scheme" and spec["in_place"]:
            spec["output_path"] = str(source)
            return spec
        root = resolve_directory(raw, writable=True) if raw else self.data_dir
        if spec["kind"] == "pack":
            spec["output_path"] = str(root / f"{task_id}.mbtiles")
        else:
            output = root / task_id
            if output.resolve() == source.resolve() or source.resolve() in output.resolve().parents or output.resolve() in source.resolve().parents:
                raise ValueError("输出目录不能位于源目录之中")
            spec["output_path"] = str(output)
        return spec

    def execute(self, task_id: str, spec: dict, checkpoint, progress) -> dict:
        kind = spec["kind"]
        source = Path(spec["source"])
        output = Path(spec["output_path"])
        if kind == "pack":
            result = pack_directory(
                source, output, name=spec.get("name") or source.name, task_id=task_id,
                checkpoint=checkpoint, progress=progress,
            )
        elif kind == "unpack":
            result = unpack_mbtiles(source, output, checkpoint=checkpoint, progress=progress)
            self._zip_owned(task_id, output, result)
        else:
            result = convert_scheme(
                source, spec["target"], None if spec["in_place"] else output,
                in_place=bool(spec["in_place"]), checkpoint=checkpoint, progress=progress,
            )
            if spec["in_place"] and self.on_directory_changed:
                try:
                    self.on_directory_changed(str(source))
                except Exception as exc:
                    result["refresh_error"] = str(exc)
            elif not spec["in_place"]:
                self._zip_owned(task_id, output, result)
        result["kind"] = kind
        result["in_place"] = bool(spec["in_place"])
        return result

    def _zip_owned(self, task_id: str, output: Path, result: dict):
        if not output.is_dir() or output.name != task_id or output.parent.resolve() != self.data_dir.resolve():
            return
        archive = shutil.make_archive(str(self.data_dir / task_id), "zip", output)
        result["archive"] = Path(archive).name
        result["archive_path"] = str(Path(archive).resolve())

    def discard(self, task_id: str, record: dict) -> None:
        spec = record.get("request") or {}
        output = Path(spec.get("output_path") or record.get("output_path") or "")
        kind = spec.get("kind")
        if spec.get("in_place"):
            if kind == "pack":
                self._discard_inplace_pack(task_id, spec, output)
            return
        if kind == "pack":
            remove_owned_file(output, task_id)
            return
        remove_owned_directory(output, task_id)

    @staticmethod
    def _discard_inplace_pack(task_id: str, spec: dict, path: Path):
        source = Path(spec.get("source") or "").resolve()
        if not path.is_file() or path.parent.resolve() != source or path.suffix.lower() != ".mbtiles":
            return
        owner = mbtiles_task_id(path)
        planned = source / safe_mbtiles_name(source.name)
        if owner == task_id or (not owner and path.resolve() == planned.resolve()):
            path.unlink()
