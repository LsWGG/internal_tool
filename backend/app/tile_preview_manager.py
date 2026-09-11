import json
import math
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


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

    @staticmethod
    def public(record):
        return {key: value for key, value in record.items() if key not in ("tile_root", "extensions")}

    def list(self):
        with self.lock:
            return [self.public(item) for item in sorted(self.layers.values(), key=lambda item: item["created_at"], reverse=True)]

    def tile(self, layer_id: str, z: int, x: int, y: int):
        with self.lock:
            record = self.layers.get(layer_id)
            roots = [(record.get("tile_root") or "", extension) for extension in record.get("extensions", [])] if record else []
        if not roots:
            return None
        root = (self.data_dir / layer_id).resolve()
        for prefix, extension in roots:
            path = (root / prefix / str(z) / str(x) / f"{y}{extension}").resolve()
            if root in path.parents and path.is_file():
                return path
        return None

    def delete(self, layer_id: str):
        with self.lock:
            if not self.layers.pop(layer_id, None):
                return False
            self._save()
        shutil.rmtree(self.data_dir / layer_id, ignore_errors=True)
        return True
