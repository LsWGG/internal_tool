#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 GJB5068-2004 军用数字地图矢量数据交换格式转换为 ESRI Shapefile (.shp)。

GJB5068 文件组由同名前缀的多个文件组成，一个图层组包含：
    .XMS  主索引文件(图层组名、组成文件清单、各类要素数量)
    .XSX  层索引文件(要素编码、名称、说明、几何类型码、属性指针等)
    .XTP  属性表文件(要素序号 → 属性记录指针)
    .XZB  坐标/几何数据文件(P点 / L线 / A面 / N注记)
另有:
    .SMS  图幅元数据文件(图名、图号、投影、带号、相对原点等)

图层组字母含义: A测量控制点 B工农业社会文化设施 C居民地及附属设施
D陆地交通 E管线 F水域/陆地 I水文 J陆地地貌及土质 K境界与政区
L植被 R注记

坐标说明: .XZB 中的坐标是相对图幅"相对原点"的偏移量，
真实高斯-克吕格坐标 = 相对原点 + 偏移量 × 放大系数 (取自 .SMS)。
默认按 CGCS2000 / 6度带高斯-克吕格投影 (EPSG:4498) 写出 .prj。

用法示例：
    uv run --with pyshp gjb2shp.py "zone - 副本" --outdir out
    uv run --with pyshp gjb2shp.py zone.AZB --outdir out
    uv run --with pyshp gjb2shp.py zone.BZB --merge --crs EPSG:4498
