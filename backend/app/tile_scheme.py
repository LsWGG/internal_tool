"""Rewrite a z/x/y tree between XYZ, TMS and WMTS.

XYZ and WMTS (GoogleMapsCompatible) both count Y from the top. Only TMS counts
from the bottom, so only a conversion that involves TMS moves files. The tree
stays ``{z}/{x}/{y}.ext``; nothing is re-downloaded.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
SCHEMES = ("xyz", "tms", "wmts")
JOURNAL_NAME = ".scheme-journal.json"
STAGING_NAME = ".scheme-staging"
MERCATOR = 20037508.342789244
INITIAL_RESOLUTION = 156543.03392804097


def detect_scheme(root: Path) -> str:
    root = Path(root)
    marker = root / ".tile-scheme"
    if marker.is_file():
        text = marker.read_text("utf-8").strip().lower()
        if text in SCHEMES:
            return text
    if (root / "tilemapresource.xml").is_file():
        return "tms"
    if (root / "WMTSCapabilities.xml").is_file():
        return "wmts"
    return "xyz"


def needs_flip(source: str, target: str) -> bool:
    return (source == "tms") != (target == "tms")


def _xml(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def iter_tiles(root: Path):
    root = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [item for item in dirnames if not item.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            parts = path.relative_to(root).parts
            if len(parts) < 3:
                continue
            try:
                z, x, y = int(parts[-3]), int(parts[-2]), int(Path(parts[-1]).stem)
            except ValueError:
                continue
            limit = 1 << z if 0 <= z <= 24 else 0
            if not limit or not (0 <= x < limit and 0 <= y < limit):
                continue
            prefix = Path(*parts[:-3]) if len(parts) > 3 else Path()
            yield z, x, y, prefix, path


def _move(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dest)
    except OSError:
        shutil.copy2(src, dest)
        src.unlink()


def _write_journal(root: Path, source: str, target: str, phase: str):
    (root / JOURNAL_NAME).write_text(
        json.dumps({"source": source, "target": target, "phase": phase}, ensure_ascii=False),
        "utf-8",
    )


def _read_journal(root: Path):
    path = root / JOURNAL_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("方案转换记录损坏，请删除 .scheme-journal.json 后重试") from exc
    if not isinstance(data, dict):
        raise ValueError("方案转换记录损坏，请删除 .scheme-journal.json 后重试")
    return data


def _stage(root: Path, checkpoint, progress):
    staging = root / STAGING_NAME
    staging.mkdir(exist_ok=True)
    tiles = list(iter_tiles(root))
    total = len(tiles)
    for index, (z, x, y, prefix, path) in enumerate(tiles, 1):
        if checkpoint:
            checkpoint()
        dest = staging / prefix / str(z) / str(x) / path.name
        if not dest.exists():
            _move(path, dest)
        if progress and (index == total or index % 100 == 0):
            progress(index, total, f"正在暂存瓦片 {index}/{total}")


def _place(root: Path, checkpoint, progress):
    staging = root / STAGING_NAME
    tiles = list(iter_tiles(staging))
    total = len(tiles)
    for index, (z, x, y, prefix, path) in enumerate(tiles, 1):
        if checkpoint:
            checkpoint()
        flipped = (1 << z) - 1 - y
        dest = root / prefix / str(z) / str(x) / f"{flipped}{path.suffix.lower()}"
        if dest.is_file() and dest.stat().st_size == path.stat().st_size:
            path.unlink()
        else:
            if dest.exists():
                dest.unlink()
            _move(path, dest)
        if progress and (index == total or index % 100 == 0):
            progress(index, total, f"正在按 TMS 行列落盘 {index}/{total}")


def _remove_staging(root: Path):
    staging = root / STAGING_NAME
    if staging.is_dir() and staging.name == STAGING_NAME and staging.parent == root:
        shutil.rmtree(staging)


def _prune_empty(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root or path.name.startswith("."):
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def _extension(tiles) -> str:
    suffixes = {path.suffix.lower().lstrip(".") for *_, path in tiles}
    if len(suffixes) != 1:
        raise ValueError("一套瓦片只能包含同一种图片格式")
    value = suffixes.pop()
    return "jpg" if value == "jpeg" else value


def _tile_pixels(root: Path):
    for tile in iter_tiles(root):
        try:
            from PIL import Image
            with Image.open(tile[-1]) as image:
                return image.size
        except Exception:
            break
    return 256, 256


def _write_metadata(root: Path, target: str, name: str):
    tiles = list(iter_tiles(root))
    extension = _extension(tiles) if tiles else "png"
    zooms = sorted({tile[0] for tile in tiles})
    width, height = _tile_pixels(root)
    if target == "tms":
        _write_tms(root, name, extension, zooms, width, height)
        (root / "WMTSCapabilities.xml").unlink(missing_ok=True)
    elif target == "wmts":
        _write_wmts(root, name, extension, zooms, width, height)
        (root / "tilemapresource.xml").unlink(missing_ok=True)
    else:
        (root / "tilemapresource.xml").unlink(missing_ok=True)
        (root / "WMTSCapabilities.xml").unlink(missing_ok=True)
    (root / ".tile-scheme").write_text(target + "\n", "utf-8")


def _write_tms(root: Path, name: str, extension: str, zooms, width: int = 256, height: int = 256):
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(extension, "image/png")
    sets = "\n".join(
        f'    <TileSet href="{zoom}" units-per-pixel="{INITIAL_RESOLUTION / (1 << zoom):.12f}" order="{zoom}"/>'
        for zoom in zooms
    )
    text = f"""<?xml version="1.0" encoding="utf-8"?>
