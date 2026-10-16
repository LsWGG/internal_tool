import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shapefile
from PIL import Image
from pydantic import ValidationError

from app.downloader import _tile_range, download_bounds
from app.gjb_converter import _ready_shapefile
from app.gjb_manager import GJBTaskManager
from app.local_path import resolve_directory
from app.models import Bounds, CreateTask, TaskOptions
from app.propzone_manager import PropZoneTaskManager, _Control
from app.shp_preview_manager import ShpPreviewManager
from app.task_manager import TaskManager
from app.tile_preview_manager import TilePreviewManager


WGS84 = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)


def _bounds():
    return Bounds(west=-1, south=-1, east=1, north=1)


def _request(download_path=""):
    return CreateTask(
        bounds=_bounds(),
        options=TaskOptions(zoom_min=0, zoom_max=0, output_name="t", max_workers=1, download_path=download_path),
    )


def _task_record(task_id, status, request):
    return {
        "id": task_id, "status": status, "progress": 0, "current": 0, "total": 0,
        "message": "old", "created_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
        "request": request.model_dump(), "result": None, "error": "stale",
    }


def _point_image(x, y, zoom, source="google_satellite"):
    return Image.new("RGB", (256, 256), (x % 200, y % 200, 40))


class DirectoryResolutionTests(unittest.TestCase):
    def test_rejects_root_relative_missing_and_unwritable(self):
        with self.assertRaises(ValueError):
            resolve_directory("/")
        with self.assertRaises(ValueError):
            resolve_directory("maps/output")
        with self.assertRaises(ValueError):
            resolve_directory("/tmp/internal-tool-missing-dir")
        with self.assertRaises(ValueError):
            resolve_directory("/tmp/bad\x00path")
        with tempfile.TemporaryDirectory() as folder:
            locked = Path(folder) / "locked"
            locked.mkdir()
            locked.chmod(0o555)
            try:
                with self.assertRaises(ValueError):
                    resolve_directory(str(locked), writable=True)
            finally:
                locked.chmod(0o755)

    def test_prepare_checks_the_mounted_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = TaskManager(Path(folder) / "data")
            try:
                with self.assertRaises(ValueError):
                    manager.prepare(_request("/"))
                with self.assertRaises(ValueError):
                    manager.prepare(_request(str(Path(folder) / "missing")))
                locked = Path(folder) / "locked"
                locked.mkdir()
                locked.chmod(0o555)
                try:
                    with self.assertRaises(ValueError):
                        manager.prepare(_request(str(locked)))
                finally:
                    locked.chmod(0o755)
                manager.prepare(_request(folder))
            finally:
                manager.pool.shutdown(wait=False)

    def test_relative_download_path_is_rejected_before_prepare(self):
        with self.assertRaises(ValidationError):
            TaskOptions(download_path="relative/maps")


class MapResumeTests(unittest.TestCase):
    def test_custom_png_second_run_does_not_fetch_when_pieces_match(self):
        bounds = Bounds(west=-20, south=-20, east=20, north=20)
        options = TaskOptions(
            zoom_min=2, zoom_max=2, output_format="png", png_width=128, png_height=128,
            max_workers=2, output_name="custom",
        )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            with patch("app.downloader.fetch_tile", side_effect=_point_image) as fetch:
                first = download_bounds(bounds, None, options, output, lambda *_: None)
                self.assertGreater(fetch.call_count, 0)
                self.assertGreaterEqual(len(first["files"]), 1)
                for name in first["files"]:
                    parts = Path(name).parts
                    self.assertEqual(len(parts), 3)
                    self.assertTrue(name.endswith(".png"))
                    with Image.open(output / name) as image:
                        self.assertEqual(image.size, (128, 128))
                shutil.rmtree(output / ".tiles")
                fetch.reset_mock()
                second = download_bounds(bounds, None, options, output, lambda *_: None)
            self.assertEqual(fetch.call_count, 0)
            self.assertEqual(second["layers"][0]["downloaded_tiles"], 0)
            self.assertEqual(second["layers"][0]["cached_tiles"], second["layers"][0]["tile_count"])

    def test_raw_png_skips_fetch_when_output_tile_exists_without_cache(self):
        bounds = _bounds()
        options = TaskOptions(zoom_min=0, zoom_max=0, output_format="png", max_workers=1, output_name="raw")
        x0, _, y0, _ = _tile_range(bounds, 0)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            target = output / "0" / str(x0) / f"{y0}.png"
            target.parent.mkdir(parents=True)
            Image.new("RGB", (256, 256), (8, 8, 8)).save(target)
            with patch("app.downloader.fetch_tile", side_effect=_point_image) as fetch:
                result = download_bounds(bounds, None, options, output, lambda *_: None)
            self.assertEqual(fetch.call_count, 0)
            self.assertEqual(result["layers"][0]["downloaded_tiles"], 0)
            cache = output / ".tiles" / "google_satellite" / "z0" / str(x0) / f"{y0}.png"
            self.assertTrue(cache.is_file())


