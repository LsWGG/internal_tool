import math
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Callable

import requests
import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.transform import from_bounds

from .models import Bounds, TaskOptions

Progress = Callable[[int, int, str], None]
Checkpoint = Callable[[], None]

TILE_SOURCES = {
    "google_satellite": {"type": "satellite", "min_zoom": 0, "max_zoom": 20},
    "google_hybrid": {"type": "satellite", "min_zoom": 0, "max_zoom": 20},
    "bing_aerial": {"type": "satellite", "min_zoom": 1, "max_zoom": 20},
    "amap_satellite": {"type": "satellite", "min_zoom": 0, "max_zoom": 18},
    "amap_hybrid": {"type": "satellite", "min_zoom": 0, "max_zoom": 18},
    "amap_standard": {"type": "satellite", "min_zoom": 0, "max_zoom": 18},
    "osm_standard": {"type": "satellite", "min_zoom": 0, "max_zoom": 19},
    "opentopomap": {"type": "satellite", "min_zoom": 0, "max_zoom": 17},
    "aws_terrarium": {"type": "dem", "min_zoom": 0, "max_zoom": 15},
}


def lon_to_x(lon: float, zoom: int) -> int:
    return int((lon + 180.0) / 360.0 * (2**zoom))


def lat_to_y(lat: float, zoom: int) -> int:
    lat = max(-85.05112878, min(85.05112878, lat))
    return int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * (2**zoom))


def lon_to_pixel(lon: float, zoom: int) -> float:
    return (lon + 180.0) / 360.0 * (2**zoom) * 256