"""

import argparse
import glob
import os
import re
import sys

import shapefile  # pyshp


# ============================================================
# 格式常量
# ============================================================

# 统一文件头部: 文件名 / Y / B / ABCDEFIJKLR??????? / GJB5068-2004 / 描述
HEADER_LINES = 6

# 图层组字母
GROUP_LETTERS = 'ABCDEFIJKLR'

# ZB 几何类型: P点 L线 A面 N注记
ZB_TYPES = 'PLAN'

# 常用坐标系的 WKT(用于 .prj 文件)
CRS_WKT = {
    'EPSG:4326': (
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
        'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],'
        'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","4326"]]'
    ),
    'EPSG:4490': (
        'GEOGCS["China Geodetic Coordinate System 2000",'
        'DATUM["China_2000",SPHEROID["CGCS2000",6378137,298.257222101,'
        'AUTHORITY["EPSG","1024"]],AUTHORITY["EPSG","1043"]],'
        'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        'AUTHORITY["EPSG","4490"]]'
    ),
}

# 按图幅分带动态生成 CGCS2000 6度带高斯-克吕格投影 WKT
# (大地基准 + 带号 → 中央经线 6*n-3, 东伪偏移 n*1e6+500000)
CGCS2000_DATUM = (
    'DATUM["China_2000",SPHEROID["CGCS2000",6378137,298.257222101,'
    'AUTHORITY["EPSG","1024"]],AUTHORITY["EPSG","1043"]]'
)


def build_gk_wkt(zone):
    """按 6 度带号生成 CGCS2000 高斯-克吕格投影 WKT。"""
    lon0 = 6 * zone - 3
    fe = zone * 1000000 + 500000
    return (
        f'PROJCS["CGCS2000 / Gauss-Kruger zone {zone}",'
        f'GEOGCS["China Geodetic Coordinate System 2000",{CGCS2000_DATUM},'
        f'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        f'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
        f'AUTHORITY["EPSG","4490"]],'
        f'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
        f'PARAMETER["central_meridian",{lon0}],PARAMETER["scale_factor",1],'
        f'PARAMETER["false_easting",{fe}],PARAMETER["false_northing",0],'
        f'UNIT["metre",1,AUTHORITY["EPSG","9001"]]]'
    )


CRS_WKT['EPSG:4498'] = build_gk_wkt(20)


# ============================================================
# 基础 IO
# ============================================================

def read_lines(path):
    """读取 GJB5068 文本文件，按 GBK 编码解码(兼容 CRLF/LF)。"""
    with open(path, 'rb') as f:
        raw = f.read()
    return raw.decode('gbk', errors='replace').splitlines()


def check_header(lines, path):
    """校验统一文件头(6 行)。"""
    if len(lines) < HEADER_LINES:
        raise ValueError(f'{path} 内容不完整')
    if lines[4] != 'GJB5068-2004':
        raise ValueError(f'{path} 缺少 GJB5068-2004 格式标识(第5行: {lines[4]!r})')


def iter_layer_sections(lines, start=HEADER_LINES):
    """迭代 "类型 数量" 图层头。

    每个元素为 (gtype, count, begin, end): begin 为首个要素行下标, end 为下一图层头行下标(或 EOF)。
    """
    idx = start
    while idx < len(lines):
        parts = lines[idx].split()
        if len(parts) == 2 and parts[0] in ZB_TYPES and parts[1].isdigit():
            gtype, count = parts[0], int(parts[1])
            begin = idx + 1
            # 扫描到下一图层头(或 EOF)，要素行首列均为数字，不会误判
            idx = begin
            while idx < len(lines):
                p = lines[idx].split()
                if len(p) == 2 and p[0] in ZB_TYPES and p[1].isdigit():
                    break
                idx += 1
            yield gtype, count, begin, idx
        else:
            raise ValueError(f'无法识别的图层头(行 {idx + 1}): {lines[idx]!r}')


# ============================================================
# .SMS 图幅元数据
# ============================================================

def parse_sms(path):
    """解析 .SMS 图幅元数据，返回 {字段名: 值} 字典。"""
    lines = read_lines(path)
    meta = {}
    # 每行格式: 行号 + 右对齐字段名 + 值
    for line in lines:
        m = re.match(r'^\s*\d+\s+(\S+)\s+(.*?)\s*$', line)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta


def sms_summary(meta):
    """从图幅元数据提取关键信息。"""
    if not meta:
        return None
    keys = ['图名', '图号', '地图比例尺分母', '大地基准', '地图投影',
            '分带方式', '高斯投影带号', '中央经线', '坐标放大系数',
            '相对原点横坐标', '相对原点纵坐标']
    return {k: meta.get(k, '') for k in keys}


# ============================================================
# .XMS 主索引
# ============================================================

def parse_ms(path):
    """解析 .XMS 主索引，返回 (图层组名, 组成文件列表, {类型: 数量})。"""
    lines = read_lines(path)
    check_header(lines, path)
    idx = HEADER_LINES
    group_name = lines[idx].strip()
    idx += 1
    nfiles = int(lines[idx].split()[0])
    idx += 1
    files = [lines[idx + i].strip() for i in range(nfiles)]
    idx += nfiles
    counts = {}
    for gtype in ZB_TYPES:
        parts = lines[idx].split()
        counts[gtype] = int(parts[0]) if parts else 0
        idx += 1
    return group_name, files, counts


# ============================================================
# .XSX 层索引
# ============================================================

def parse_sx(path):
    """解析 .XSX 层索引，返回 {几何类型: [属性字典, ...]}。

    通用属性: seq 序号, code 编码, name 名称, note 说明, gid 标识码,
              extra 组特定属性, gtype 几何类型码, ptr 属性指针, tail 尾值
    注记(N)属性: seq, code, text 文本, font 字体, style 字形, size 字号, color 颜色
    """
    lines = read_lines(path)
    check_header(lines, path)
    result = {}
    for gtype, count, begin, end in iter_layer_sections(lines):
        attrs = []
        for line in lines[begin:end]:
            if not line.strip():
                continue
            parts = line.split()
            if not parts:
                continue
            rec = {'seq': int(parts[0]), 'code': parts[1] if len(parts) > 1 else ''}
            if gtype == 'N':
                # 注记行: 序号 编码 文本 字体 字形 字号 0 颜色
                rec['text'] = parts[2] if len(parts) > 2 else ''
                rec['font'] = parts[3] if len(parts) > 3 else ''
                rec['style'] = parts[4] if len(parts) > 4 else ''
                rec['size'] = float(parts[5]) if len(parts) > 5 else 0.0
                rec['color'] = parts[-1] if len(parts) > 6 else ''
                rec['extra'] = ' '.join(parts[6:-1])
                rec['gtype'] = 'N'
                rec['ptr'] = 0
            else:
                # 通用行: 序号 编码 名称 说明 [组特定属性...] 类型码 指针 尾值
                mid = parts[2:-3] if len(parts) >= 6 else parts[2:]
                rec['name'] = mid[0] if mid else ''
                rec['note'] = mid[1] if len(mid) > 1 else ''
                extras = mid[2:]
                rec['gtype'] = parts[-3] if len(parts) >= 3 else ''
                rec['ptr'] = int(parts[-2]) if len(parts) >= 2 else 0
                rec['tail'] = parts[-1] if parts else ''
                # 从组特定属性中提取 12 位要素标识码
                gid = ''
                for tok in extras:
                    if re.fullmatch(r'\d{12,}', tok):
                        gid = tok
                        break
                rec['gid'] = gid
                rec['extra'] = ' '.join(t for t in extras if t != gid)
            attrs.append(rec)
        if len(attrs) != count:
            print(f'  [警告] {os.path.basename(path)} {gtype} 图层索引 {len(attrs)} 行'
                  f' 与 MS 声明数量 {count} 不一致')
        result[gtype] = attrs
    return result


# ============================================================
# .XTP 属性表
# ============================================================

def parse_tp(path):
    """解析 .XTP 属性表，返回 {几何类型: [指针, ...]}。"""
    lines = read_lines(path)
    check_header(lines, path)
    result = {}
    for gtype, count, begin, end in iter_layer_sections(lines):
        ptrs = []
        for line in lines[begin:end]:
            parts = line.split()
            if len(parts) >= 2:
                ptrs.append(int(parts[1]))
            else:
                ptrs.append(0)
        result[gtype] = ptrs
    return result


# ============================================================
# .XZB 坐标/几何数据
# ============================================================

def _read_coord_lines(lines, idx, nvals):
    """连续读取坐标行，累计 nvals 个浮点数，返回 (坐标列表, 下一个行下标)。"""
    vals = []
    while len(vals) < nvals and idx < len(lines):
        row = lines[idx].split()
        vals.extend(float(v) for v in row)
        idx += 1
    if len(vals) < nvals:
        raise ValueError(f'坐标数据不足: 需要 {nvals} 个数，实际 {len(vals)} 个')
    return vals, idx


def parse_zb(path):
    """解析 .XZB 几何数据，返回 {几何类型: [[[x,y], ...], ...]}。

    P 要素: 首对坐标作为点位，其余非零坐标对视为方向点(附加点)。
    L 要素: 折线顶点列表。
    A 要素: 环列表，首环为外环，其余为岛(洞)。
    N 要素: 首对坐标为注记锚点，其余为注记延伸点。
    """
    lines = read_lines(path)
    check_header(lines, path)
    result = {}
    for gtype, count, begin, end in iter_layer_sections(lines):
        feats = []
        idx = begin
        for _ in range(count):
            if idx >= len(lines):
                raise ValueError(f'{path} {gtype} 图层要素数量不足')
            parts = lines[idx].split()
            idx += 1
            if gtype == 'P':
                # 序号 X Y [Z M | X2 Y2 ...]
                vals = [float(v) for v in parts[1:]]
                if len(vals) < 2:
                    raise ValueError(f'P 要素坐标不足: {parts!r}')
                pts = [[vals[i], vals[i + 1]] for i in range(0, len(vals) - 1, 2)]
                # (0,0) 为高程/量测占位，非实际点
                real = [p for p in pts if p != [0.0, 0.0]]
                feats.append({'anchor': real[0] if real else [vals[0], vals[1]],
                              'extra': real[1:]})
            elif gtype == 'L':
                # 序号 顶点数，后跟坐标行
                npts = int(parts[1])
                vals, idx = _read_coord_lines(lines, idx, npts * 2)
                feats.append({'coords': [[vals[i], vals[i + 1]]
                                         for i in range(0, len(vals), 2)]})
            elif gtype == 'A':
                try:
                    npts = int(parts[4]) if len(parts) > 4 else 0
                    rings = []
                    if npts > 0:
                        vals, idx = _read_coord_lines(lines, idx, npts * 2)
                        rings = [[[vals[i], vals[i + 1]] for i in range(0, len(vals), 2)]]
                    anchor = [float(parts[1]), float(parts[2])] if len(parts) >= 3 else [0.0, 0.0]
                    feats.append({'rings': rings, 'anchor': anchor})   # 空要素也产出，计数对齐
                except Exception:
                    feats.append({'rings': [], 'anchor': [0.0, 0.0]})
            else:  # N 注记：序号 X1 Y1 X2 Y2 <点数> [坐标...] 都在同一行
                rest = [float(v) for v in parts[6:]]
                pts = [[rest[i], rest[i + 1]] for i in range(0, len(rest) - 1, 2)]
                anchor = [float(parts[1]), float(parts[2])]
                extra = [[float(parts[3]), float(parts[4])]] + pts
                feats.append({'anchor': anchor, 'extra': extra})
        result[gtype] = feats
    return result


# ============================================================
# 坐标系与坐标换算
# ============================================================

def resolve_crs(sms_meta, crs_arg):
    """确定输出坐标系: --crs 优先，否则按 SMS 元数据自动生成，缺省 EPSG:4498。"""
    if crs_arg:
        return crs_arg
    meta = sms_meta or {}
    zone = meta.get('高斯投影带号', '').strip()
    if zone.isdigit() and 1 <= int(zone) <= 60:
        return build_gk_wkt(int(zone))
    unit = meta.get('坐标单位', '').strip()
    projection = meta.get('地图投影', '').strip()
    datum = meta.get('大地基准', '').strip()
    if unit in ('度', '经纬度') or projection in ('无投影', '地理坐标', '经纬度'):
        if '2000' in datum or 'CGCS' in datum.upper():
            return 'EPSG:4490'
        print('[提示] SMS 表明数据为经纬度坐标，使用 EPSG:4326')
        return 'EPSG:4326'
    print('[提示] 未在 .SMS 中找到投影带号，默认使用 EPSG:4498 (CGCS2000 / GK zone 20)')
    return 'EPSG:4498'


def resolve_offset(sms_meta):
    """从 SMS 元数据提取相对原点与放大系数，缺省 (0, 0, 1)。"""
    if not sms_meta:
        return 0.0, 0.0, 1.0
    try:
        ox = float(sms_meta.get('相对原点横坐标', '0') or 0)
        oy = float(sms_meta.get('相对原点纵坐标', '0') or 0)
        scale = float(sms_meta.get('坐标放大系数', '1') or 1)
    except ValueError:
        print('[警告] 相对原点/放大系数解析失败，使用原始坐标')
        return 0.0, 0.0, 1.0
    # GJB 文件常用 -32767/-32768 表示字段无数据，不能参与坐标换算。
    if ox <= -32000:
        ox = 0.0
    if oy <= -32000:
        oy = 0.0
    if scale <= 0 or scale <= -32000:
        scale = 1.0
    return ox, oy, scale


def to_abs(coords, ox, oy, scale):
    """相对坐标 → 绝对高斯-克吕格坐标。"""
    return [[ox + x * scale, oy + y * scale] for x, y in coords]


# ============================================================
# Shapefile 输出
# ============================================================

# DBF 字段定义: (字段名, 类型, 长度(字节), 小数位)
# DBF 单个字段最大 254 字节；中文按 UTF-8 每字 3 字节估算
COMMON_FIELDS = [
    ('SEQ', 'N', 10, 0),
    ('CODE', 'C', 20, 0),
    ('NAME', 'C', 250, 0),
    ('NOTE', 'C', 60, 0),
    ('GID', 'C', 20, 0),
    ('EXTRA', 'C', 250, 0),
    ('TYPE', 'C', 6, 0),
    ('PTR', 'N', 12, 0),
    ('LAYER', 'C', 40, 0),
    ('GROUP', 'C', 2, 0),
]
NOTE_FIELDS = [
    ('TEXT', 'C', 250, 0),
    ('FONT', 'C', 30, 0),
    ('STYLE', 'C', 10, 0),
    ('SIZE', 'N', 12, 2),
    ('COLOR', 'C', 10, 0),
]

GEOM_TYPE = {
    'P': shapefile.POINT,
    'L': shapefile.POLYLINE,
    'A': shapefile.POLYGON,
    'N': shapefile.POINT,
}


def _add_fields(w, gtype, with_group):
    for name, ftype, size, dec in COMMON_FIELDS:
        if name == 'GROUP' and not with_group:
            continue
        w.field(name, ftype, size, dec)
    if gtype == 'N':
        for name, ftype, size, dec in NOTE_FIELDS:
            w.field(name, ftype, size, dec)


def _record_values(rec, gtype, group, layer_name, with_group):
    """将属性字典转换为按字段顺序的值列表。"""
    vals = [
        rec.get('seq', 0),
        rec.get('code', ''),
        rec.get('name', '') if gtype != 'N' else rec.get('text', ''),
        rec.get('note', '') if gtype != 'N' else '',
        rec.get('gid', ''),
        rec.get('extra', ''),
        rec.get('gtype', ''),
        rec.get('ptr', 0),
        layer_name[:13],
    ]
    if with_group:
        vals.append(group)
    if gtype == 'N':
        vals += [rec.get('text', ''), rec.get('font', ''),
                 rec.get('style', ''), rec.get('size', 0.0),
                 rec.get('color', '')]
    return vals


def _write_geometry(w, gtype, geom, ox, oy, scale):
    """将单个要素几何写入 Writer，返回是否成功。"""
    if gtype == 'P':
        x, y = to_abs([geom['anchor']], ox, oy, scale)[0]
        w.point(x, y)
        return True
    if gtype == 'L':
        coords = to_abs(geom['coords'], ox, oy, scale)
        if len(coords) < 2:
            return False
        w.line([coords])
        return True
    if gtype == 'A':
        rings = [ensure_closed(to_abs(r, ox, oy, scale)) for r in geom['rings']]
        rings = [r for r in rings if len(r) >= 4 and abs(ring_area(r)) > 1e-12]
        if not rings:
            return False
        if ring_area(rings[0]) > 0:
            rings[0] = list(reversed(rings[0]))
        for index in range(1, len(rings)):
            if ring_area(rings[index]) < 0:
                rings[index] = list(reversed(rings[index]))
        w.poly(rings)
        return True
    # N 注记
    x, y = to_abs([geom['anchor']], ox, oy, scale)[0]
    w.point(x, y)
    return True


def ensure_closed(coords):
    """确保 ring 首尾闭合(shapefile 要求多边形 ring 闭合)。"""
    if len(coords) < 2:
        return coords
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return coords


def ring_area(coords):
    """计算闭合环有符号面积，用于过滤零面积退化环。"""
    if len(coords) < 4:
        return 0.0
    return sum(coords[i][0] * coords[i + 1][1] - coords[i + 1][0] * coords[i][1]
               for i in range(len(coords) - 1)) / 2.0


def write_prj(out_base, crs):
    """写出坐标系 .prj 文件。"""
    prj = CRS_WKT.get(crs, crs)
    with open(out_base + '.prj', 'w', encoding='utf-8') as f:
        f.write(prj)
    # DBF 字符编码声明
    with open(out_base + '.cpg', 'w', encoding='utf-8') as f:
        f.write('UTF-8')


def write_layer_shapefile(group_letter, layer_name, gtype, attrs, geoms,
                          out_base, crs, ox, oy, scale):
    """写出单个图层的 shapefile(attrs 与 geoms 按序号对齐)。"""
    w = shapefile.Writer(out_base, shapeType=GEOM_TYPE[gtype], encoding='utf-8')
    _add_fields(w, gtype, with_group=False)
    written = 0
    for i, geom in enumerate(geoms):
        rec = attrs[i] if i < len(attrs) else {'seq': i + 1}
        if _write_geometry(w, gtype, geom, ox, oy, scale):
            w.record(*_record_values(rec, gtype, group_letter, layer_name, False))
            written += 1
    w.close()
    write_prj(out_base, crs)
    return written


def write_merged_shapefile(gtype, groups, out_base, crs, ox, oy, scale):
    """将多个图层组的同类型几何合并写为一个 shapefile。"""
    w = shapefile.Writer(out_base, shapeType=GEOM_TYPE[gtype], encoding='utf-8')
    _add_fields(w, gtype, with_group=True)
    written = 0
    for letter, layer_name, attrs, geoms in groups:
        for i, geom in enumerate(geoms):
            rec = attrs[i] if i < len(attrs) else {'seq': i + 1}
            if _write_geometry(w, gtype, geom, ox, oy, scale):
                w.record(*_record_values(rec, gtype, letter, layer_name, True))
                written += 1
    w.close()
    write_prj(out_base, crs)
    return written


# ============================================================
# 文件组定位
# ============================================================

def find_group_files(input_path):
    """根据输入(目录或任一文件)定位文件组。

    返回 (图幅前缀, [(图层组字母, XMS路径, XSX路径, XTP路径, XZB路径), ...], SMS路径)
    输入为具体文件时仅返回该文件所属的图层组(传入 .SMS 时返回全部)。
    """
    if os.path.isdir(input_path):
        base_dir = input_path
        stem = None
        only_letter = None
    else:
        base_dir = os.path.dirname(os.path.abspath(input_path))
        # 从文件名提取图幅前缀与图层组字母: zone.KZB -> zone, K
        # 组文件名形如 .XMS/.XSX/.XTP/.XZB，扩展名第 2 个字符即图层组字母
        fname = os.path.basename(input_path)
        stem, ext = os.path.splitext(fname)
        ext = ext.upper()
        only_letter = ext[1] if len(ext) == 4 and ext[1] in GROUP_LETTERS else None

    # 列出目录中的 GJB 文件(键为完整文件名，如 zone.AMS)
    all_files = {
        os.path.basename(p): p
        for p in glob.glob(os.path.join(base_dir, '*'))
        if os.path.isfile(p)
    }

    # 若未指定文件，则用任一图层组 .XMS 主索引的前缀(扩展名 3 字符，无点前缀)
    if stem is None:
        for name in sorted(all_files):
            ext = os.path.splitext(name)[1].upper()  # 如 .AMS / .BMS / .SMS
            if ext.endswith('MS') and ext != '.SMS':
                stem = os.path.splitext(name)[0]
                break
    if stem is None:
        print('错误：目录中未找到图层组 .MS 主索引文件')
        sys.exit(1)

    # 定位 SMS 与各图层组(传入具体文件时只取该组)
    sms_path = all_files.get(f'{stem}.SMS')
    letters = [only_letter] if only_letter else list(GROUP_LETTERS)
    groups = []
    for letter in letters:
        files = {ext: all_files.get(f'{stem}.{letter}{ext}')
                 for ext in ('MS', 'SX', 'TP', 'ZB')}
        # 图层组至少要有 MS 主索引
        if not files['MS']:
            continue
        groups.append((letter, files['MS'], files['SX'], files['TP'], files['ZB']))
    if not groups:
        print(f'错误：未找到以 {stem} 为前缀的图层组文件')
        sys.exit(1)
    return stem, groups, sms_path


# ============================================================
# 主流程
# ============================================================

def main():
    # 统一 stdout/stderr 为 UTF-8，避免 Windows 控制台下中文输出乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description='GJB5068-2004 矢量数据 → Shapefile 转换工具'
    )
    parser.add_argument('input', help='GJB5068 文件所在目录或文件组中任一文件')
    parser.add_argument('--outdir', default='shp_output', help='输出目录(默认 shp_output)')
    parser.add_argument('--crs', default=None,
                        help='坐标系 EPSG 代码或 WKT；缺省按 .SMS 图幅分带自动生成')
    parser.add_argument('--merge', action='store_true',
                        help='额外按几何类型合并所有图层组(带 GROUP 字段)')
    args = parser.parse_args()

    stem, groups, sms_path = find_group_files(args.input)

    # 1. 图幅元数据
    sms_meta = parse_sms(sms_path) if sms_path else {}
    summary = sms_summary(sms_meta)
    if summary:
        print('=' * 66)
        print('图幅元数据 (.SMS)')
        print('=' * 66)
        for k, v in summary.items():
            if v:
                print(f'  {k}: {v}')
        print()

    crs = resolve_crs(sms_meta, args.crs)
    ox, oy, scale = resolve_offset(sms_meta)
    print(f'输出坐标系: {"(自动生成)" if not args.crs else ""} {crs[:60]}{"..." if len(crs) > 60 else ""}')
    print(f'相对原点: ({ox:.3f}, {oy:.3f})  放大系数: {scale}')
    print()

    os.makedirs(args.outdir, exist_ok=True)

    # 2. 逐图层组解析并输出
    parsed = []  # (letter, layer_name, {gtype: attrs}, {gtype: geoms})
    print('=' * 66)
    print(f'图层组解析 ({len(groups)} 组)')
    print('=' * 66)
    for letter, ms_path, sx_path, tp_path, zb_path in groups:
        group_name, _, counts = parse_ms(ms_path)
        attrs = parse_sx(sx_path) if sx_path else {}
        geoms = parse_zb(zb_path) if zb_path else {}
        # 解析 .XTP 属性表以校验其结构(属性记录指针，本例中几乎全为 0)
        if tp_path:
            parse_tp(tp_path)

        print(f'  [{letter}] {group_name}')
        for gtype in ZB_TYPES:
            nattr = len(attrs.get(gtype, []))
            ngeom = len(geoms.get(gtype, []))
            status = ''
            if ngeom:
                # 属性与几何数量一致性检查
                if nattr and nattr != ngeom:
                    status = '  [警告] 属性/几何数量不一致'
                out_base = os.path.join(args.outdir, f'{stem}_{letter}_{gtype}')
                written = write_layer_shapefile(
                    letter, group_name, gtype,
                    attrs.get(gtype, []), geoms.get(gtype, []),
                    out_base, crs, ox, oy, scale)
                print(f'    {gtype} 要素 {ngeom:6d} -> {os.path.basename(out_base)}.shp'
                      f' (写出 {written}){status}')
            elif counts.get(gtype):
                print(f'    {gtype} 声明 {counts[gtype]} 个要素但无几何数据')
        parsed.append((letter, group_name, attrs, geoms))

    # 3. 合并输出
    if args.merge:
        print()
        for gtype in ZB_TYPES:
            group_items = [(letter, name, attrs.get(gtype, []), geoms.get(gtype, []))
                           for letter, name, attrs, geoms in parsed
                           if geoms.get(gtype)]
            if not group_items:
                continue
            out_base = os.path.join(args.outdir, f'{stem}_all_{gtype}')
            written = write_merged_shapefile(
                gtype, group_items, out_base, crs, ox, oy, scale)
            print(f'  合并 {gtype}: {written} 个要素 -> {os.path.basename(out_base)}.shp')

    print()
    print(f'完成，输出目录：{os.path.abspath(args.outdir)}')


if __name__ == '__main__':
    main()
