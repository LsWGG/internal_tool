import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

from script.maps_utils.image_metadata_json import main, read_metadata


class ImageMetadataJsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.image = self.directory / "sample.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("Description", "中文说明")
        Image.new("RGB", (12, 8), "red").save(self.image, pnginfo=info, dpi=(300, 300))

    def test_reads_dimensions_and_container_metadata(self):
        data = read_metadata(self.image, "pillow")
        self.assertEqual(data["image"]["width"], 12)
        self.assertEqual(data["image"]["height"], 8)
        self.assertEqual(data["container"]["Description"], "中文说明")

    def test_wasm_exports_grouped_fields(self):
        data = read_metadata(self.image, "wasm")
        self.assertEqual(data["metadata_engine"], "exiftool-wasm")
        self.assertEqual(data["fields"]["PNG:ImageWidth"], 12)

    def test_batch_writes_valid_json(self):
        output = self.directory / "output"
        self.assertEqual(main([str(self.directory), "-o", str(output)]), 0)
        result = output / "sample.png.metadata.json"
        self.assertEqual(json.loads(result.read_text("utf-8"))["file"]["name"], "sample.png")


if __name__ == "__main__":
    unittest.main()
