import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import shapefile
from pyproj import CRS, Transformer


class ShpPreviewManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_file = data_dir / "layers.json"
        self.lock = threading.RLock()
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.layers = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.layers = {}
        # Upload directories are temporary staging areas. They may survive an
        # interrupted process, but are never valid persisted layer directories.
        for path in self.data_dir.iterdir():
            if path.is_dir() and path.name.startswith("upload-"):
                shutil.rmtree(path, ignore_errors=True)

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.layers, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(self.state_file)

    @staticmethod
    def _transform_geometry(geometry, transformer):
        def transform(value):
            if isinstance(value, (list, tuple)) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
                return list(transformer.transform(value[0], value[1]))
            return [transform(item) for item in value]
        return {"type": geometry["type"], "coordinates": transform(geometry["coordinates"])}

    @staticmethod
    def _fallback_geometry(shape):
        points = [list(point[:2]) for point in shape.points]
        if shape.shapeType in (shapefile.POINT, shapefile.POINTM, shapefile.POINTZ):
            return {"type": "Point", "coordinates": points[0]} if points else None
        if shape.shapeType in (shapefile.MULTIPOINT, shapefile.MULTIPOINTM, shapefile.MULTIPOINTZ):
            return {"type": "MultiPoint", "coordinates": points}
        parts = list(shape.parts) + [len(points)]
        segments = [points[parts[i]:parts[i + 1]] for i in range(len(parts) - 1)]
        if shape.shapeType in (shapefile.POLYLINE, shapefile.POLYLINEM, shapefile.POLYLINEZ):
            segments = [line for line in segments if len(line) >= 2]
            if not segments: return None
            return {"type": "LineString" if len(segments) == 1 else "MultiLineString",
                    "coordinates": segments[0] if len(segments) == 1 else segments}
        if shape.shapeType in (shapefile.POLYGON, shapefile.POLYGONM, shapefile.POLYGONZ):
            def area(ring):
                return sum(ring[i][0] * ring[(i + 1) % len(ring)][1] -
                           ring[(i + 1) % len(ring)][0] * ring[i][1]
                           for i in range(len(ring))) / 2.0
            rings = [ring for ring in segments if len(ring) >= 4 and abs(area(ring)) > 1e-12]
            return {"type": "Polygon", "coordinates": rings} if rings else None
        return None

    def create(self, group_dir: Path, stem: str):
        shp_path = group_dir / f"{stem}.shp"
        dbf_path = group_dir / f"{stem}.dbf"
        shx_path = group_dir / f"{stem}.shx"
        if not all(path.exists() for path in (shp_path, dbf_path, shx_path)):
            raise ValueError(f"{stem} 文件组缺少 SHP、SHX 或 DBF")
        prj_path = group_dir / f"{stem}.prj"
        if not prj_path.exists():
            raise ValueError(f"{stem} 文件组缺少 PRJ，无法确定地图位置")
        try:
            source_crs = CRS.from_wkt(prj_path.read_text("utf-8", errors="ignore"))
            transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        except Exception as exc:
            raise ValueError(f"{stem}.prj 坐标系无法解析：{exc}") from exc
        try:
            cpg_path = group_dir / f"{stem}.cpg"
            encoding = cpg_path.read_text("ascii", errors="ignore").strip() if cpg_path.exists() else "utf-8"
            reader = shapefile.Reader(str(shp_path), encoding=encoding or "utf-8", encodingErrors="replace")
            fields = [field[0] for field in reader.fields[1:]]
            features = []
            bounds = [180.0, 90.0, -180.0, -90.0]
            for item in reader.iterShapeRecords():
                try:
                    geometry = item.shape.__geo_interface__
                except Exception:
                    geometry = self._fallback_geometry(item.shape)
                if not geometry or not geometry.get("coordinates"):
                    continue
                geometry = self._transform_geometry(geometry, transformer)
                properties = dict(zip(fields, list(item.record)))
                features.append({"type": "Feature", "properties": properties, "geometry": geometry})
            if not features:
                raise ValueError(f"{stem}.shp 没有可预览的有效要素")
            raw_bounds = reader.bbox
            west, south = transformer.transform(raw_bounds[0], raw_bounds[1])
            east, north = transformer.transform(raw_bounds[2], raw_bounds[3])
            bounds = [west, south, east, north]
        except (shapefile.ShapefileException, UnicodeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith(stem):
                raise
            raise ValueError(f"{stem}.shp 读取失败：{exc}") from exc
        layer_id = uuid.uuid4().hex
        target = self.data_dir / layer_id
        shutil.move(str(group_dir), target)
        (target / "preview.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "features": features}, ensure_ascii=False, default=str), "utf-8")
        now = datetime.now(timezone.utc).isoformat()
        record = {"id": layer_id, "name": stem, "feature_count": len(features),
                  "shape_type": reader.shapeTypeName, "crs": source_crs.to_string(),
                  "bounds": bounds, "created_at": now}
        with self.lock:
            self.layers[layer_id] = record
            self._save()
        return record

    def list(self):
        with self.lock:
            return sorted(self.layers.values(), key=lambda item: item["created_at"])

    def get(self, layer_id):
        with self.lock:
            return self.layers.get(layer_id)

    def delete(self, layer_id):
        with self.lock:
            if not self.layers.pop(layer_id, None):
                return False
            self._save()
        shutil.rmtree(self.data_dir / layer_id, ignore_errors=True)
        return True
