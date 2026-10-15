"""Pack and unpack MBTiles 1.3 without copying a preview cache beside the archive.

Tile rows in the sqlite file are TMS: ``tile_row = (1 << z) - 1 - y``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
FORMAT_BY_SUFFIX = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".webp": "webp"}


def tms_row(z: int, y: int) -> int:
    return (1 << z) - 1 - y


def safe_mbtiles_name(name: str) -> str:
    cleaned = [char for char in (name or "").strip() if char.isalnum() or char in "-_."]
    text = "".join(cleaned).strip(".-_")
    if text.lower().endswith(".mbtiles"):
        text = text[: -len(".mbtiles")].strip(".-_")
    return f"{(text or 'tiles')[:80]}.mbtiles"


def iter_xyz(root: Path):
    root = root.resolve()
    seen = set()
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
            key = (z, x, y)
            if key in seen:
                raise ValueError("目录里有重复的 z/x/y 瓦片")
            seen.add(key)
            yield z, x, y, path


def mbtiles_task_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT value FROM metadata WHERE name='task_id'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return str(row[0]) if row and row[0] else None


def _bounds_text(tiles, bounds):
    if bounds is not None:
        west, south, east, north = bounds
        return f"{west},{south},{east},{north}"
    max_zoom = max(tile[0] for tile in tiles)
    xs = [tile[1] for tile in tiles if tile[0] == max_zoom]
    ys = [tile[2] for tile in tiles if tile[0] == max_zoom]
    span = 1 << max_zoom
    west = min(xs) / span * 360 - 180
    east = (max(xs) + 1) / span * 360 - 180
    north = _latitude(min(ys), max_zoom)
    south = _latitude(max(ys) + 1, max_zoom)
    return f"{west},{south},{east},{north}"


def _latitude(y, z):
    import math
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def pack_directory(root, dest, *, name, bounds=None, task_id="", checkpoint=None, progress=None):
    root = Path(root).resolve()
    dest = Path(dest)
    if dest.suffix.lower() != ".mbtiles" or dest.name != Path(dest.name).name:
        raise ValueError("MBTiles 目标必须是 .mbtiles 文件")
    tiles = list(iter_xyz(root))
    if not tiles:
        raise ValueError("没有识别到 z/x/y 瓦片")
    formats = {FORMAT_BY_SUFFIX[path.suffix.lower()] for *_, path in tiles}
    if len(formats) != 1:
        raise ValueError("一套 MBTiles 只能包含同一种图片格式")
    image_format = formats.pop()
    if dest.exists():
        owner = mbtiles_task_id(dest)
        if not task_id or owner != task_id:
            raise ValueError("目标 MBTiles 已存在")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(tiles[0][3]) as image:
        width, height = image.size
    zooms = [tile[0] for tile in tiles]
    connection = sqlite3.connect(dest)
    written = skipped = 0
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)"
        )
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row)")
        connection.execute("DELETE FROM metadata WHERE name='task_id'")
        connection.execute("INSERT INTO metadata (name, value) VALUES ('task_id', ?)", (task_id or "",))
        connection.commit()
        total = len(tiles)
        for index, (z, x, y, path) in enumerate(tiles, 1):
            if checkpoint:
                checkpoint()
            before = connection.total_changes
            connection.execute(
                "INSERT OR IGNORE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                (z, x, tms_row(z, y), path.read_bytes()),
            )
            if connection.total_changes > before:
                written += 1
            else:
                skipped += 1
            if index == total or index % 100 == 0:
                connection.commit()
                if progress:
                    progress(index, total, f"正在打包 MBTiles {index}/{total}")
        metadata = {
            "name": name or dest.stem,
            "format": image_format,
            "minzoom": str(min(zooms)),
            "maxzoom": str(max(zooms)),
            "bounds": _bounds_text(tiles, bounds),
            "type": "overlay",
            "version": "1.3",
            "description": name or dest.stem,
            "tile_width": str(width),
            "tile_height": str(height),
            "task_id": task_id or "",
        }
        connection.execute("DELETE FROM metadata")
        connection.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", metadata.items())
        connection.commit()
    finally:
        connection.commit()
        connection.close()
    return {
        "tiles": len(tiles), "skipped": skipped, "written": written,
        "path": str(dest.resolve()), "file": dest.name,
        "minzoom": min(zooms), "maxzoom": max(zooms), "format": image_format,
    }


def _image_format(metadata, blob):
    value = str(metadata.get("format") or "").lower()
    if value == "jpeg":
        value = "jpg"
    if value in ("png", "jpg", "webp"):
        return value
    if blob.startswith(b"\x89PNG"):
        return "png"
    if blob.startswith(b"\xff\xd8"):
        return "jpg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    raise ValueError("不支持的 MBTiles 图片格式")


def unpack_mbtiles(src, output_dir, *, checkpoint=None, progress=None):
    src = Path(src).resolve()
    output = Path(output_dir).resolve()
    if output == src or src in output.parents:
        raise ValueError("不能把 MBTiles 拆到它自己里面")
    try:
        connection = sqlite3.connect(f"{src.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError("无法读取 MBTiles") from exc
    try:
        metadata = dict(connection.execute("SELECT name, value FROM metadata").fetchall())
        rows = connection.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles").fetchall()
    except sqlite3.Error as exc:
        raise ValueError("无法读取 MBTiles") from exc
    finally:
        connection.close()
    if not rows:
        raise ValueError("MBTiles 里没有瓦片")
    image_format = _image_format(metadata, rows[0][3] or b"")
    extension = f".{image_format}"
    output.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    total = len(rows)
    for index, (z, x, row, blob) in enumerate(rows, 1):
        if checkpoint:
            checkpoint()
        try:
            z, x, row = int(z), int(x), int(row)
        except (TypeError, ValueError):
            continue
        limit = 1 << z if 0 <= z <= 24 else 0
        y = (1 << z) - 1 - row
        if not limit or not (0 <= x < limit and 0 <= y < limit) or not isinstance(blob, (bytes, bytearray)):
            continue
        dest = (output / str(z) / str(x) / f"{y}{extension}").resolve()
        if output not in dest.parents:
            raise ValueError("瓦片路径越界")
        if dest.is_file() and dest.stat().st_size == len(blob):
            skipped += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_name(dest.name + ".part")
            partial.write_bytes(blob)
            partial.replace(dest)
            written += 1
        if progress and (index == total or index % 100 == 0):
            progress(index, total, f"正在拆包 {index}/{total}")
    return {
        "tiles": total, "skipped": skipped, "written": written,
        "path": str(output), "format": image_format,
    }
