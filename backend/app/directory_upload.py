"""Browser directory uploads land under the tool data directory.

The hex folder is created by this app. Deleting it is allowed. A user-supplied
path is never removed here, and a symlink is never followed out of ``uploads``.
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

MAX_FILES = 5000
MAX_BYTES = 2 * 1024 * 1024 * 1024
_HEX = re.compile(r"^[0-9a-f]{32}$")

RASTER_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".webp", ".xml"}
SHAPE_SUFFIXES = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".xml"}
VECTOR_SUFFIXES = SHAPE_SUFFIXES | {".geojson", ".json", ".kml", ".gpkg", ".dxf"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_TIF_SUFFIXES = {".tif", ".tiff"}


class UploadTooLarge(ValueError):
    pass


def check_relative(raw: str) -> PurePosixPath:
    name = (raw or "").replace("\\", "/")
    if any(char in name for char in ("\x00", "\n", "\r")):
        raise ValueError("文件路径不安全")
    # Split before PurePosixPath, which would otherwise collapse ".." and ".".
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"文件路径不安全：{name or '未命名文件'}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"文件路径不安全：{name}")
    return PurePosixPath(*parts)


def new_upload(data_dir: Path) -> Path:
    root = Path(data_dir) / "uploads" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    return root


def member_path(root: Path, relative: PurePosixPath) -> Path:
    root_resolved = root.resolve()
    target = root.joinpath(*relative.parts)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    if resolved_parent != root_resolved and root_resolved not in resolved_parent.parents:
        raise ValueError("文件路径不安全")
    if target.is_symlink():
        raise ValueError("文件路径不安全")
    resolved = target.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("文件路径不安全")
    return target


def place_file(root: Path, relative: str, data: bytes = b"x") -> Path:
    target = member_path(root, check_relative(relative))
    target.write_bytes(data)
    return target


def _accepted(relative: PurePosixPath, allowed: set[str], extra_names: frozenset[str]) -> bool:
    return relative.name in extra_names or Path(relative.name).suffix.lower() in allowed


async def save_members(root: Path, files, allowed: set[str], extra_names: frozenset[str] = frozenset()) -> int:
    kept = 0
    total = 0
    seen = 0
    for upload in files or []:
        seen += 1
        if seen > MAX_FILES:
            raise UploadTooLarge("单次最多上传 5000 个文件")
        relative = check_relative(getattr(upload, "filename", None) or "")
        if not _accepted(relative, allowed, extra_names):
            continue
        target = member_path(root, relative)
        file_size = 0
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                file_size += len(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise UploadTooLarge("单次上传总大小不能超过 2GB")
                output.write(chunk)
        if not file_size:
            target.unlink(missing_ok=True)
            continue
        kept += 1
    if kept == 0:
        raise ValueError("目录里没有可处理的文件")
    return kept


def unwrap_upload(root: Path) -> Path:
    entries = [item for item in Path(root).iterdir() if not item.name.startswith(".")]
    dirs = [item for item in entries if item.is_dir()]
    files = [item for item in entries if item.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return Path(root)


def raster_source(directory: Path) -> Path:
    directory = Path(directory).resolve()
    tile_roots = set()
    tifs = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        suffix = path.suffix.lower()
        if suffix in _TIF_SUFFIXES:
            tifs.append(path)
        if suffix in _IMAGE_SUFFIXES and path.stem.isdigit() and path.parent.name.isdigit() and path.parent.parent.name.isdigit():
            tile_roots.add(path.parent.parent.parent.resolve())
    inside = {root for root in tile_roots if root == directory or directory in root.parents}
    if len(inside) == 1:
        return inside.pop()
    if len(inside) > 1:
        raise ValueError("目录里有多套瓦片，请只放一套，或改用服务器路径")
    if len(tifs) == 1:
        return tifs[0]
    if len(tifs) > 1:
        raise ValueError("目录里有多个 GeoTIFF，请只放一个，或改用服务器路径")
    raise ValueError("目录里没有可裁切的 GeoTIFF 或 z/x/y 瓦片")


def shapefile_source(directory: Path) -> Path:
    found = [path for path in Path(directory).rglob("*") if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".shp"]
    if len(found) != 1:
        raise ValueError("Shapefile 目录里要有且只有一个 .shp")
    return found[0]


def claimed_upload(data_dir: Path, raw: str) -> str:
    text = (raw or "").strip()
    if not text or any(char in text for char in ("\x00", "\n", "\r")):
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        return ""
    try:
        parent = path.parent.resolve()
        uploads = (Path(data_dir) / "uploads").resolve()
    except OSError:
        return ""
    if parent != uploads or not _HEX.fullmatch(path.name):
        return ""
    leaf = parent / path.name
    if leaf.is_symlink():
        return ""
    return str(leaf)


def remove_upload(data_dir: Path, raw: str) -> None:
    claimed = claimed_upload(data_dir, raw)
    if not claimed:
        return
    path = Path(claimed)
    if path.is_symlink() or not path.is_dir():
        return
    shutil.rmtree(path)
