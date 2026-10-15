import json
import math
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .local_path import resolve_directory, resolve_file
from .tile_scheme import detect_scheme


TILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class TilePreviewManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "layers.json"
        self.lock = threading.RLock()
        try:
            self.layers = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.layers = {}

    def _save(self):
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.layers, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.state_file)

    @staticmethod
    def _tile_parts(relative: Path):
        parts = relative.parts
        if len(parts) < 3:
            return None
        try:
            z, x, y = int(parts[-3]), int(parts[-2]), int(Path(parts[-1]).stem)
        except ValueError:
            return None
        limit = 1 << z if 0 <= z <= 24 else 0
        return (z, x, y) if limit and 0 <= x < limit and 0 <= y < limit else None

    @staticmethod
    def _latitude(y, z):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))

    def create(self, upload_dir: Path, relative_files: list[str], name: str = ""):
        tiles = []
        common_prefixes = []
        for value in relative_files:
            relative = Path(value)
            if relative.suffix.lower() not in TILE_EXTENSIONS:
                continue
            xyz = self._tile_parts(relative)
            if xyz:
                tiles.append((relative, xyz))
                common_prefixes.append(relative.parts[:-3])
        if not tiles:
            raise ValueError("没有识别到 z/x/y.png、jpg、jpeg 或 webp 瓦片目录")
        prefixes = {relative.parts[:-3] for relative, _ in tiles}
        if len(prefixes) != 1:
            raise ValueError("一个图层目录中只能包含一套 z/x/y 瓦片结构")

        layer_id = uuid.uuid4().hex
        target = self.data_dir / layer_id
        shutil.move(str(upload_dir), target)
        zooms = [xyz[0] for _, xyz in tiles]
        min_zoom, max_zoom = min(zooms), max(zooms)
        tile_root = "/".join(next(iter(prefixes)))
        extensions = sorted({relative.suffix.lower() for relative, _ in tiles})
        sample_zoom = max_zoom
        samples = [(x, y) for _, (z, x, y) in tiles if z == sample_zoom]
        xs, ys = [item[0] for item in samples], [item[1] for item in samples]
        bounds = {
            "west": min(xs) / (1 << sample_zoom) * 360 - 180,
            "east": (max(xs) + 1) / (1 << sample_zoom) * 360 - 180,
            "north": self._latitude(min(ys), sample_zoom),
            "south": self._latitude(max(ys) + 1, sample_zoom),
        }
        display_name = str(name or "").strip()[:100]
        if not display_name:
            prefix = next((parts[-1] for parts in common_prefixes if parts), "")
            display_name = prefix or f"本地瓦片 {layer_id[:8]}"
        record = {
            "id": layer_id, "name": display_name, "tile_count": len(tiles),
            "min_zoom": min_zoom, "max_zoom": max_zoom, "bounds": bounds,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tile_root": tile_root, "extensions": extensions,
        }
        with self.lock:
            self.layers[layer_id] = record
            self._save()
        return self.public(record)

    def _scan(self, root: Path):
        tiles = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [item for item in dirnames if not item.startswith(".")]
            for filename in filenames:
                path = Path(dirpath) / filename
                if path.suffix.lower() not in TILE_EXTENSIONS:
                    continue
                relative = path.relative_to(root)
                xyz = self._tile_parts(relative)
                if xyz:
                    tiles.append((relative, xyz))
        if not tiles:
            raise ValueError("没有识别到 z/x/y.png、jpg、jpeg 或 webp 瓦片目录")
        prefixes = {relative.parts[:-3] for relative, _ in tiles}
        if len(prefixes) != 1:
            raise ValueError("一个图层目录中只能包含一套 z/x/y 瓦片结构")
        scheme = detect_scheme(root)
        zooms = [xyz[0] for _, xyz in tiles]
        min_zoom, max_zoom = min(zooms), max(zooms)
        samples = []
        for _, (z, x, y) in tiles:
            if z != max_zoom:
                continue
            xyz_y = (1 << z) - 1 - y if scheme == "tms" else y
            samples.append((x, xyz_y))
        xs, ys = [item[0] for item in samples], [item[1] for item in samples]
        return {
            "tile_count": len(tiles), "min_zoom": min_zoom, "max_zoom": max_zoom,
            "bounds": {
                "west": min(xs) / (1 << max_zoom) * 360 - 180,
                "east": (max(xs) + 1) / (1 << max_zoom) * 360 - 180,
                "north": self._latitude(min(ys), max_zoom),
                "south": self._latitude(max(ys) + 1, max_zoom),
            },
            "tile_root": "/".join(next(iter(prefixes))),
            "extensions": sorted({relative.suffix.lower() for relative, _ in tiles}),
            "scheme": scheme, "kind": "directory",
        }

    def open_directory(self, raw: str, name: str = ""):
        root = resolve_directory(raw)
        scanned = self._scan(root)
        layer_id = uuid.uuid4().hex
        display_name = str(name or "").strip()[:100] or root.name or f"外接瓦片 {layer_id[:8]}"
        record = {
            "id": layer_id, "name": display_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(root), "external": True,
            **scanned,
        }
        with self.lock:
            self.layers[layer_id] = record
            self._save()
        return self.public(record)

    def open_mbtiles(self, raw: str, name: str = ""):
        path = resolve_file(raw, suffixes=(".mbtiles",))
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ValueError("无法读取 MBTiles") from exc
        try:
            try:
                metadata = dict(connection.execute("SELECT name, value FROM metadata").fetchall())
                row = connection.execute("SELECT MIN(zoom_level), MAX(zoom_level), COUNT(*) FROM tiles").fetchone()
            except sqlite3.Error as exc:
                raise ValueError("无法读取 MBTiles") from exc
        finally:
            connection.close()
        if not row or not row[2]:
            raise ValueError("MBTiles 里没有瓦片")
        min_zoom, max_zoom, count = int(row[0]), int(row[1]), int(row[2])
        image_format = str(metadata.get("format") or "png").lower()
        if image_format == "jpeg":
            image_format = "jpg"
        if image_format not in ("png", "jpg", "webp"):
            image_format = "png"
        layer_id = uuid.uuid4().hex
        display_name = str(name or "").strip()[:100] or str(metadata.get("name") or "").strip() or path.stem
        record = {
            "id": layer_id, "name": display_name, "tile_count": count,
            "min_zoom": min_zoom, "max_zoom": max_zoom,
            "bounds": self._mbtiles_bounds(path, metadata, max_zoom),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tile_root": "", "extensions": [f".{image_format}"],
            "source_path": str(path), "external": True,
            "kind": "mbtiles", "scheme": "xyz", "mbtiles_format": image_format,
        }
        with self.lock:
            self.layers[layer_id] = record
            self._save()
        return self.public(record)

    def _mbtiles_bounds(self, path: Path, metadata: dict, max_zoom: int):
        parts = str(metadata.get("bounds") or "").split(",")
        if len(parts) == 4:
            try:
                west, south, east, north = (float(item) for item in parts)
            except ValueError:
                west = south = east = north = None
            if west is not None and west < east and south < north:
                return {"west": west, "south": south, "east": east, "north": north}
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) FROM tiles WHERE zoom_level=?",
                (max_zoom,),
            ).fetchone()
        finally:
            connection.close()
        min_x, max_x, min_row, max_row = (int(item) for item in row)
        north_y = (1 << max_zoom) - 1 - max_row
        south_y = (1 << max_zoom) - 1 - min_row
        return {
            "west": min_x / (1 << max_zoom) * 360 - 180,
            "east": (max_x + 1) / (1 << max_zoom) * 360 - 180,
            "north": self._latitude(north_y, max_zoom),
            "south": self._latitude(south_y + 1, max_zoom),
        }

    def refresh_directory(self, raw: str):
        try:
            root = resolve_directory(str(raw or ""))
        except ValueError:
            return
        with self.lock:
            matched = [
                record["id"] for record in self.layers.values()
                if record.get("kind") != "mbtiles" and record.get("source_path")
                and Path(record["source_path"]).resolve() == root
            ]
        if not matched:
            return
        try:
            scanned = self._scan(root)
        except ValueError:
            return
        with self.lock:
            for layer_id in matched:
                current = self.layers.get(layer_id)
                if current:
                    current.update(scanned)
            self._save()

    @staticmethod
    def public(record):
        return {key: value for key, value in record.items() if key not in ("tile_root", "extensions")}

    def list(self):
        with self.lock:
            return [self.public(item) for item in sorted(self.layers.values(), key=lambda item: item["created_at"], reverse=True)]

    def tile(self, layer_id: str, z: int, x: int, y: int):
        with self.lock:
            record = self.layers.get(layer_id)
            if not record:
                return None
            kind = record.get("kind")
            scheme = record.get("scheme") or "xyz"
            source = record.get("source_path")
            extensions = list(record.get("extensions") or [])
            tile_root = record.get("tile_root") or ""
            image_format = record.get("mbtiles_format") or "png"
        if kind == "mbtiles":
            return self._mbtiles_tile(layer_id, source, z, x, y, image_format)
        file_y = (1 << z) - 1 - y if scheme == "tms" else y
        root = Path(source).resolve() if source else (self.data_dir / layer_id).resolve()
        if not root.is_dir():
            return None
        for extension in extensions:
            path = (root / tile_root / str(z) / str(x) / f"{file_y}{extension}").resolve()
            if root in path.parents and path.is_file():
                return path
        return None

    def _mbtiles_tile(self, layer_id: str, source: str, z: int, x: int, y: int, image_format: str):
        if not source:
            return None
        cache_root = (self.data_dir / layer_id / "cache").resolve()
        if cache_root.parent.parent != self.data_dir.resolve() or cache_root.parent.name != layer_id:
            return None
        extension = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "webp": ".webp"}.get(image_format, ".png")
        dest = (cache_root / str(int(z)) / str(int(x)) / f"{int(y)}{extension}").resolve()
        if cache_root not in dest.parents:
            return None
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        try:
            connection = sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = (1 << int(z)) - 1 - int(y)
            found = connection.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (int(z), int(x), row),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        if not found or not isinstance(found[0], (bytes, bytearray)):
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        partial.write_bytes(found[0])
        partial.replace(dest)
        return dest

    def delete(self, layer_id: str):
        with self.lock:
            record = self.layers.pop(layer_id, None)
            if not record:
                return False
            self._save()
        cache = (self.data_dir / layer_id).resolve()
        if cache.parent == self.data_dir.resolve() and cache.name == layer_id and cache.exists():
            shutil.rmtree(cache)
        return True
