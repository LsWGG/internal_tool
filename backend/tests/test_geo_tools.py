import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import rasterio
import shapefile
from PIL import Image
from pydantic import ValidationError
from rasterio.transform import from_bounds
from shapely.geometry import Point, box

from app.ai_capabilities import prompt_catalog, validate_action
from app.directory_upload import (
    RASTER_SUFFIXES, UploadTooLarge, check_relative, new_upload, place_file,
    raster_source, remove_upload, save_members, shapefile_source, unwrap_upload,
)
from app.mbtiles import pack_directory, unpack_mbtiles
from app.models import Bounds, CreateTask, TaskOptions
from app.path_jobs import TaskCancelled
from app.raster_clip import clip_raster, clip_tiles, load_geometry
from app.raster_clip_manager import RasterClipManager
from app.task_manager import TaskManager
from app.tile_preview_manager import TilePreviewManager
from app.tile_scheme import convert_scheme, detect_scheme
from app.vector_convert import convert_directory
from app.vector_convert_manager import VectorConvertManager


def _png(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def _pixel(path):
    with Image.open(path) as image:
        return image.getpixel((0, 0))[:3]


def _inline(manager):
    def submit(fn, /, *args, **kwargs):
        fn(*args, **kwargs)

    manager.pool.submit = submit
    return manager


def _tif(path):
    data = np.arange(400, dtype="float32").reshape(20, 20)
    transform = from_bounds(0, 0, 1, 1, 20, 20)
    with rasterio.open(path, "w", driver="GTiff", height=20, width=20, count=1, dtype="float32", crs="EPSG:4326", transform=transform) as dataset:
        dataset.write(data, 1)


def _geojson(path, name="a"):
    frame = gpd.GeoDataFrame({"name": [name]}, geometry=[Point(0.2, 0.3)], crs="EPSG:4326")
    frame.to_file(path, driver="GeoJSON")


class MbtilesTests(unittest.TestCase):
    def test_pack_uses_tms_row_and_second_run_skips(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _png(root / "1" / "0" / "0.png", (255, 0, 0))
            dest = root / "out.mbtiles"
            packed = pack_directory(root, dest, name="demo", task_id="task-a")
            self.assertEqual(packed["written"], 1)
            connection = sqlite3.connect(dest)
            try:
                row = connection.execute("SELECT tile_row FROM tiles WHERE zoom_level=1 AND tile_column=0").fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], 1)
            again = pack_directory(root, dest, name="demo", task_id="task-a")
            self.assertEqual(again["written"], 0)
            self.assertEqual(again["skipped"], 1)

    def test_refuses_existing_file_owned_by_someone_else(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _png(root / "1" / "0" / "0.png", (0, 0, 0))
            dest = root / "out.mbtiles"
            pack_directory(root, dest, name="demo", task_id="task-a")
            with self.assertRaisesRegex(ValueError, "目标 MBTiles 已存在"):
                pack_directory(root, dest, name="demo", task_id="task-b")
            foreign = root / "foreign.mbtiles"
            foreign.write_bytes(b"not-a-database")
            with self.assertRaisesRegex(ValueError, "目标 MBTiles 已存在"):
                pack_directory(root, foreign, name="demo", task_id="task-a")

    def test_empty_directory_and_empty_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "没有识别到 z/x/y 瓦片"):
                pack_directory(root, root / "empty.mbtiles", name="demo", task_id="task-a")
            archive = root / "blank.mbtiles"
            connection = sqlite3.connect(archive)
            connection.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
            connection.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "MBTiles 里没有瓦片"):
                unpack_mbtiles(archive, root / "unpacked")

    def test_unpack_round_trip_skips_matching_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _png(root / "1" / "0" / "0.png", (1, 2, 3))
            archive = root / "pack.mbtiles"
            pack_directory(root, archive, name="demo", task_id="task-a")
            output = root / "xyz"
            first = unpack_mbtiles(archive, output)
            self.assertTrue((output / "1" / "0" / "0.png").is_file())
            second = unpack_mbtiles(archive, output)
            self.assertEqual(second["skipped"], first["tiles"])
            self.assertEqual(second["written"], 0)