class TaskManagerPathTests(unittest.TestCase):
    def test_restart_requeues_active_tasks_and_leaves_paused(self):
        request = _request()
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "tasks.json").write_text(json.dumps({
                "queued1": _task_record("queued1", "queued", request),
                "running1": _task_record("running1", "running", request),
                "paused1": _task_record("paused1", "paused", request),
            }), "utf-8")
            with patch("app.task_manager.ThreadPoolExecutor") as pool_cls:
                manager = TaskManager(data)
            submitted = [call.args[1] for call in pool_cls.return_value.submit.call_args_list]
            self.assertEqual(submitted, ["queued1", "running1"])
            self.assertEqual(manager.tasks["queued1"]["status"], "queued")
            self.assertEqual(manager.tasks["running1"]["status"], "queued")
            self.assertEqual(manager.tasks["running1"]["message"], "服务重启，从已下载内容继续")
            self.assertIsNone(manager.tasks["running1"]["error"])
            self.assertEqual(manager.tasks["paused1"]["status"], "paused")
            self.assertEqual(manager.tasks["paused1"]["message"], "old")

    def test_external_archive_sits_beside_task_dir_and_delete_keeps_other_names(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as external:
            data = Path(folder)
            disk = Path(external)
            (disk / "keep-data").mkdir()
            (disk / "keep-data" / "secret.txt").write_text("stay", "utf-8")
            (disk / "marker.txt").write_text("stay", "utf-8")

            def fake_download(bounds, geometry, options, output_dir, progress, checkpoint):
                (output_dir / "tile.png").write_bytes(b"png")
                return {"files": ["tile.png"]}

            with patch("app.task_manager.ThreadPoolExecutor") as pool_cls, \
                    patch("app.task_manager.download_bounds", side_effect=fake_download), \
                    patch("app.task_manager.validate_result_files"):
                pool_cls.return_value.submit.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
                manager = TaskManager(data)
                record = manager.create(_request(str(disk)))
            task_id = record["id"]
            self.assertEqual(record["status"], "completed")
            self.assertTrue((disk / task_id).is_dir())
            self.assertTrue((disk / f"{task_id}.zip").is_file())
            self.assertFalse((data / f"{task_id}.zip").exists())
            self.assertTrue(manager.delete(task_id))
            self.assertFalse((disk / task_id).exists())
            self.assertFalse((disk / f"{task_id}.zip").exists())
            self.assertEqual((disk / "keep-data" / "secret.txt").read_text("utf-8"), "stay")
            self.assertTrue((disk / "marker.txt").is_file())


class PreviewPathTests(unittest.TestCase):
    def test_tile_open_does_not_copy_or_delete_the_source(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            source = Path(folder) / "disk"
            tile = source / "1" / "0" / "0.png"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b"png-bytes")
            (source / "readme.txt").write_text("keep", "utf-8")
            manager = TilePreviewManager(data)
            record = manager.open_directory(str(source), "外接")
            self.assertTrue(record["external"])
            self.assertFalse((data / record["id"]).exists())
            self.assertEqual(manager.tile(record["id"], 1, 0, 0), tile.resolve())
            self.assertTrue(manager.delete(record["id"]))
            self.assertTrue(tile.is_file())
            self.assertEqual((source / "readme.txt").read_text("utf-8"), "keep")

    def test_shp_preview_sidecar_stays_in_the_tool_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            source = Path(folder) / "disk"
            source.mkdir()
            writer = shapefile.Writer(str(source / "sample"), shapeType=shapefile.POINT)
            writer.field("name", "C", size=20)
            writer.point(10, 20)
            writer.record("alpha")
            writer.close()
            (source / "sample.prj").write_text(WGS84, "utf-8")
            manager = ShpPreviewManager(data)
            created = manager.open_directory(str(source))
            self.assertEqual(len(created), 1)
            layer_id = created[0]["id"]
            self.assertTrue((data / layer_id / "preview.geojson").is_file())
            self.assertFalse((source / "preview.geojson").exists())
            self.assertTrue(manager.delete(layer_id))
            self.assertFalse((data / layer_id).exists())
            self.assertTrue((source / "sample.shp").is_file())
            self.assertTrue((source / "sample.prj").is_file())


class PropzoneResumeTests(unittest.TestCase):
    def test_second_run_skips_a_cached_area(self):
        area = {
            "name": "Test County", "short_name": "Test", "id": "1",
            "geometry": {"type": "Point", "coordinates": [-97.0, 35.0]},
        }
        feature = {"type": "Feature", "geometry": area["geometry"], "properties": {"zone_id": "z", "zone_code": "T"}}
        with tempfile.TemporaryDirectory() as folder:
            manager = PropZoneTaskManager(Path(folder))
            manager.pool.submit = lambda fn, *args, **kwargs: None
            try:
                record = manager.create([area])
                calls = {"count": 0}

                def fetch_zoning(area):
                    calls["count"] += 1
                    return [feature]

                manager.client.fetch_zoning = fetch_zoning
                manager._run(record["id"])
                self.assertEqual(calls["count"], 1)
                cache = Path(record["output_dir"]) / "areas" / "1.geojson"
                self.assertTrue(cache.is_file())
                calls["count"] = 0
                manager.controls[record["id"]] = _Control()
                manager._run(record["id"])
                self.assertEqual(calls["count"], 0)
                self.assertEqual(manager.get(record["id"])["status"], "completed")
            finally:
                manager.pool.shutdown(wait=False)


class GjbPathTests(unittest.TestCase):
    def test_ready_shapefile_accepts_a_tiny_file_and_rejects_a_missing_one(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "tiny"
            writer = shapefile.Writer(str(base), shapeType=shapefile.POINT)
            writer.field("name", "C", size=20)
            writer.point(1, 2)
            writer.record("a")
            writer.close()
            self.assertEqual(_ready_shapefile(base), 1)
            self.assertIsNone(_ready_shapefile(Path(folder) / "missing"))

    def test_discard_in_place_does_not_remove_the_source(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            source = Path(folder) / "source"
            source.mkdir()
            (source / "keep.txt").write_text("stay", "utf-8")
            (source / "other").mkdir()
            manager = GJBTaskManager(data)
            try:
                task_id = "abc123"
                sidecar = data / task_id
                sidecar.mkdir()
                (sidecar / "preview.geojson").write_text("{}", "utf-8")
                task = {
                    "id": task_id, "output_dir": str(source),
                    "request": {"source_path": str(source), "in_place": True, "crs": ""},
                }
                manager._discard(task_id, task)
                self.assertTrue((source / "keep.txt").is_file())
                self.assertTrue((source / "other").is_dir())
                self.assertFalse(sidecar.exists())
            finally:
                manager.pool.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
