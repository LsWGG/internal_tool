"""Convert a single directory among GeoJSON, KML, GPKG, DXF and SHP.

Nothing here writes a preview sidecar. The SHP viewer keeps its own cache.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pyogrio

DRIVERS = {
    "geojson": "GeoJSON",
    "kml": "KML",
    "gpkg": "GPKG",
    "dxf": "DXF",
    "shp": "ESRI Shapefile",
}
TARGET_SUFFIX = {"geojson": ".geojson", "kml": ".kml", "gpkg": ".gpkg", "dxf": ".dxf", "shp": ".shp"}
SOURCE_SUFFIX = {".geojson": "geojson", ".json": "geojson", ".kml": "kml", ".gpkg": "gpkg", ".dxf": "dxf", ".shp": "shp"}
SHP_SIDECARS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix")


def list_vectors(directory: Path):
    found = [path for path in sorted(Path(directory).iterdir()) if path.is_file() and path.suffix.lower() in SOURCE_SUFFIX]
    if not found:
        raise ValueError("目录里没有可转换的矢量文件")
    return found


def _safe_stem(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(name)).strip("._")
    return (cleaned or "layer")[:80]


def _layers(path: Path):
    if path.suffix.lower() == ".gpkg":
        return [str(row[0]) for row in pyogrio.list_layers(path)]
    return [path.stem]


def _read(path: Path, layer: str):
    frame = gpd.read_file(path, layer=layer) if path.suffix.lower() == ".gpkg" else gpd.read_file(path)
    if frame.crs is None:
        raise ValueError("无法识别坐标系")
    return frame


def _count(path: Path, layer: str | None):
    info = pyogrio.read_info(path, layer=layer) if layer else pyogrio.read_info(path)
    if "features" in info:
        return int(info["features"])
    return int(info["feature_count"])


def _ready(path: Path, layer: str | None, expected: int, source_mtime: float) -> bool:
    if not path.is_file() or path.stat().st_mtime < source_mtime:
        return False
    try:
        return _count(path, layer) == expected
    except Exception:
        return False


def _claim(path: Path, task_id: str):
    Path(str(path) + ".taskid").write_text(task_id, "utf-8")


def load_manifest(manifest: Path):
    if not manifest.is_file():
        return {"inputs": [], "outputs": []}
    try:
        data = json.loads(manifest.read_text("utf-8"))
    except json.JSONDecodeError:
        return {"inputs": [], "outputs": []}
    if isinstance(data, list):
        return {"inputs": [], "outputs": [str(item) for item in data]}
    if not isinstance(data, dict):
        return {"inputs": [], "outputs": []}
    return {
        "inputs": [str(item) for item in data.get("inputs") or []],
        "outputs": [str(item) for item in data.get("outputs") or []],
    }


def _write_single(frame, dest: Path, target: str):
    if target == "kml":
        frame = frame.to_crs(4326)
    if target == "dxf":
        # The DXF driver rejects attribute fields. Geometry is written; attributes are not.
        frame = gpd.GeoDataFrame(geometry=frame.geometry, crs=frame.crs)
    kwargs = {"driver": DRIVERS[target]}
    if target == "shp":
        kwargs["encoding"] = "utf-8"
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame.to_file(dest, **kwargs)


def remove_output(path: Path, protected: set[Path]) -> None:
    resolved = path.resolve()
    if resolved in protected:
        return
    if path.suffix.lower() == ".shp":
        for suffix in SHP_SIDECARS:
            candidate = path.with_suffix(suffix)
            if candidate.is_file() and candidate.resolve() not in protected:
                candidate.unlink()
        xml = Path(str(path) + ".xml")
        if xml.is_file() and xml.resolve() not in protected:
            xml.unlink()
    elif path.suffix.lower() == ".gpkg":
        if path.is_file():
            path.unlink()
        for extra in ("-wal", "-shm"):
            sidecar = Path(str(path) + extra)
            if sidecar.is_file() and sidecar.resolve() not in protected:
                sidecar.unlink()
    elif path.is_file():
        path.unlink()
    Path(str(path) + ".taskid").unlink(missing_ok=True)


def _remember(manifest: Path, path: Path):
    data = load_manifest(manifest)
    text = str(path)
    if text not in data["outputs"]:
        data["outputs"].append(text)
        manifest.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def convert_directory(source_dir, output_dir, target, task_id, manifest: Path, checkpoint=None, progress=None):
    source_dir = Path(source_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if target not in DRIVERS:
        raise ValueError("目标格式无效")
    files = list_vectors(source_dir)
    protected = {path.resolve() for path in files}
    for path in files:
        if path.suffix.lower() == ".shp":
            for suffix in SHP_SIDECARS:
                protected.add(path.with_suffix(suffix).resolve())
            protected.add(Path(str(path) + ".xml").resolve())
    manifest.parent.mkdir(parents=True, exist_ok=True)
    current = load_manifest(manifest)
    current["inputs"] = sorted(str(path) for path in protected)
    manifest.write_text(json.dumps(current, ensure_ascii=False), "utf-8")
    discovered = [(path, layer) for path in files for layer in _layers(path)]
    if not discovered:
        raise ValueError("目录里没有可转换的矢量文件")
    used = set()

    def unique(name):
        base = _safe_stem(name)
        candidate, number = base, 2
        while candidate.lower() in used:
            candidate = f"{base}_{number}"[:80]
            number += 1
        used.add(candidate.lower())
        return candidate

    output_dir.mkdir(parents=True, exist_ok=True)
    if target == "gpkg":
        dest = output_dir / f"{_safe_stem(source_dir.name)}.gpkg"
        if dest.resolve() in protected:
            raise ValueError("不能覆盖源文件")
        return _convert_gpkg(discovered, dest, task_id, manifest, checkpoint, progress, output_dir)
    planned = []
    for path, layer in discovered:
        dest = output_dir / f"{unique(layer)}{TARGET_SUFFIX[target]}"
        if dest.resolve() in protected:
            raise ValueError("不能覆盖源文件")
        planned.append((path, layer, dest))
    written = skipped = 0
    outputs = []
    total = len(planned)
    for index, (path, layer, dest) in enumerate(planned, 1):
        if checkpoint:
            checkpoint()
        frame = _read(path, layer)
        if _ready(dest, None, len(frame), path.stat().st_mtime):
            skipped += 1
        else:
            if dest.exists() and dest.resolve() in protected:
                raise ValueError("不能覆盖源文件")
            if dest.is_file() and dest.resolve() not in protected:
                remove_output(dest, protected)
            _write_single(frame, dest, target)
            written += 1
        _claim(dest, task_id)
        _remember(manifest, dest)
        outputs.append(str(dest))
        if progress:
            progress(index, total, f"正在转换 {index}/{total}")
    result = {"outputs": outputs, "written": written, "skipped": skipped, "path": str(output_dir), "layers": total}
    if target == "shp" and outputs:
        result["shapefile_dir"] = str(output_dir)
    return result


def _convert_gpkg(discovered, dest: Path, task_id: str, manifest: Path, checkpoint, progress, output_dir: Path):
    loaded = []
    total = len(discovered)
    used = set()
    for index, (path, layer) in enumerate(discovered, 1):
        if checkpoint:
            checkpoint()
        frame = _read(path, layer)
        base = _safe_stem(layer)
        name, number = base, 2
        while name.lower() in used:
            name = f"{base}_{number}"[:80]
            number += 1
        used.add(name.lower())
        loaded.append((name, frame, path.stat().st_mtime))
        if progress:
            progress(index, total, f"正在读取 {index}/{total}")
    rewrite = False
    ready = set()
    if dest.exists():
        try:
            existing = {str(row[0]) for row in pyogrio.list_layers(dest)}
        except Exception:
            rewrite = True
            existing = set()
        if not rewrite:
            for name, frame, mtime in loaded:
                if name not in existing:
                    continue
                if _ready(dest, name, len(frame), mtime):
                    ready.add(name)
                else:
                    rewrite = True
                    break
    if rewrite and dest.exists():
        remove_output(dest, set())
        ready = set()
    written = skipped = 0
    for name, frame, _mtime in loaded:
        if checkpoint:
            checkpoint()
        if name in ready:
            skipped += 1
            continue
        mode = "a" if dest.exists() else "w"
        frame.to_file(dest, layer=name, driver="GPKG", mode=mode)
        written += 1
    _claim(dest, task_id)
    _remember(manifest, dest)
    return {
        "outputs": [str(dest)], "written": written, "skipped": skipped,
        "path": str(output_dir), "layers": len(loaded),
    }
