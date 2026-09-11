"""PropZone/Gridics 行政区域（Zoning District）下载适配器。

该模块把 PropZone 使用的公开 API 封装成可重试的后台任务。搜索使用
Nominatim 获取全美行政区边界，下载时优先调用 Gridics zoning API；当某个
行政区没有对应的 PropZone 路径时，仍会把搜索到的行政区边界导出为 SHP，
避免页面点选后无法交付结果。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import shape

API_BASE = "https://pr1-api.gridics.com/api/ui-api"
GRAPHQL_URL = "https://propzone.gridics.com/graphql"
FALLBACK_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJpc3MiOiJodHRwczpcL1wvYWNjb3VudHMuZ3JpZGljcy5jb20iLCJpYXQiOjE3ODYwNzg5ODgsInVpZCI6IjAiLCJtYWlsIjoiIiwiZXhwIjoxNzg2MTY1Mzg4fQ."
    "wPaK6sPeQxEBog0xGQpwF7q5Dj8QnIbLAwAxuVh8W2Y"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (PropZoneWeb/1.0)", "Accept": "application/json, text/plain, */*",
           "Referer": "https://propzone.gridics.com/", "Origin": "https://propzone.gridics.com"}


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return value or "area"

_STATE_CODES = {"alabama":"al","alaska":"ak","arizona":"az","arkansas":"ar","california":"ca","colorado":"co","connecticut":"ct","delaware":"de","florida":"fl","georgia":"ga","hawaii":"hi","idaho":"id","illinois":"il","indiana":"in","iowa":"ia","kansas":"ks","kentucky":"ky","louisiana":"la","maine":"me","maryland":"md","massachusetts":"ma","michigan":"mi","minnesota":"mn","mississippi":"ms","missouri":"mo","montana":"mt","nebraska":"ne","nevada":"nv","new hampshire":"nh","new jersey":"nj","new mexico":"nm","new york":"ny","north carolina":"nc","north dakota":"nd","ohio":"oh","oklahoma":"ok","oregon":"or","pennsylvania":"pa","rhode island":"ri","south carolina":"sc","south dakota":"sd","tennessee":"tn","texas":"tx","utah":"ut","vermont":"vt","virginia":"va","washington":"wa","west virginia":"wv","wisconsin":"wi","wyoming":"wy","district of columbia":"dc"}
_STATE_CENTERS = {"al":(32.8,-86.8),"ak":(64.2,-149.5),"az":(34.3,-111.7),"ar":(35.1,-92.4),"ca":(37.2,-119.5),"co":(39.0,-105.5),"ct":(41.6,-72.7),"de":(39.0,-75.5),"dc":(38.9,-77.0),"fl":(28.6,-82.4),"ga":(32.7,-83.4),"hi":(20.8,-156.3),"id":(44.2,-114.4),"il":(40.0,-89.2),"in":(39.9,-86.3),"ia":(42.1,-93.5),"ks":(38.5,-98.4),"ky":(37.5,-85.3),"la":(31.0,-92.0),"me":(45.3,-69.0),"md":(39.0,-76.7),"ma":(42.3,-71.8),"mi":(44.3,-85.6),"mn":(46.3,-94.3),"ms":(32.7,-89.7),"mo":(38.5,-92.5),"mt":(47.1,-110.0),"ne":(41.5,-99.8),"nv":(39.3,-116.6),"nh":(43.7,-71.6),"nj":(40.1,-74.7),"nm":(34.5,-106.0),"ny":(42.9,-75.5),"nc":(35.5,-79.4),"nd":(47.5,-100.5),"oh":(40.4,-82.8),"ok":(35.6,-97.5),"or":(44.0,-120.5),"pa":(40.9,-77.8),"ri":(41.7,-71.6),"sc":(33.8,-80.9),"sd":(44.4,-100.2),"tn":(35.8,-86.4),"tx":(31.5,-99.3),"ut":(39.3,-111.7),"vt":(44.0,-72.7),"va":(37.5,-78.8),"wa":(47.4,-120.7),"wv":(38.6,-80.6),"wi":(44.5,-89.7),"wy":(43.0,-107.6)}


def _title(value, fallback=""):
    if isinstance(value, list):
        return str(value[0]) if value else fallback
    return str(value or fallback)


class PropZoneClient:
    def __init__(self):
        self.token = FALLBACK_TOKEN
        self.lock = threading.RLock()

    def fetch_token(self):
        response = requests.post(GRAPHQL_URL, json={"query": "{ ssoUser }"},
                                 headers={**HEADERS, "Content-Type": "application/json"}, timeout=20)
        response.raise_for_status()
        token = ((response.json().get("data") or {}).get("ssoUser") or {}).get("gridicsApiPublicToken")
        if not token:
            raise RuntimeError("PropZone 未返回公开访问令牌")
        with self.lock:
            self.token = token
        return token

    def _get(self, params, timeout=90):
        for attempt in range(3):
            with self.lock:
                token = self.token
            query = {**params, "publicToken": token}
            try:
                response = requests.get(API_BASE, params=query, headers=HEADERS, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                if data.get("status") not in (None, 0):
                    message = str(data.get("error") or data)
                    if attempt == 0 and any(word in message.lower() for word in ("expired", "forbidden", "token")):
                        try:
                            self.fetch_token()
                            continue
                        except Exception:
                            pass
                    raise RuntimeError(message)
                return data
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("PropZone 请求失败")

    def search_places(self, query: str, limit: int = 12):
        response = requests.get("https://nominatim.openstreetmap.org/search",
                                params={"q": query, "format": "jsonv2", "polygon_geojson": 1,
                                        "addressdetails": 1, "limit": max(1, min(limit, 100)), "countrycodes": "us"},
                                headers={"User-Agent": "PropZoneWeb/1.0 (local tool)"}, timeout=25)
        response.raise_for_status()
        results = []
        for item in response.json():
            geometry = item.get("geojson")
            if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            address = item.get("address") or {}
            state_code = (address.get("ISO3166-2-lvl4") or "").split("-")[-1].lower()
            if not state_code:
                state_code = _STATE_CODES.get(str(address.get("state", "")).lower(), _slug(address.get("state", ""))[:2])
            county = address.get("county", "")
            name = item.get("display_name") or item.get("name") or query
            place_name = item.get("name") or name.split(",")[0]
            kind = item.get("type") or item.get("class") or "administrative"
            # PropZone 的公开路径，用户也可在页面中直接修改/复制该路径。
            if county and kind in ("city", "town", "village", "municipality", "suburb"):
                propzone_url = f"/city/us/{state_code}/{_slug(county)}/{_slug(place_name)}"
            elif county and "unincorporated" in name.lower():
                propzone_url = f"/unincorporated/us/{state_code}/{_slug(county)}/{_slug(place_name)}"
            elif kind == "state":
                propzone_url = f"/state/us/{state_code}"
            else:
                propzone_url = f"/county/us/{state_code}/{_slug(place_name)}" if "county" in name.lower() else ""
            bbox = item.get("boundingbox") or []
            results.append({"id": f"{item.get('osm_type', 'osm')}-{item.get('osm_id', uuid.uuid4().hex[:8])}",
                            "name": name, "short_name": place_name, "type": kind, "class": item.get("class"), "state": state_code,
                            "county": county, "propzone_url": propzone_url,
                            "bounds": {"south": float(bbox[0]), "north": float(bbox[1]),
                                       "west": float(bbox[2]), "east": float(bbox[3])} if len(bbox) >= 4 else None,
                            "geometry": geometry})
        return results

    def search_children(self, area):
        """获取州级区域下的县级行政区，供树状选择使用。"""
        state_name = str(area.get("short_name") or area.get("name") or "").split(",")[0].strip()
        if not state_name:
            return []
        # 复用标准化逻辑，避免前端拿到另一套字段结构；去除州本身和重复县名。
        # Nominatim 对 “county, California” 往往只返回一个候选；使用复数行政层级
        # 能稳定返回该州的县级关系，再在本地做严格过滤。
        results = self.search_places(f"counties in {state_name}, United States", limit=100)
        unique, seen = [], set()
        for item in results:
            kind = str(item.get("type") or "").lower()
            text = str(item.get("name") or item.get("short_name") or "")
            if "county" not in text.lower() and kind not in ("administrative", "county"):
                continue
            key = item.get("propzone_url") or item.get("id")
            if key not in seen:
                seen.add(key); unique.append({**item, "parent_id": area.get("id")})
        return unique

    def reverse_place(self, lat: float, lon: float):
        response = requests.get("https://nominatim.openstreetmap.org/reverse",
                                params={"lat": lat, "lon": lon, "format": "jsonv2", "polygon_geojson": 1,
                                        "addressdetails": 1, "zoom": 12},
                                headers={"User-Agent": "PropZoneWeb/1.0 (local tool)"}, timeout=25)
        response.raise_for_status()
        item = response.json()
        if not item.get("display_name"):
            return None
        # 直接使用 reverse 返回的行政层级和边界，避免再次把完整地址交给
        # Nominatim 搜索后命中附近同名城市/景点。
        address = item.get("address") or {}
        state_name = address.get("state") or ""
        state_code = (address.get("ISO3166-2-lvl4") or "").split("-")[-1].lower() or _STATE_CODES.get(state_name.lower(), _slug(state_name)[:2])
        county = address.get("county") or ""
        place_name = address.get("city") or address.get("town") or address.get("village") or address.get("municipality") or county or state_name
        kind = "state" if place_name == state_name and state_name else ("administrative" if "county" in place_name.lower() else "city")
        geometry = item.get("geojson")
        if geometry and geometry.get("type") in ("Polygon", "MultiPolygon"):
            if kind == "state":
                propzone_url = f"/state/us/{state_code}"
            elif "county" in place_name.lower():
                propzone_url = f"/county/us/{state_code}/{_slug(place_name)}"
            else:
                propzone_url = f"/city/us/{state_code}/{_slug(county)}/{_slug(place_name)}" if county else ""
            return {"id": f"reverse-{item.get('osm_type', 'osm')}-{item.get('osm_id', uuid.uuid4().hex[:8])}",
                    "name": item.get("display_name"), "short_name": place_name, "type": kind,
                    "class": item.get("class"), "state": state_code, "county": county,
                    "propzone_url": propzone_url, "geometry": geometry}
        target = ", ".join(part for part in (place_name, state_name, "United States") if part)
        matches = self.search_places(target or item["display_name"])
        if matches:
            return matches[0]
        return None

    def fetch_zoning(self, area):
        place, state = self.resolve_place(area)
        if not place:
            return []
        place_id = place.get("id")
        if not place_id:
            return []
        jurisdiction = ",".join([place_id] + [f"{place_id}-{i}" for i in range(1, 10)])
        all_items, offset = [], 0
        while True:
            data = self._get({"action": "_land_use", "type": "_land_use", "fields[]": ["id", "title", "color"],
                              "geometryFormat": "json", "state_env": state, "jurisdiction": jurisdiction,
                              "publicVisibility": "1", "land_use_type": "municipal-zoning-code",
                              "rows": "200", "offset": str(offset), "order_by": "title"})
            batch = data.get("items") or []
            all_items.extend(batch)
            if not batch or len(all_items) >= (data.get("rows") or len(all_items)):
                break
            offset += len(batch)
            time.sleep(.2)
        features = []
        for item in all_items:
            geom = (item.get("gisData") or {}).get("geom") or item.get("geometry")
            if not geom:
                continue
            try:
                if isinstance(geom, str):
                    geom = json.loads(geom)
                geometry = shape(geom).__geo_interface__ if isinstance(geom, dict) else geom
            except Exception:
                continue
            features.append({"type": "Feature", "geometry": geometry,
                             "properties": {"zone_id": str(item.get("id") or ""),
                                             "zone_code": _title(item.get("title"), "Unknown"),
                                             "color": "#" + str(item.get("color") or "888888").lstrip("#")}})
        return features

    def resolve_place(self, area):
        url = "/" + str(area.get("propzone_url") or area.get("url") or "").strip("/")
        state = str(area.get("state") or "").lower()
        if not url or not state:
            return None, state
        place_data = self._get({"action": "_place", "featureLayer": "state", "type": "_place",
                                "fields[]": ["id", "title"], "geometryFormat": "json",
                                "importFeed": "census.gov,usa_neighborhoods", "state_env": state, "url": url})
        places = place_data.get("items") or []
        if not places:
            return None, state
        place = places[0]
        geometry = (place.get("gisData") or {}).get("geom") or place.get("geometry")
        if isinstance(geometry, str):
            try: geometry = json.loads(geometry)
            except json.JSONDecodeError: geometry = None
        if isinstance(geometry, dict):
            try: geometry = shape(geometry).__geo_interface__
            except Exception: geometry = None
        return {"id": place.get("id"), "title": _title(place.get("title")), "geometry": geometry}, state

    def resolve_area(self, area):
        place, state = self.resolve_place(area)
        if not place:
            raise ValueError(f"未找到 PropZone 区域：{area.get('propzone_url') or area.get('url')}")
        zones = self.fetch_zoning({**area, "state": state, "propzone_url": area.get("propzone_url") or area.get("url")})
        result = {**area, "id": area.get("id") or place.get("id"), "state": state, "place_id": place.get("id"),
                  "short_name": place.get("title") or area.get("short_name"),
                  "geometry": place.get("geometry") or area.get("geometry"), "zones": zones}
        return result


class _Control:
    def __init__(self):
        self.condition = threading.Condition(); self.paused = False; self.cancelled = False
    def checkpoint(self):
        with self.condition:
            while self.paused and not self.cancelled: self.condition.wait()
            if self.cancelled: raise RuntimeError("任务已取消")
    def pause(self):
        with self.condition: self.paused = True
    def resume(self):
        with self.condition: self.paused = False; self.condition.notify_all()
    def cancel(self):
        with self.condition: self.cancelled = True; self.paused = False; self.condition.notify_all()


class PropZoneTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir; data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"; self.cache_file = data_dir / "region_cache.json"; self.lock = threading.RLock(); self.client = PropZoneClient()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="propzone-task"); self.controls = {}
        try: self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError): self.tasks = {}
        for task in self.tasks.values():
            if task.get("status") in ("queued", "running", "paused"): task.update(status="failed", message="服务重启，任务已中断")
        self._save()
        # 首次启动即落盘州级离线目录，页面在完全断网时也能定位州并加载其缓存。
        if "state:__all__" not in self._read_cache():
            self.cached_search("", "state")
    def _read_cache(self):
        try:
            value = json.loads(self.cache_file.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_cache(self, cache):
        # 原子写入，避免服务异常退出时损坏离线目录。
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.cache_file)

    def _offline_matches(self, cache, query, level):
        """在历史查询缓存中做本地模糊检索，供断网时继续使用。"""
        needle = query.strip().lower()
        records = []
        for key, values in cache.items():
            if not key.startswith(f"{level}:") or not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                haystack = " ".join(str(item.get(field) or "") for field in ("short_name", "name", "state", "county", "propzone_url")).lower()
                if not needle or needle in haystack:
                    records.append(item)
        unique, seen = [], set()
        for item in records:
            key = item.get("propzone_url") or item.get("id")
            if key and key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:100]

    @staticmethod
    def _builtin_states():
        """返回不依赖网络的州目录；geometry 仅作定位兜底，联网后会被真实边界替换。"""
        return [{"id": f"state-{code}", "name": name.title(), "short_name": name.title(), "type": "state", "class": "boundary", "state": code, "propzone_url": f"/state/us/{code}", "center": {"lat": center[0], "lon": center[1]}, "geometry": {"type": "Polygon", "coordinates": [[[center[1]-1.5, center[0]-1.0], [center[1]+1.5, center[0]-1.0], [center[1]+1.5, center[0]+1.0], [center[1]-1.5, center[0]+1.0], [center[1]-1.5, center[0]-1.0]]]}} for name, code, center in ((name, code, _STATE_CENTERS.get(code, (39.5, -98.35))) for name, code in _STATE_CODES.items())]

    def cached_search(self, query: str, level: str = "all", refresh: bool = False):
        """按颗粒度缓存区域目录，并在网络不可用时从本地索引回退。"""
        level = level if level in ("all", "state", "county", "city") else "all"
        if level == "state" and not query.strip():
            states = self._builtin_states()
            cache = self._read_cache()
            cache["state:__all__"] = states
            self._write_cache(cache)
            return states
        builtin_matches = []
        if level in ("state", "all") and query.strip():
            needle = query.strip().lower()
            states = self._builtin_states()
            builtin_matches = [item for item in states if needle in item["short_name"].lower() or needle == item["state"]]
        search_query = query
        if level == "county" and query.strip() and "county" not in query.lower():
            search_query = f"{query} County, United States"
        cache = self._read_cache()
        key = f"{level}:{search_query.strip().lower()}"
        if not refresh and key in cache:
            return cache[key]
        try:
            result = self.client.search_places(search_query, limit=100 if level != "all" else 20)
            if level == "state":
                result = [x for x in result if x.get("type") == "state" or x.get("class") == "boundary" and "state" in str(x.get("name", "")).lower()]
            elif level == "county":
                result = [x for x in result if "county" in str(x.get("name", "")).lower() and x.get("type") in ("administrative", "county")]
            elif level == "city":
                result = [x for x in result if x.get("type") in ("city", "town", "village", "municipality")]
            else:
                # “全部”也只返回行政区域，避免 Convention、Bureau、景点等 POI 混入。
                result = [x for x in result if x.get("type") in ("state", "administrative", "county", "city", "town", "village", "municipality")]
            cache[key] = result
            self._write_cache(cache)
            return result
        except (requests.RequestException, requests.Timeout, requests.ConnectionError, OSError):
            fallback = self._offline_matches(cache, query, level)
            if fallback:
                return fallback
            if builtin_matches:
                return builtin_matches
            raise

    def cached_children(self, area, refresh=False):
        """缓存州下 County 列表，避免每次展开都依赖 Nominatim。"""
        state = str(area.get("state") or "").lower()
        state_name = str(area.get("short_name") or area.get("name") or "").split(",")[0].strip()
        key = f"children:{state or _slug(state_name)}"
        cache = self._read_cache()
        if not refresh and key in cache:
            return cache[key]
        try:
            results = self.client.search_children(area)
            cache[key] = results
            self._write_cache(cache)
            return results
        except (requests.RequestException, requests.Timeout, requests.ConnectionError, OSError):
            return cache.get(key, [])
    def _save(self):
        tmp = self.state_file.with_suffix(".tmp"); tmp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8"); tmp.replace(self.state_file)
    def _update(self, task_id, **values):
        with self.lock:
            if task_id in self.tasks:
                values["updated_at"] = datetime.now(timezone.utc).isoformat(); self.tasks[task_id].update(values); self._save()
    def list(self):
        with self.lock: return sorted(self.tasks.values(), key=lambda x: x["created_at"], reverse=True)
    def get(self, task_id):
        with self.lock: return self.tasks.get(task_id)
    def create(self, areas, level="all"):
        if not areas: raise ValueError("请至少选择一个行政区域")
        task_id, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        label = "、".join(str(a.get("short_name") or a.get("name") or "美国区域").split(",")[0] for a in areas[:3])
        if len(areas) > 3: label += f" 等 {len(areas)} 个区域"
        level_name = {"all": "行政区域", "state": "州", "county": "County", "city": "城市"}.get(level, "行政区域")
        record = {"id": task_id, "name": f"美国{level_name} · {label}", "status": "queued", "progress": 0,
                  "current": 0, "total": len(areas), "message": "等待下载", "created_at": now, "updated_at": now,
                  "request": {"areas": areas, "level": level, "output_format": "shp"}, "result": None, "error": None}
        with self.lock: self.tasks[task_id] = record; self.controls[task_id] = _Control(); self._save()
        self.pool.submit(self._run, task_id); return record
    def _run(self, task_id):
        task_dir = self.data_dir / task_id; output_dir = task_dir / "output"; output_dir.mkdir(parents=True, exist_ok=True)
        control = self.controls.get(task_id)
        try:
            areas = self.tasks[task_id]["request"]["areas"]; features = []
            for index, area in enumerate(areas, 1):
                control.checkpoint(); self._update(task_id, status="running", message=f"正在获取 {area.get('short_name') or area.get('name', '区域')} 数据")
                try:
                    zoning = self.client.fetch_zoning(area)
                except Exception as exc:
                    zoning = []
                    self._update(task_id, message=f"官方 zoning 不可用，使用 {area.get('short_name') or '行政区域'} 边界（{exc}）")
                selected_zone_ids = {str(value) for value in (area.get("selected_zone_ids") or [])}
                if selected_zone_ids:
                    zoning = [feature for feature in zoning
                              if str((feature.get("properties") or {}).get("zone_id")) in selected_zone_ids]
                if not zoning and area.get("geometry"):
                    zoning = [{"type": "Feature", "geometry": area["geometry"],
                               "properties": {"zone_id": area.get("id", ""), "zone_code": area.get("short_name") or area.get("name", "区域"), "color": "#4169e1"}}]
                features.extend(zoning); self._update(task_id, current=index, progress=round(index / len(areas) * 85, 1), message=f"已获取 {index}/{len(areas)} 个区域")
            if not features: raise RuntimeError("所选区域没有可下载的边界或 zoning 数据")
            geojson = {"type": "FeatureCollection", "features": features}
            (task_dir / "preview.geojson").write_text(json.dumps(geojson, ensure_ascii=False), "utf-8")
            records = []
            for feature in features:
                try: records.append({**feature.get("properties", {}), "geometry": shape(feature["geometry"])})
                except Exception: continue
            gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
            name = re.sub(r"[^\w\-一-龥]+", "_", self.tasks[task_id]["name"])[:80] or "propzone"
            shp_path = output_dir / f"{name}.shp"; gdf.to_file(shp_path, driver="ESRI Shapefile", encoding="UTF-8")
            (output_dir / f"{name}.cpg").write_text("UTF-8", "ascii")
            archive = shutil.make_archive(str(task_dir), "zip", output_dir)
            self._update(task_id, status="completed", progress=100, current=len(areas), message="SHP 下载完成",
                         result={"archive": Path(archive).name, "preview": "preview.geojson", "feature_count": len(records), "files": [p.name for p in output_dir.iterdir()]})
        except Exception as exc:
            if task_id in self.tasks: self._update(task_id, status="failed", message="下载失败", error=str(exc))
        finally:
            with self.lock: self.controls.pop(task_id, None)
    def control(self, task_id, action):
        with self.lock:
            task, control = self.tasks.get(task_id), self.controls.get(task_id)
            if not task: return None
            if action == "pause" and task["status"] in ("queued", "running"): control.pause(); task.update(status="paused", message="任务已暂停")
            elif action == "resume" and task["status"] == "paused": control.resume(); task.update(status="running", message="任务继续执行")
            else: raise ValueError("当前状态不支持此操作")
            task["updated_at"] = datetime.now(timezone.utc).isoformat(); self._save(); return task
    def delete(self, task_id):
        with self.lock:
            task = self.tasks.pop(task_id, None); control = self.controls.get(task_id)
            if not task: return False
            if control: control.cancel()
            self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True); (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True); return True
    def retry(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task: return None
            if task.get("status") != "failed": raise ValueError("仅失败任务可以重试")
            task.update(status="queued", progress=0, current=0, message="等待重新下载", result=None, error=None); self.controls[task_id] = _Control(); self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True); (self.data_dir / f"{task_id}.zip").unlink(missing_ok=True); self.pool.submit(self._run, task_id); return task
