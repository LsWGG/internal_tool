"""Self-contained GJB to Shapefile conversion adapter."""
import importlib.util
import json
from pathlib import Path

from pyproj import CRS, Transformer


def _load_converter():
    path = Path(__file__).with_name("gjb2shp.py")
    spec = importlib.util.spec_from_file_location("canonical_gjb2shp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gjb = _load_converter()


def discover(input_dir: Path):
    try:
        stem, groups, sms_path = gjb.find_group_files(str(input_dir))
    except SystemExit as exc:
        raise ValueError("未找到有效的 GJB 文件组，请检查 SMS、XMS、XSX、XTP、XZB 文件是否完整") from exc
    return stem, groups, sms_path


def _preview_geometry(kind, geom, ox, oy, scale, transformer):
    def points(values):
        return [[*transformer.transform(x, y)] for x, y in gjb.to_abs(values, ox, oy, scale)]
    if kind in ("P", "N"):
        return {"type": "Point", "coordinates": points([geom["anchor"]])[0]}
    if kind == "L":
        return {"type": "LineString", "coordinates": points(geom["coords"])}
    rings = [gjb.ensure_closed(ring) for ring in geom["rings"]]
    return {"type": "Polygon", "coordinates": [points(ring)
            for ring in rings if len(ring) >= 4 and abs(gjb.ring_area(ring)) > 1e-12]}


def _preview_quotas(parsed, limit):
    """按图层组和几何类型分层分配预览额度，避免后置图层被顺序截断。"""
    buckets = [(letter, kind, len(features))
               for letter, _, _, geoms in parsed
               for kind, features in geoms.items() if features]
    total = sum(size for _, _, size in buckets)
    if total <= limit:
        return {(letter, kind): size for letter, kind, size in buckets}
    quotas = {(letter, kind): max(1, int(size * limit / total))
              for letter, kind, size in buckets}
    used = sum(quotas.values())
    # 将取整剩余额度优先补给尚未完整展示的大图层。
    for letter, kind, size in sorted(buckets,
                                     key=lambda item: item[2] - quotas[(item[0], item[1])],
                                     reverse=True):
        if used >= limit:
            break
        key = (letter, kind)
        add = min(size - quotas[key], limit - used)
        quotas[key] += add
        used += add
    return quotas


def _sample_indexes(size, quota):
    if quota >= size:
        return range(size)
    if quota <= 1:
        return (size // 2,)
    return (round(i * (size - 1) / (quota - 1)) for i in range(quota))


def _valid_preview_geometry(kind, geom):
    if kind in ("P", "N"):
        return bool(geom.get("anchor"))
    if kind == "L":
        return len(geom.get("coords", [])) >= 2
    return any(len(gjb.ensure_closed(ring)) >= 4 and
               abs(gjb.ring_area(gjb.ensure_closed(ring))) > 1e-12
               for ring in geom.get("rings", []))


def _preview_name(kind, record):
    value = record.get("text") if kind == "N" else record.get("name")
    value = str(value or "").strip()
    return "" if value.upper() in {"NULL", "NONE", "N/A", "NAN", "-"} else value


def convert(input_dir: Path, output_dir: Path, crs_arg: str | None, progress):
    stem, groups, sms_path = discover(input_dir)
    sms_meta = gjb.parse_sms(sms_path) if sms_path else {}
    crs = gjb.resolve_crs(sms_meta, crs_arg or None)
    ox, oy, scale = gjb.resolve_offset(sms_meta)
    output_dir.mkdir(parents=True, exist_ok=True)

    parsed, total = [], 0
    for letter, ms_path, sx_path, tp_path, zb_path in groups:
        group_name, _, _ = gjb.parse_ms(ms_path)
        attrs = gjb.parse_sx(sx_path) if sx_path else {}
        geoms = gjb.parse_zb(zb_path) if zb_path else {}
        if tp_path:
            gjb.parse_tp(tp_path)
        total += sum(len(items) for items in geoms.values())
        parsed.append((letter, group_name, attrs, geoms))

    merged_types, written_total = [], 0
    for kind in gjb.ZB_TYPES:
        items = [(letter, name, attrs.get(kind, []), geoms.get(kind, []))
                 for letter, name, attrs, geoms in parsed if geoms.get(kind)]
        if not items:
            continue
        written = gjb.write_merged_shapefile(kind, items, str(output_dir / f"{stem}_all_{kind}"),
                                             crs, ox, oy, scale)
        merged_types.append(kind); written_total += written
        progress(written_total, max(1, total), f"已合并 {kind} 类要素：{written_total}/{total}")
    if not merged_types or written_total == 0:
        raise ValueError("GJB 文件组中没有可转换的有效几何数据")

    try:
        source_crs = CRS.from_user_input(crs)
        transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    except Exception as exc:
        raise ValueError(f"无法解析输出坐标系，不能生成地图预览：{exc}") from exc

    preview = []
    preview_limit = 5000
    preview_parsed = []
    for letter, group_name, attrs, geoms in parsed:
        valid = {kind: [(index, geom) for index, geom in enumerate(features)
                        if _valid_preview_geometry(kind, geom)]
                 for kind, features in geoms.items()}
        preview_parsed.append((letter, group_name, attrs, valid))
    quotas = _preview_quotas(preview_parsed, preview_limit)
    for letter, group_name, attrs, geoms in preview_parsed:
        for kind, features in geoms.items():
            for sampled_index in _sample_indexes(len(features), quotas.get((letter, kind), 0)):
                index, geom = features[sampled_index]
                records = attrs.get(kind, [])
                record = records[index] if index < len(records) else {}
                preview.append({"type": "Feature", "properties": {
                    "group": letter, "layer": group_name, "type": kind,
                    "code": record.get("code", ""), "name": _preview_name(kind, record)},
                    "geometry": _preview_geometry(kind, geom, ox, oy, scale, transformer)})
        progress(written_total, max(1, total), f"正在生成预览：{letter} 图层组")

    (output_dir.parent / "preview.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": preview}, ensure_ascii=False), "utf-8")
    metadata = {"stem": stem, "crs": source_crs.to_string(), "crs_wkt": source_crs.to_wkt(),
                "offset": [ox, oy], "scale": scale,
                "sms": gjb.sms_summary(sms_meta) if sms_meta else None}
    (output_dir / f"{stem}_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), "utf-8")
    files = [str(path.relative_to(output_dir)) for path in output_dir.iterdir() if path.is_file()]
    return {"group_count": len(groups), "merged_shapefile_count": len(merged_types),
            "geometry_types": merged_types, "feature_count": written_total,
            "skipped_invalid_count": total - written_total,
            "preview_count": len(preview), "crs": source_crs.to_string(), "files": files}