<TileMap version="1.0.0" tilemapservice="http://tms.osgeo.org/1.0.0">
  <Title>{_xml(name)}</Title>
  <SRS>EPSG:3857</SRS>
  <BoundingBox minx="-{MERCATOR}" miny="-{MERCATOR}" maxx="{MERCATOR}" maxy="{MERCATOR}"/>
  <Origin x="-{MERCATOR}" y="-{MERCATOR}"/>
  <TileFormat width="{width}" height="{height}" mime-type="{mime}" extension="{extension}"/>
  <TileSets profile="global-mercator">
{sets}
  </TileSets>
</TileMap>
"""
    (root / "tilemapresource.xml").write_text(text, "utf-8")


def _write_wmts(root: Path, name: str, extension: str, zooms, width: int = 256, height: int = 256):
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(extension, "image/png")
    matrices = "\n".join(
        f"""    <TileMatrix>
      <Identifier>{zoom}</Identifier>
      <TopLeftCorner>-{MERCATOR} {MERCATOR}</TopLeftCorner>
    </TileMatrix>"""
        for zoom in zooms
    )
    text = f"""<?xml version="1.0" encoding="UTF-8"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0" version="1.0.0">
  <Contents>
    <Layer>
      <Identifier>{_xml(name)}</Identifier>
      <Format>{mime}</Format>
      <TileMatrixSetLink><TileMatrixSet>GoogleMapsCompatible</TileMatrixSet></TileMatrixSetLink>
    </Layer>
    <TileMatrixSet>
      <Identifier>GoogleMapsCompatible</Identifier>
      <SupportedCRS>EPSG:3857</SupportedCRS>
      <TileWidth>{width}</TileWidth>
      <TileHeight>{height}</TileHeight>
{matrices}
    </TileMatrixSet>
  </Contents>
</Capabilities>
"""
    (root / "WMTSCapabilities.xml").write_text(text, "utf-8")


def _copy_tiles(root: Path, output: Path, flip: bool, checkpoint, progress):
    tiles = list(iter_tiles(root))
    if not tiles:
        raise ValueError("没有识别到 z/x/y 瓦片")
    written = skipped = 0
    total = len(tiles)
    for index, (z, x, y, prefix, path) in enumerate(tiles, 1):
        if checkpoint:
            checkpoint()
        file_y = (1 << z) - 1 - y if flip else y
        dest = output / prefix / str(z) / str(x) / f"{file_y}{path.suffix.lower()}"
        if output.resolve() not in dest.resolve().parents:
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
        if progress and (index == total or index % 100 == 0):
            progress(index, total, f"正在写出瓦片 {index}/{total}")
    return {"tiles": total, "written": written, "skipped": skipped, "flipped": flip}


def convert_scheme(root, target, output_dir=None, *, in_place, checkpoint=None, progress=None):
    root = Path(root).resolve()
    if target not in SCHEMES:
        raise ValueError("目标方案无效")
    if in_place:
        if output_dir:
            raise ValueError("原目录转换时不需要另填输出路径")
        return _convert_in_place(root, target, checkpoint, progress)
    if not output_dir:
        raise ValueError("请填写输出目录")
    output = Path(output_dir).resolve()
    if output == root or root in output.parents or output in root.parents:
        raise ValueError("输出目录不能位于源目录之中")
    output.mkdir(parents=True, exist_ok=True)
    source = detect_scheme(root)
    result = _copy_tiles(root, output, needs_flip(source, target), checkpoint, progress)
    _write_metadata(output, target, root.name)
    result.update(scheme=target, path=str(output))
    return result


def _convert_in_place(root: Path, target: str, checkpoint, progress):
    journal = _read_journal(root)
    if journal:
        pending = str(journal.get("target") or "")
        if pending != target:
            raise ValueError(f"已有未完成的方案转换（目标 {pending}），请先完成该转换")
        source = str(journal.get("source") or "xyz")
        phase = str(journal.get("phase") or "staging")
    else:
        source = detect_scheme(root)
        if not list(iter_tiles(root)):
            raise ValueError("没有识别到 z/x/y 瓦片")
        if not needs_flip(source, target):
            _write_metadata(root, target, root.name)
            count = len(list(iter_tiles(root)))
            return {"tiles": count, "written": 0, "skipped": count, "flipped": False, "scheme": target, "path": str(root)}
        _write_journal(root, source, target, "staging")
        phase = "staging"
    if phase != "placing":
        _stage(root, checkpoint, progress)
        _write_journal(root, source, target, "placing")
    _place(root, checkpoint, progress)
    _write_metadata(root, target, root.name)
    (root / JOURNAL_NAME).unlink(missing_ok=True)
    _remove_staging(root)
    _prune_empty(root)
    count = len(list(iter_tiles(root)))
    return {"tiles": count, "written": count, "skipped": 0, "flipped": True, "scheme": target, "path": str(root)}