def lat_to_pixel(lat: float, zoom: int) -> float:
    lat = max(-85.05112878, min(85.05112878, lat))
    return (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * (2**zoom) * 256


def tile_bounds(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    n = 2**zoom
    west = x / n * 360 - 180
    east = (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def pixel_to_mercator(x: float, y: float, zoom: int) -> tuple[float, float]:
    radius = 6378137.0
    origin = math.pi * radius
    world_size = 256 * (2**zoom)
    return x / world_size * origin * 2 - origin, origin - y / world_size * origin * 2


def _quadkey(x: int, y: int, zoom: int) -> str:
    key = []
    for level in range(zoom, 0, -1):
        digit, mask = 0, 1 << (level - 1)
        if x & mask: digit += 1
        if y & mask: digit += 2
        key.append(str(digit))
    return "".join(key)


def fetch_tile(x: int, y: int, zoom: int, source: str = "google_satellite") -> Image.Image:
    headers = {"User-Agent": "Mozilla/5.0 SatelliteWeb/1.0"}
    if source == "google_satellite":
        url = f"https://mt{(x+y)%4}.google.com/vt/lyrs=s&x={x}&y={y}&z={zoom}"
    elif source == "google_hybrid":
        url = f"https://mt{(x+y)%4}.google.com/vt/lyrs=y&x={x}&y={y}&z={zoom}"
    elif source == "bing_aerial":
        url = f"https://ecn.t{(x+y)%4}.tiles.virtualearth.net/tiles/a{_quadkey(x,y,zoom)}.jpeg?g=1"
    elif source in ("amap_satellite", "amap_hybrid", "amap_standard"):
        server=(x+y)%4+1
        headers.update({"Referer":"https://www.amap.com/","User-Agent":"Mozilla/5.0"})
        if source == "amap_standard":
            url=f"http://webrd0{server}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={zoom}"
        else:
            url=f"https://webst0{server}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={zoom}"
    elif source == "osm_standard":
        url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    elif source == "opentopomap":
        url = f"https://tile.opentopomap.org/{zoom}/{x}/{y}.png"
    else:
        raise ValueError(f"不支持的影像数据源：{source}")
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    base=Image.open(BytesIO(response.content)).convert("RGB")
    if source == "amap_hybrid":
        label_url=f"https://wprd0{server}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scl=1&style=8&ltype=7&x={x}&y={y}&z={zoom}"
        label_response=requests.get(label_url,headers=headers,timeout=30);label_response.raise_for_status()
        label=Image.open(BytesIO(label_response.content)).convert("RGBA")
        base=Image.alpha_composite(base.convert("RGBA"),label).convert("RGB")
    return base


def fetch_dem_tile(x: int, y: int, zoom: int) -> Image.Image:
    url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{zoom}/{x}/{y}.png"
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 TerrainWeb/1.0"}, timeout=30)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGB")


def _valid_tile(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 100:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return image.size == (256, 256)
    except Exception:
        return False


def _load_tile(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB").copy()


def _fetch_with_retry(x: int, y: int, zoom: int, options: TaskOptions,
                      cache_path: Path, checkpoint: Checkpoint, max_retries: int = 6) -> Image.Image:
    if _valid_tile(cache_path):
        return _load_tile(cache_path)
    cache_path.unlink(missing_ok=True)
    error = None
    for attempt in range(max_retries):
        checkpoint()
        try:
            image = fetch_dem_tile(x, y, zoom) if options.data_type == "dem" else fetch_tile(x, y, zoom, options.tile_source)
            if image.size != (256, 256):
                raise ValueError(f"瓦片尺寸异常：{image.size}")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = cache_path.with_suffix(".part")
            image.save(temp, format="PNG")
            temp.replace(cache_path)
            if not _valid_tile(cache_path):
                cache_path.unlink(missing_ok=True)
                raise ValueError("瓦片写入后完整性校验失败")
            return image.convert("RGB")
        except Exception as exc:
            error = exc
            cache_path.with_suffix(".part").unlink(missing_ok=True)
            if attempt + 1 < max_retries:
                delay = min(30.0, 1.2 * (2 ** attempt)) + random.uniform(0, .8)
                end = time.monotonic() + delay
                while time.monotonic() < end:
                    checkpoint()
                    time.sleep(min(.5, end - time.monotonic()))
    raise RuntimeError(f"瓦片 {zoom}/{x}/{y} 重试 {max_retries} 次仍失败：{error}")


def decode_terrarium(pixels: np.ndarray) -> np.ndarray:
    values = pixels.astype(np.float32)
    return values[:, :, 0] * 256.0 + values[:, :, 1] + values[:, :, 2] / 256.0 - 32768.0


def _rings(geometry: dict):
    kind, coordinates = geometry.get("type"), geometry.get("coordinates", [])
    if kind == "Polygon":
        yield coordinates
    elif kind == "MultiPolygon":
        yield from coordinates


def _mask_geometry(image: Image.Image, geometry: dict, bounds: Bounds, zoom: int, left: int, top: int):
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    for polygon in _rings(geometry):
        if not polygon:
            continue
        projected = [[(round(lon_to_pixel(lon, zoom) - (lon_to_x(bounds.west, zoom) * 256) - left),
                       round(lat_to_pixel(lat, zoom) - (lat_to_y(bounds.north, zoom) * 256) - top))
                      for lon, lat in ring] for ring in polygon]
        if projected[0]:
            draw.polygon(projected[0], fill=255)
        for hole in projected[1:]:
            draw.polygon(hole, fill=0)
    background = Image.new("RGB", image.size, (0, 0, 0))
    return Image.composite(image, background, mask)


def _mask_geometry_at_pixels(image: Image.Image, geometry: dict, zoom: int,
                             origin_x: int, origin_y: int):
    mask=Image.new("L",image.size,0);draw=ImageDraw.Draw(mask)
    for polygon in _rings(geometry):
        if not polygon: continue
        projected=[[(round(lon_to_pixel(lon,zoom)-origin_x),round(lat_to_pixel(lat,zoom)-origin_y))
                    for lon,lat in ring] for ring in polygon]
        if projected[0]: draw.polygon(projected[0],fill=255)
        for hole in projected[1:]: draw.polygon(hole,fill=0)
    return Image.composite(image,Image.new("RGB",image.size,(0,0,0)),mask)


def _tile_range(bounds: Bounds, zoom: int):
    max_index = 2**zoom - 1
    x0 = max(0, min(max_index, math.floor(lon_to_pixel(bounds.west, zoom) / 256)))
    x1 = max(0, min(max_index, math.ceil(lon_to_pixel(bounds.east, zoom) / 256) - 1))
    y0 = max(0, min(max_index, math.floor(lat_to_pixel(bounds.north, zoom) / 256)))
    y1 = max(0, min(max_index, math.ceil(lat_to_pixel(bounds.south, zoom) / 256) - 1))
    return x0, x1, y0, y1


def _download_zoom(bounds: Bounds, geometry: dict | None, options: TaskOptions, output_dir: Path,
                   zoom: int, progress: Progress, checkpoint: Checkpoint, completed: int, total: int) -> dict:
    x0, x1, y0, y1 = _tile_range(bounds, zoom)
    tasks = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]

    failures: list[str] = []
    cache_root=output_dir/".tiles"/options.tile_source/f"z{zoom}"
    cached=[];missing=[]
    for x,y in tasks:
        path=cache_root/str(x)/f"{y}.png"
        if _valid_tile(path): cached.append((x,y,path))
        else:
            path.unlink(missing_ok=True);missing.append((x,y,path))
    progress(completed+len(cached), total, f"Z{zoom}：已复用 {len(cached)} 个，待下载 {len(missing)} 个瓦片")
    def controlled_fetch(x, y):
        checkpoint()
        return _fetch_with_retry(x,y,zoom,options,cache_root/str(x)/f"{y}.png",checkpoint)
    with ThreadPoolExecutor(max_workers=options.max_workers) as pool:
        futures = {pool.submit(controlled_fetch, x, y): (x, y) for x,y,_ in missing}
        for done, future in enumerate(as_completed(futures), 1):
            checkpoint()
            xy = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{xy[0]}/{xy[1]}: {exc}")
            progress(completed+len(cached)+done,total,f"Z{zoom}：已下载 {done}/{len(missing)}，复用 {len(cached)} 个瓦片")

    if failures:
        valid=sum(_valid_tile(cache_root/str(x)/f"{y}.png") for x,y in tasks)
        raise RuntimeError(f"Z{zoom} 有 {len(failures)} 个瓦片未通过完整性校验；已保留 {valid} 个有效瓦片，可稍后重试继续")
    valid=sum(_valid_tile(cache_root/str(x)/f"{y}.png") for x,y in tasks)
    if valid!=len(tasks): raise RuntimeError(f"Z{zoom} 瓦片不完整：需要 {len(tasks)}，有效 {valid}")

    files=[];prefix="dem" if options.data_type=="dem" else "satellite"
    if options.output_format=="png":
        for index,(x,y) in enumerate(tasks,1):
            checkpoint();source=cache_root/str(x)/f"{y}.png";target=output_dir/str(zoom)/str(x)/f"{y}.png"
            if not _valid_tile(target):
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
            files.append(str(target.relative_to(output_dir)))
            if index%200==0: progress(completed+len(tasks),total,f"Z{zoom}：正在整理 PNG 瓦片 {index}/{len(tasks)}")
    else:
        chunk_tiles=max(1,options.split_size//256)
        req_left=round(lon_to_pixel(bounds.west,zoom));req_right=round(lon_to_pixel(bounds.east,zoom))
        req_top=round(lat_to_pixel(bounds.north,zoom));req_bottom=round(lat_to_pixel(bounds.south,zoom))
        for chunk_y in range(y0,y1+1,chunk_tiles):
            for chunk_x in range(x0,x1+1,chunk_tiles):
                checkpoint();end_x=min(x1+1,chunk_x+chunk_tiles);end_y=min(y1+1,chunk_y+chunk_tiles)
                origin_x,origin_y=chunk_x*256,chunk_y*256
                canvas=Image.new("RGB",((end_x-chunk_x)*256,(end_y-chunk_y)*256))
                for y in range(chunk_y,end_y):
                    for x in range(chunk_x,end_x):
                        canvas.paste(_load_tile(cache_root/str(x)/f"{y}.png"),((x-chunk_x)*256,(y-chunk_y)*256))
                global_left=max(origin_x,req_left);global_top=max(origin_y,req_top)
                global_right=min(end_x*256,req_right);global_bottom=min(end_y*256,req_bottom)
                if global_right<=global_left or global_bottom<=global_top: continue
                part=canvas.crop((global_left-origin_x,global_top-origin_y,global_right-origin_x,global_bottom-origin_y))
                if geometry: part=_mask_geometry_at_pixels(part,geometry,zoom,global_left,global_top)
                west_m,north_m=pixel_to_mercator(global_left,global_top,zoom);east_m,south_m=pixel_to_mercator(global_right,global_bottom,zoom)
                transform=from_bounds(west_m,south_m,east_m,north_m,part.width,part.height);pixels=np.asarray(part)
                path=output_dir/f"{prefix}_z{zoom:02d}_x{chunk_x}_y{chunk_y}.tif";temp=path.with_suffix(".part")
                profile=dict(driver="GTiff",width=part.width,height=part.height,crs="EPSG:3857",transform=transform,compress="deflate")
                if options.data_type=="dem":
                    elevation=decode_terrarium(pixels)
                    with rasterio.open(temp,"w",count=1,dtype="float32",nodata=-32768.0,**profile) as dataset:
                        dataset.write(elevation,1);dataset.set_band_description(1,"Elevation (meters)")
                else:
                    with rasterio.open(temp,"w",count=3,dtype=pixels.dtype,**profile) as dataset:
                        for band in range(3): dataset.write(pixels[:,:,band],band+1)
                temp.replace(path);files.append(str(path.relative_to(output_dir)))
    return {"files": files, "tile_count": len(tasks), "cached_tiles": len(cached),
            "downloaded_tiles": len(missing), "failed_tiles": len(failures),
            "format": options.output_format, "data_type": options.data_type, "tile_source": options.tile_source,
            "crs": "EPSG:3857" if options.output_format == "tif" else None,
            "unit": "meter" if options.data_type == "dem" else None}


def download_bounds(bounds: Bounds, geometry: dict | None, options: TaskOptions, output_dir: Path,
                    progress: Progress, checkpoint: Checkpoint = lambda: None) -> dict:
    zooms = list(range(options.zoom_min, options.zoom_max + 1))
    counts = []
    for zoom in zooms:
        x0, x1, y0, y1 = _tile_range(bounds, zoom)
        counts.append((x1 - x0 + 1) * (y1 - y0 + 1))
    total = sum(counts)
    layers, completed = [], 0
    for zoom, count in zip(zooms, counts):
        checkpoint()
        info = _download_zoom(bounds, geometry, options, output_dir, zoom, progress, checkpoint, completed, total)
        layers.append({"zoom": zoom, **info})
        completed += count
    return {"layers": layers, "zoom_min": options.zoom_min, "zoom_max": options.zoom_max,
            "tile_count": total, "files": [f for layer in layers for f in layer["files"]]}


def validate_result_files(output_dir: Path, result: dict) -> None:
    errors=[]
    for relative in result.get("files",[]):
        path=output_dir/relative
        try:
            if not path.is_file() or path.stat().st_size==0: raise ValueError("文件为空或不存在")
            if path.suffix.lower()==".tif":
                with rasterio.open(path) as dataset:
                    if dataset.width<1 or dataset.height<1 or dataset.count<1: raise ValueError("GeoTIFF 尺寸无效")
                    dataset.read(1,window=((0,min(1,dataset.height)),(0,min(1,dataset.width))))
            else:
                with Image.open(path) as image: image.verify()
        except Exception as exc: errors.append(f"{relative}: {exc}")
    if errors:
        sample="；".join(errors[:5])
        raise RuntimeError(f"结果完整性校验失败 {len(errors)} 个：{sample}")
