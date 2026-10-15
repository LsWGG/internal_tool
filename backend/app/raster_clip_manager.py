"""Clip jobs. A tile directory is never modified in place."""
from __future__ import annotations

import math
import shutil
from pathlib import Path

from .directory_upload import claimed_upload, remove_upload
from .local_path import resolve_directory, resolve_file, resolve_path
from .path_jobs import PathJobManager, remove_owned_directory
from .raster_clip import _marker, clip_raster, clip_tiles, load_geometry

_RASTER_SUFFIXES = {".tif", ".tiff"}


class RasterClipManager(PathJobManager):
    def validate(self, payload: dict) -> dict:
        in_place = bool(payload.get("in_place"))
        output_raw = str(payload.get("output_path") or "").strip()
        if in_place and output_raw:
            raise ValueError("原文件裁切时不需要另填输出路径")
        source = resolve_path(str(payload.get("source_path") or ""), writable=in_place)
        if source.is_dir():
            kind = "tiles"
            if in_place:
                raise ValueError("瓦片目录不能在原位置裁切")
        elif source.suffix.lower() in _RASTER_SUFFIXES:
            kind = "raster"
        else:
            raise ValueError("只支持 GeoTIFF 或 z/x/y 瓦片目录")
        shp = str(payload.get("shp_path") or "").strip()
        spec = {
            "source_path": str(source), "source_kind": kind, "in_place": in_place,
            "output_path": output_raw, "shp_path": "",
            "west": None, "south": None, "east": None, "north": None,
            "upload_dir": claimed_upload(self.data_dir, str(payload.get("upload_dir") or "")),
            "shp_upload_dir": claimed_upload(self.data_dir, str(payload.get("shp_upload_dir") or "")),
        }
        if shp:
            spec["shp_path"] = str(resolve_file(shp, suffixes=(".shp",)))
            return spec
        try:
            values = [float(payload[key]) for key in ("west", "south", "east", "north")]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("请提供 Shapefile 或框选范围") from exc
        if not all(math.isfinite(value) for value in values) or values[0] >= values[2] or values[1] >= values[3]:
            raise ValueError("范围坐标无效")
        spec["west"], spec["south"], spec["east"], spec["north"] = values
        return spec

    def assign_output(self, task_id: str, spec: dict) -> dict:
        source = Path(spec["source_path"])
        if spec["in_place"]:
            dest = source.with_name(f"{source.stem}_clip{source.suffix}")
            if dest.resolve() == source.resolve():
                raise ValueError("不能覆盖源文件")
            spec["output_path"] = str(dest)
            return spec
        raw = str(spec.get("output_path") or "").strip()
        root = resolve_directory(raw, writable=True) if raw else self.data_dir
        output = root / task_id
        if output.resolve() == source.resolve() or source.resolve() in output.resolve().parents:
            raise ValueError("不能把裁切结果写进源文件")
        spec["output_path"] = str(output)
        return spec

    def execute(self, task_id: str, spec: dict, checkpoint, progress) -> dict:
        geom = load_geometry(spec)
        source = Path(spec["source_path"])
        output = Path(spec["output_path"])
        if spec["source_kind"] == "raster":
            dest = output if spec["in_place"] else output / source.name
            if not spec["in_place"]:
                output.mkdir(parents=True, exist_ok=True)
            result = clip_raster(source, dest, geom, task_id, checkpoint, progress)
        else:
            result = clip_tiles(source, output, geom, checkpoint, progress)
        self._zip_owned(task_id, output if not spec["in_place"] else None, result)
        result["in_place"] = bool(spec["in_place"])
        return result

    def _zip_owned(self, task_id: str, output, result: dict):
        if output is None or not output.is_dir():
            return
        if output.name != task_id or output.parent.resolve() != self.data_dir.resolve():
            return
        archive = shutil.make_archive(str(self.data_dir / task_id), "zip", output)
        result["archive"] = Path(archive).name
        result["archive_path"] = str(Path(archive).resolve())

    def discard(self, task_id: str, record: dict) -> None:
        spec = record.get("request") or {}
        output = Path(spec.get("output_path") or record.get("output_path") or "")
        if spec.get("in_place"):
            self._discard_inplace(task_id, spec, output)
        else:
            remove_owned_directory(output, task_id)
        remove_upload(self.data_dir, spec.get("upload_dir") or "")
        remove_upload(self.data_dir, spec.get("shp_upload_dir") or "")

    def _discard_inplace(self, task_id: str, spec: dict, dest: Path):
        source = Path(spec.get("source_path") or "")
        marker = _marker(dest)
        if not marker.is_file():
            return
        try:
            owner = json_owner(marker)
        except ValueError:
            return
        if owner.get("task_id") != task_id:
            return
        expected = source.with_name(f"{source.stem}_clip{source.suffix}")
        if dest.resolve() == source.resolve() or dest.resolve() != expected.resolve():
            return
        if not dest.name.endswith(f"_clip{source.suffix}") or dest.parent.resolve() != source.parent.resolve():
            return
        dest.unlink(missing_ok=True)
        dest.with_name(dest.name + ".part").unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def json_owner(path: Path) -> dict:
    import json
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("owner") from exc
    if not isinstance(data, dict):
        raise ValueError("owner")
    return data
