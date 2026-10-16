"""Clip a GeoTIFF or copy whole tiles whose footprint meets a geometry.

Tiles are never cropped. Cropping a tile would break the z/x/y grid the viewer
already reads.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import transform_geom
from shapely.geometry import box, mapping
from shapely.prepared import prep

from .tile_scheme import detect_scheme, iter_tiles


def _latitude(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def _xyz_bounds(z, x, y):
    span = 1 << z
    west = x / span * 360 - 180
    east = (x + 1) / span * 360 - 180
    north = _latitude(y, z)
    south = _latitude(y + 1, z)
    return west, south, east, north


def load_geometry(spec: dict):
    shp = str(spec.get("shp_path") or "").strip()
    if shp:
        frame = gpd.read_file(shp)
        if frame.crs is None:
            raise ValueError("无法识别坐标系")
        frame = frame.to_crs(4326)
        geom = frame.geometry.union_all() if hasattr(frame.geometry, "union_all") else frame.geometry.unary_union
        if geom is None or geom.is_empty:
            raise ValueError("裁切范围为空")
        return geom
    return box(float(spec["west"]), float(spec["south"]), float(spec["east"]), float(spec["north"]))


def _marker(dest: Path) -> Path:
    return Path(str(dest) + ".task")


def clip_raster(source: Path, dest: Path, geom, task_id: str, checkpoint=None, progress=None):
    source = Path(source).resolve()
    dest = Path(dest)
    if dest.resolve() == source:
        raise ValueError("不能覆盖源文件")
    marker = _marker(dest)
    if dest.is_file() and marker.is_file():
        try:
            owner = json.loads(marker.read_text("utf-8"))
        except json.JSONDecodeError:
            owner = {}
        if owner.get("task_id") == task_id and owner.get("source") == str(source):
            with rasterio.open(dest) as existing:
                if existing.width and existing.height:
                    if progress:
                        progress(1, 1, "裁切结果已存在，跳过")
                    return {"skipped": True, "path": str(dest.resolve()), "kind": "raster"}
    if checkpoint:
        checkpoint()
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    try:
        with rasterio.open(source) as src:
            if src.crs is None:
                raise ValueError("无法识别坐标系")
            projected = transform_geom("EPSG:4326", src.crs, mapping(geom))
            try:
                image, transform = rio_mask(src, [projected], crop=True)
            except ValueError as exc:
                if "overlap" in str(exc).lower() or "not overlap" in str(exc).lower() or "Input shapes do not overlap" in str(exc):
                    raise ValueError("裁切范围与栅格不相交") from exc
                raise
            if image.size == 0:
                raise ValueError("裁切范围与栅格不相交")
            profile = src.profile.copy()
            profile.update(height=image.shape[1], width=image.shape[2], transform=transform)
            for key in ("blockxsize", "blockysize", "tiled"):
                profile.pop(key, None)
            with rasterio.open(partial, "w", **profile) as out:
                out.write(image)
        partial.replace(dest)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    marker.write_text(json.dumps({"task_id": task_id, "source": str(source)}, ensure_ascii=False), "utf-8")
    if progress:
        progress(1, 1, "栅格已裁切")
    return {"skipped": False, "path": str(dest.resolve()), "kind": "raster"}


def clip_tiles(source: Path, output: Path, geom, checkpoint=None, progress=None):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if output == source or source in output.parents:
        raise ValueError("不能把裁切结果写进源目录")
    scheme = detect_scheme(source)
    prepared = prep(geom)
    tiles = list(iter_tiles(source))
    if not tiles:
        raise ValueError("没有识别到 z/x/y 瓦片")
    output.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    total = len(tiles)
    matched = 0
    for index, (z, x, file_y, prefix, path) in enumerate(tiles, 1):
        if checkpoint:
            checkpoint()
        xyz_y = (1 << z) - 1 - file_y if scheme == "tms" else file_y
        west, south, east, north = _xyz_bounds(z, x, xyz_y)
        if not prepared.intersects(box(west, south, east, north)):
            if progress and (index == total or index % 100 == 0):
                progress(index, total, f"正在判断瓦片 {index}/{total}")
            continue
        matched += 1
        dest = output / prefix / str(z) / str(x) / path.name
        if output not in dest.resolve().parents:
            raise ValueError("瓦片路径越界")
        size = path.stat().st_size
        if dest.is_file() and dest.stat().st_size == size:
            skipped += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_name(dest.name + ".part")
            shutil.copyfile(path, partial)
            partial.replace(dest)
            written += 1
        if progress and (index == total or index % 50 == 0):
            progress(index, total, f"正在复制瓦片 {matched}")
    if not matched:
        raise ValueError("裁切范围与瓦片不相交")
    (output / ".tile-scheme").write_text(scheme + "\n", "utf-8")
    return {
        "skipped": skipped, "written": written, "tiles": matched,
        "path": str(output), "kind": "tiles", "scheme": scheme,
    }