class SchemeTests(unittest.TestCase):
    def _tree(self, root):
        _png(root / "1" / "0" / "0.png", (255, 0, 0))
        _png(root / "1" / "0" / "1.png", (0, 0, 255))

    def test_flip_resumes_once_and_does_not_flip_again(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "tiles"
            self._tree(root)

            def checkpoint():
                raise TaskCancelled()

            with self.assertRaises(TaskCancelled):
                convert_scheme(root, "tms", in_place=True, checkpoint=checkpoint)
            journal = json.loads((root / ".scheme-journal.json").read_text("utf-8"))
            self.assertEqual(journal["target"], "tms")
            self.assertEqual(_pixel(root / "1" / "0" / "0.png"), (255, 0, 0))
            convert_scheme(root, "tms", in_place=True)
            self.assertEqual(_pixel(root / "1" / "0" / "0.png"), (0, 0, 255))
            self.assertEqual(_pixel(root / "1" / "0" / "1.png"), (255, 0, 0))
            self.assertEqual((root / ".tile-scheme").read_text("utf-8").strip(), "tms")
            self.assertTrue((root / "tilemapresource.xml").is_file())
            convert_scheme(root, "tms", in_place=True)
            self.assertEqual(_pixel(root / "1" / "0" / "0.png"), (0, 0, 255))
            self.assertFalse((root / ".scheme-journal.json").exists())

    def test_unfinished_journal_blocks_a_different_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "tiles"
            self._tree(root)

            def checkpoint():
                raise TaskCancelled()

            with self.assertRaises(TaskCancelled):
                convert_scheme(root, "tms", in_place=True, checkpoint=checkpoint)
            with self.assertRaisesRegex(ValueError, "已有未完成的方案转换（目标 tms），请先完成该转换"):
                convert_scheme(root, "wmts", in_place=True)

    def test_wmts_does_not_move_tiles(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "tiles"
            self._tree(root)
            convert_scheme(root, "wmts", in_place=True)
            self.assertEqual(_pixel(root / "1" / "0" / "0.png"), (255, 0, 0))
            self.assertEqual(_pixel(root / "1" / "0" / "1.png"), (0, 0, 255))
            self.assertTrue((root / "WMTSCapabilities.xml").is_file())
            self.assertFalse((root / "tilemapresource.xml").exists())
            self.assertEqual(detect_scheme(root), "wmts")

    def test_marker_wins_and_tilemapresource_wins_without_one(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "tilemapresource.xml").write_text("<TileMap/>", "utf-8")
            (root / "WMTSCapabilities.xml").write_text("<Capabilities/>", "utf-8")
            self.assertEqual(detect_scheme(root), "tms")
            (root / ".tile-scheme").write_text("xyz\n", "utf-8")
            self.assertEqual(detect_scheme(root), "xyz")


class RasterTests(unittest.TestCase):
    def test_clip_raster_leaves_the_source_alone(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "dem.tif"
            _tif(source)
            before = source.read_bytes()
            dest = root / "out" / "dem.tif"
            clip_raster(source, dest, box(0.2, 0.2, 0.6, 0.6), "task-a")
            self.assertEqual(source.read_bytes(), before)
            with rasterio.open(dest) as dataset:
                self.assertLess(dataset.width, 20)
                self.assertGreater(dataset.width, 0)

    def test_clip_tiles_copies_only_the_intersection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "tiles"
            west = root / "1" / "0" / "0.png"
            east = root / "1" / "1" / "0.png"
            _png(west, (9, 9, 9))
            _png(east, (3, 3, 3))
            output = Path(raw) / "clipped"
            result = clip_tiles(root, output, box(10, 10, 20, 20))
            copied = output / "1" / "1" / "0.png"
            self.assertTrue(copied.is_file())
            self.assertEqual(copied.read_bytes(), east.read_bytes())
            self.assertFalse((output / "1" / "0" / "0.png").exists())
            self.assertEqual(result["tiles"], 1)
            self.assertEqual((output / ".tile-scheme").read_text("utf-8").strip(), "xyz")

    def test_in_place_directory_is_rejected_and_raster_delete_checks_the_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tiles = root / "tiles"
            _png(tiles / "1" / "1" / "0.png", (1, 1, 1))
            source = root / "dem.tif"
            _tif(source)
            manager = _inline(RasterClipManager(root / "jobs"))
            try:
                with self.assertRaisesRegex(ValueError, "瓦片目录不能在原位置裁切"):
                    manager.validate({"source_path": str(tiles), "in_place": True, "west": 10, "south": 10, "east": 20, "north": 20})
                record = manager.create({
                    "source_path": str(source), "in_place": True,
                    "west": 0.2, "south": 0.2, "east": 0.6, "north": 0.6,
                })
                self.assertEqual(record["status"], "completed", record.get("error"))
                clip = source.with_name("dem_clip.tif")
                self.assertTrue(clip.is_file())
                marker = Path(str(clip) + ".task")
                marker.write_text(json.dumps({"task_id": "other", "source": str(source.resolve())}), "utf-8")
                self.assertTrue(manager.delete(record["id"]))
                self.assertTrue(clip.is_file())
                self.assertTrue(source.is_file())
            finally:
                manager.pool.shutdown(wait=False)

    def test_shp_geometry_is_used_when_present(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            frame = gpd.GeoDataFrame(geometry=[box(0.1, 0.1, 0.4, 0.4)], crs="EPSG:4326")
            shp = root / "area.shp"
            frame.to_file(shp)
            geom = load_geometry({"shp_path": str(shp), "west": 10, "south": 10, "east": 11, "north": 11})
            self.assertLess(geom.bounds[2], 1)


class VectorTests(unittest.TestCase):
    def test_convert_formats_and_reject_missing_crs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "src"
            source.mkdir()
            _geojson(source / "places.geojson")
            _geojson(source / "other.geojson", "b")
            for target in ("shp", "kml", "gpkg", "dxf"):
                output = root / target
                manifest = root / f"{target}.manifest.json"
                result = convert_directory(source, output, target, "task-a", manifest)
                self.assertTrue(result["outputs"])
                self.assertFalse((output / "preview.geojson").exists())
                self.assertFalse((source / "preview.geojson").exists())
            layers = list((root / "gpkg").glob("*.gpkg"))
            self.assertEqual(len(layers), 1)
            import pyogrio
            self.assertGreaterEqual(len(pyogrio.list_layers(layers[0])), 2)
            bare = root / "bare"
            bare.mkdir()
            with shapefile.Writer(str(bare / "nocrs")) as writer:
                writer.field("name", "C")
                writer.point(1, 2)
                writer.record("a")
            with self.assertRaisesRegex(ValueError, "无法识别坐标系"):
                convert_directory(bare, root / "bad", "geojson", "task-b", root / "bad.manifest.json")

    def test_in_place_delete_removes_only_owned_outputs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "vectors"
            source.mkdir()
            original = source / "places.geojson"
            _geojson(original)
            (source / "keep.txt").write_text("keep", "utf-8")
            manager = _inline(VectorConvertManager(root / "jobs"))
            try:
                record = manager.create({"source_path": str(source), "target_format": "shp", "in_place": True})
                self.assertEqual(record["status"], "completed", record.get("error"))
                self.assertTrue(list(source.glob("*.shp")))
                self.assertTrue(manager.delete(record["id"]))
                self.assertFalse(list(source.glob("*.shp")))
                self.assertFalse(list(source.glob("*.shx")))
                self.assertFalse(list(source.glob("*.dbf")))
                self.assertTrue(original.is_file())
                self.assertEqual((source / "keep.txt").read_text("utf-8"), "keep")
                self.assertFalse((source / "preview.geojson").exists())
            finally:
                manager.pool.shutdown(wait=False)

    def test_external_output_delete_removes_only_the_task_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "vectors"
            source.mkdir()
            original = source / "places.geojson"
            _geojson(original)
            manager = _inline(VectorConvertManager(root / "jobs"))
            try:
                record = manager.create({"source_path": str(source), "target_format": "geojson", "in_place": False})
                self.assertEqual(record["status"], "completed", record.get("error"))
                output = Path(record["output_path"])
                self.assertEqual(output.name, record["id"])
                self.assertTrue(output.is_dir())
                self.assertTrue(manager.delete(record["id"]))
                self.assertFalse(output.exists())
                self.assertTrue(original.is_file())
            finally:
                manager.pool.shutdown(wait=False)


class MapPackTests(unittest.TestCase):
    def test_options_reject_dem_and_tif(self):
        with self.assertRaisesRegex(ValidationError, "只有 PNG 影像可以打包为 MBTiles"):
            TaskOptions(data_type="dem", output_format="tif", tile_source="aws_terrarium", zoom_min=0, zoom_max=5, pack_mbtiles=True)
        with self.assertRaisesRegex(ValidationError, "只有 PNG 影像可以打包为 MBTiles"):
            TaskOptions(output_format="tif", zoom_min=1, zoom_max=1, pack_mbtiles=True)
        TaskOptions(data_type="satellite", output_format="png", zoom_min=1, zoom_max=1, pack_mbtiles=True)
        validate_action("configure", "map", {"pack_mbtiles": True, "data_type": "satellite"})
        with self.assertRaisesRegex(ValueError, "只有 PNG 影像可以打包为 MBTiles"):
            validate_action("configure", "map", {"pack_mbtiles": True, "output_format": "tif"})
        with self.assertRaisesRegex(ValueError, "只有 PNG 影像可以打包为 MBTiles"):
            validate_action("configure", "map", {"pack_mbtiles": True, "data_type": "dem", "tile_source": "aws_terrarium", "output_format": "tif"})

    def test_task_packs_mbtiles_into_the_zip(self):
        with tempfile.TemporaryDirectory() as raw:
            def fake(bounds, geometry, options, output_dir, progress, checkpoint):
                path = Path(output_dir) / "1" / "0" / "0.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (16, 16), (12, 34, 56, 255)).save(path)
                return {"files": ["1/0/0.png"], "tile_count": 1}

            manager = TaskManager(Path(raw))
            _inline(manager)
            try:
                request = CreateTask(
                    bounds=Bounds(west=-1, south=-1, east=1, north=1),
                    options=TaskOptions(zoom_min=0, zoom_max=0, output_name="sample", max_workers=1, pack_mbtiles=True),
                )
                with patch("app.task_manager.download_bounds", fake):
                    record = manager.create(request)
                task = manager.get(record["id"])
                self.assertEqual(task["status"], "completed", task.get("error"))
                self.assertTrue(task["result"]["mbtiles"].endswith(".mbtiles"))
                archive = Path(task["result"]["archive_path"])
                names = zipfile.ZipFile(archive).namelist()
                self.assertTrue(any(name.endswith(".mbtiles") for name in names))
                self.assertTrue((Path(task["output_dir"]) / task["result"]["mbtiles"]).is_file())
            finally:
                manager.pool.shutdown(wait=False)


class PreviewTests(unittest.TestCase):
    def test_mbtiles_preview_cache_stays_in_the_tool_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _png(root / "1" / "0" / "0.png", (4, 5, 6))
            archive = root / "layer.mbtiles"
            pack_directory(root, archive, name="layer", task_id="task-a")
            data = root / "preview"
            manager = TilePreviewManager(data)
            record = manager.open_mbtiles(str(archive), "外接")
            self.assertFalse((data / record["id"]).exists())
            cached = manager.tile(record["id"], 1, 0, 0)
            self.assertTrue(str(cached).startswith(str(data.resolve())))
            self.assertNotEqual(cached.parent, archive.parent)
            self.assertTrue(manager.delete(record["id"]))
            self.assertTrue(archive.is_file())

    def test_refresh_updates_scheme_and_ignores_an_empty_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            tiles = root / "tiles"
            _png(tiles / "1" / "0" / "0.png", (7, 7, 7))
            _png(tiles / "1" / "0" / "1.png", (8, 8, 8))
            manager = TilePreviewManager(root / "preview")
            record = manager.open_directory(str(tiles), "外接")
            self.assertEqual(record["scheme"], "xyz")
            convert_scheme(tiles, "tms", in_place=True)
            manager.refresh_directory(str(tiles))
            updated = next(item for item in manager.list() if item["id"] == record["id"])
            self.assertEqual(updated["scheme"], "tms")
            for path in tiles.rglob("*.png"):
                path.unlink()
            manager.refresh_directory(str(tiles))
            kept = next(item for item in manager.list() if item["id"] == record["id"])
            self.assertEqual(kept["tile_count"], updated["tile_count"])
            self.assertEqual(kept["scheme"], "tms")


class _FakeUpload:
    def __init__(self, name, payload):
        self.filename = name
        self.payload = payload
        self.offset = 0

    async def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += size
        return chunk


class DirectoryUploadTests(unittest.TestCase):
    def test_relative_names_and_raster_detection(self):
        for raw in ("../etc/passwd", "/etc/passwd", "C:/windows/a.tif", "dir/../a.tif", "dir/./a.tif"):
            with self.assertRaises(ValueError):
                check_relative(raw)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "jobs"
            data.mkdir()
            one = new_upload(data)
            place_file(one, "dem/one.tif", b"tif")
            self.assertEqual(raster_source(unwrap_upload(one)).name, "one.tif")
            tiles = new_upload(data)
            place_file(tiles, "tiles/1/2/3.png", b"png")
            place_file(tiles, "tiles/.tile-scheme", b"xyz")
            self.assertEqual(raster_source(unwrap_upload(tiles)), (tiles / "tiles").resolve())
            mixed = new_upload(data)
            place_file(mixed, "a.tif", b"a")
            place_file(mixed, "b.tif", b"b")
            with self.assertRaisesRegex(ValueError, "多个"):
                raster_source(unwrap_upload(mixed))
            loose = new_upload(data)
            place_file(loose, "pics/a.png", b"png")
            with self.assertRaisesRegex(ValueError, "没有可裁切"):
                raster_source(unwrap_upload(loose))
            shapes = new_upload(data)
            place_file(shapes, "clip/area.shp", b"s")
            place_file(shapes, "clip/area.dbf", b"d")
            self.assertEqual(shapefile_source(unwrap_upload(shapes)).name, "area.shp")
            place_file(shapes, "clip/other.shp", b"s")
            with self.assertRaisesRegex(ValueError, "只有一个"):
                shapefile_source(unwrap_upload(shapes))

    def test_save_members_skips_junk_and_caps_size(self):
        import asyncio

        async def run():
            with tempfile.TemporaryDirectory() as raw:
                data = Path(raw) / "jobs"
                data.mkdir()
                root = new_upload(data)
                await save_members(root, [
                    _FakeUpload("folder/one.tif", b"abc"),
                    _FakeUpload("folder/note.txt", b"no"),
                    _FakeUpload("folder/empty.tif", b""),
                    _FakeUpload("folder/.tile-scheme", b"xyz"),
                ], RASTER_SUFFIXES, extra_names=frozenset({".tile-scheme"}))
                self.assertEqual((root / "folder" / "one.tif").read_bytes(), b"abc")
                self.assertFalse((root / "folder" / "note.txt").exists())
                self.assertFalse((root / "folder" / "empty.tif").exists())
                self.assertEqual((root / "folder" / ".tile-scheme").read_bytes(), b"xyz")
                with self.assertRaises(ValueError):
                    await save_members(root, [_FakeUpload("../x.tif", b"abc")], RASTER_SUFFIXES)
                self.assertFalse((data.parent / "x.tif").exists())
                with patch("app.directory_upload.MAX_BYTES", 3):
                    with self.assertRaises(UploadTooLarge):
                        await save_members(new_upload(data), [_FakeUpload("big.tif", b"abcd")], RASTER_SUFFIXES)
                with self.assertRaisesRegex(ValueError, "没有可处理"):
                    await save_members(new_upload(data), [_FakeUpload("a.txt", b"hi")], RASTER_SUFFIXES)

        asyncio.run(run())

    def test_remove_upload_only_deletes_a_real_hex_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "jobs"
            data.mkdir()
            upload = new_upload(data)
            (upload / "keep.txt").write_text("gone", "utf-8")
            neighbor = data / "uploads" / "not-a-hex"
            neighbor.mkdir()
            (neighbor / "keep.txt").write_text("keep", "utf-8")
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", "utf-8")
            link = data / "uploads" / ("b" * 32)
            link.symlink_to(outside, target_is_directory=True)
            remove_upload(data, str(outside))
            remove_upload(data, str(neighbor))
            remove_upload(data, str(link))
            self.assertTrue((outside / "keep.txt").is_file())
            self.assertTrue((neighbor / "keep.txt").is_file())
            self.assertTrue(link.is_symlink())
            remove_upload(data, str(upload))
            self.assertFalse(upload.exists())
            self.assertTrue((outside / "keep.txt").is_file())

    def test_task_delete_removes_the_upload_and_not_a_neighbor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = root / "jobs"
            raster = _inline(RasterClipManager(data))
            vector = _inline(VectorConvertManager(root / "vector-jobs"))
            try:
                upload = new_upload(data)
                source = upload / "dem.tif"
                _tif(source)
                neighbor = data / "uploads" / "not-owned"
                neighbor.mkdir()
                (neighbor / "keep.txt").write_text("keep", "utf-8")
                record = raster.create({
                    "source_path": str(source), "upload_dir": str(upload),
                    "west": 0.2, "south": 0.2, "east": 0.6, "north": 0.6,
                })
                self.assertEqual(record["status"], "completed", record.get("error"))
                self.assertEqual(record["request"]["upload_dir"], str(upload.resolve()))
                output = Path(record["output_path"])
                self.assertTrue(output.is_dir())
                self.assertTrue(raster.delete(record["id"]))
                self.assertFalse(upload.exists())
                self.assertFalse(output.exists())
                self.assertEqual((neighbor / "keep.txt").read_text("utf-8"), "keep")

                outside = root / "outside.tif"
                _tif(outside)
                forged = raster.create({
                    "source_path": str(outside), "upload_dir": str(neighbor),
                    "west": 0.2, "south": 0.2, "east": 0.6, "north": 0.6,
                })
                self.assertEqual(forged["request"]["upload_dir"], "")
                self.assertTrue(raster.delete(forged["id"]))
                self.assertTrue(outside.is_file())
                self.assertTrue((neighbor / "keep.txt").is_file())

                vector_upload = new_upload(vector.data_dir)
                folder = vector_upload / "vectors"
                folder.mkdir()
                _geojson(folder / "places.geojson")
                kept = root / "neighbor.txt"
                kept.write_text("keep", "utf-8")
                converted = vector.create({
                    "source_path": str(folder), "upload_dir": str(vector_upload), "target_format": "geojson",
                })
                self.assertEqual(converted["status"], "completed", converted.get("error"))
                converted_output = Path(converted["output_path"])
                self.assertTrue(vector.delete(converted["id"]))
                self.assertFalse(vector_upload.exists())
                self.assertFalse(converted_output.exists())
                self.assertEqual(kept.read_text("utf-8"), "keep")
            finally:
                raster.pool.shutdown(wait=False)
                vector.pool.shutdown(wait=False)


class GeoAiTests(unittest.TestCase):
    def test_raster_and_vector_are_registered(self):
        catalog = prompt_catalog()
        self.assertNotIn("manual_reason", catalog["raster-clip"])
        self.assertNotIn("manual_reason", catalog["vector"])
        self.assertIn("create", catalog["raster-clip"]["actions"])
        self.assertIn("create", catalog["vector"]["actions"])
        filled = validate_action("configure", "raster-clip", {"in_place": True})
        self.assertFalse(filled["requires_confirmation"])
        with self.assertRaisesRegex(ValueError, "Shapefile 或框选范围"):
            validate_action("create", "raster-clip", {"source_path": "/data/a.tif"})
        created = validate_action("create", "raster-clip", {
            "source_path": "/data/a.tif", "west": 1, "south": 2, "east": 3, "north": 4,
        })
        self.assertEqual(created["adapter"], "raster-clip.create")
        self.assertTrue(created["requires_confirmation"])
        validate_action("create", "raster-clip", {"source_path": "/data/a.tif", "shp_path": "/data/a.shp"})
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            validate_action("create", "raster-clip", {
                "source_path": "relative.tif", "west": 1, "south": 2, "east": 3, "north": 4,
            })
        with self.assertRaisesRegex(ValueError, "不需要另填输出路径"):
            validate_action("create", "raster-clip", {
                "source_path": "/data/a.tif", "in_place": True, "output_path": "/data/out",
                "west": 1, "south": 2, "east": 3, "north": 4,
            })
        with self.assertRaisesRegex(ValueError, "同时"):
            validate_action("configure", "raster-clip", {"source_path": "/data/a.tif", "west": 1})
        with self.assertRaisesRegex(ValueError, "范围坐标无效"):
            validate_action("create", "raster-clip", {
                "source_path": "/data/a.tif", "west": 5, "south": 0, "east": 1, "north": 2,
            })
        with self.assertRaises(ValueError):
            validate_action("configure", "raster-clip", {"west": True})
        with self.assertRaisesRegex(ValueError, "target_format"):
            validate_action("create", "vector", {"source_path": "/data/vectors"})
        converted = validate_action("create", "vector", {"source_path": "/data/vectors", "target_format": "shp"})
        self.assertEqual(converted["adapter"], "vector.create")
        with self.assertRaisesRegex(ValueError, "不需要另填输出路径"):
            validate_action("create", "vector", {
                "source_path": "/data/vectors", "target_format": "shp", "in_place": True, "output_path": "/tmp/out",
            })
        validate_action("query", "raster-clip", {})
        validate_action("control", "vector", {"task_id": "a" * 8, "action": "pause"})


if __name__ == "__main__":
    unittest.main()
