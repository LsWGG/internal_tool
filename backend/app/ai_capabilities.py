"""Declarative capability registry used by the system AI assistant.

The registry is deliberately independent from FastAPI and the task managers.  It
describes what the portal can do; execution adapters remain in ``main.py`` where
the already-created manager instances live.
"""
import math
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse


def _object(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TASK_ID = _object({"task_id": {"type": "string", "minLength": 8}}, ("task_id",))
TASK_CONTROL = _object({
    "task_id": {"type": "string", "minLength": 8},
    "action": {"type": "string", "enum": ["pause", "resume", "retry", "delete"]},
}, ("task_id", "action"))

MAP_PARAMETERS = {
    "region_query": {"type": "string", "minLength": 2, "maxLength": 160, "description": "完整行政区名称"},
    "data_type": {"type": "string", "enum": ["satellite", "dem"]},
    "tile_source": {"type": "string", "enum": ["google_satellite", "google_hybrid", "bing_aerial", "amap_satellite", "amap_hybrid", "amap_standard", "osm_standard", "opentopomap", "aws_terrarium"]},
    "zoom_min": {"type": "integer", "minimum": 0, "maximum": 20},
    "zoom_max": {"type": "integer", "minimum": 0, "maximum": 20},
    "output_format": {"type": "string", "enum": ["png", "tif"]},
    "png_width": {"type": "integer", "minimum": 0, "maximum": 8192, "description": "PNG 自定义宽度。与 png_height 同时为 0 时保存原始 z/x/y 瓦片。"},
    "png_height": {"type": "integer", "minimum": 0, "maximum": 8192, "description": "PNG 自定义高度。必须与宽度同时填写或同时留空。"},
    "download_path": {"type": "string", "maxLength": 4096, "description": "服务器上已挂载目录的绝对路径。留空则使用工具本地目录。"},
    "max_workers": {"type": "integer", "minimum": 1, "maximum": 16},
    "pack_mbtiles": {"type": "boolean", "description": "PNG 导出时额外写入 MBTiles。已写入的瓦片会跳过。"},
}

CRAWLER_PARAMETERS = {
    "source": {"type": "string", "enum": ["generic", "news", "twitter", "tiktok", "telegram", "youtube", "wechat", "ytdlp"], "description": "采集流程类型。generic 仅适合逐个读取用户明确给出的独立页面；news 适合任何具有列表、排行、分类、分页并需进入详情页的网站，不限于新闻内容；ytdlp 用于下载视频（任意 yt-dlp 支持的站点链接或播放列表），urls 里放视频/合集链接。"},
    "urls": {"type": "array", "items": {"type": "string", "format": "uri"}, "maxItems": 500},
    "keyword": {"type": "string", "maxLength": 1000},
    "account_name": {"type": "string", "maxLength": 300},
    "fields": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 50},
    "twitter_mode": {"type": "string", "enum": ["user", "history", "keyword"]},
    "tiktok_mode": {"type": "string", "enum": ["keyword", "comments", "user", "videos"]},
    "telegram_mode": {"type": "string", "enum": ["channel", "group", "members", "search"]},
    "ytdlp_mode": {"type": "string", "enum": ["videos", "playlist"], "description": "ytdlp 源的输入形态：videos 是逐条视频链接，playlist 是播放列表/合集/频道链接。"},
    "max_items": {"type": "integer", "minimum": -1, "maximum": 500},
    "output_format": {"type": "string", "enum": ["xlsx", "csv", "json", "jsonl", "postgresql"]},
    "download_videos": {"type": "boolean", "description": "是否下载视频/音频文件并与数据表一起打包为 ZIP。"},
    "quality": {"type": "integer", "enum": [360, 720, 1080]},
    "download_mode": {"type": "string", "enum": ["video", "audio_mp3", "audio_m4a"], "description": "下载内容：video 是视频，audio_mp3 / audio_m4a 是仅提取音频（需要 ffmpeg）。"},
    "subtitle_mode": {"type": "string", "enum": ["none", "sidecar", "embed"], "description": "字幕：none 不要，sidecar 存成 .vtt 单独文件，embed 嵌入视频（需要 ffmpeg，MP3 不支持）。"},
    "thumbnail_mode": {"type": "string", "enum": ["none", "sidecar", "embed"], "description": "封面：none 不要，sidecar 存成单独图片，embed 嵌入文件（需要 ffmpeg）。"},
    "write_metadata": {"type": "boolean", "description": "把标题、作者、上传日期写进文件元数据（需要 ffmpeg）。"},
    "skip_downloaded": {"type": "boolean", "description": "跳过已下载过的视频，避免定时任务重复下载同一条。"},
    "interval": {"type": "integer", "enum": [-1, 0, 60, 360, 1440]},
    "cron": {"type": "string", "maxLength": 120},
}

# Generic calls remain a stable six-function API.  This union only helps models
# that cannot infer keys from a free-form nested object; the selected tool's
# manifest still performs the authoritative validation afterwards.
GENERIC_PARAMETER_PROPERTIES = {
    **MAP_PARAMETERS,
    "urls": {"type": "array", "items": {"type": "string", "format": "uri"}, "minItems": 1, "maxItems": 200},
    "filename_template": {"type": "string", "maxLength": 160},
    **CRAWLER_PARAMETERS,
    "output_format": {"type": "string", "enum": ["png", "tif", "xlsx", "csv", "json", "jsonl", "postgresql"]},
}


def _manual(tool_id, name, description, *, uploads=False, credentials=False):
    reason = "需要在工具页补充"
    if uploads:
        reason += "本地文件"
    if uploads and credentials:
        reason += "和"
    if credentials:
        reason += "连接凭据"
    return {
        "id": tool_id, "name": name, "description": description,
        "actions": {
            "navigate": {"confirmation": "none"},
            "query": {"schema": _object({"task_id": {"type": "string", "minLength": 8}}), "confirmation": "none", "adapter": f"{tool_id}.query", "label": "查看工具状态"},
        },
        "manual_reason": reason + "后执行" if uploads or credentials else "请在工具页完成配置",
    }


CAPABILITIES = {
    "map": {
        "id": "map", "name": "卫星地图下载", "description": "按行政区下载影像或 DEM，输出 PNG 瓦片或 GeoTIFF。PNG 影像可以同时打包为 MBTiles。",
        "constraints": ["DEM 仅使用 aws_terrarium、仅输出 tif、最高 Z15", "高德最高 Z18", "其他影像最高 Z20"],
        "actions": {
            "navigate": {"confirmation": "none"},
            "configure": {"schema": _object(MAP_PARAMETERS), "confirmation": "none"},
            "create": {"schema": _object(MAP_PARAMETERS, ("region_query", "data_type", "tile_source", "zoom_min", "zoom_max", "output_format")), "confirmation": "required", "adapter": "map.create", "label": "确认并开始下载"},
            "control": {"schema": TASK_CONTROL, "confirmation": "required", "adapter": "map.control", "label": "确认任务操作"},
            "query": {"schema": _object({"task_id": {"type": "string", "minLength": 8}}), "confirmation": "none", "adapter": "map.query", "label": "查询任务"},
            "export": {"schema": TASK_ID, "confirmation": "none", "adapter": "map.export", "label": "获取下载文件"},
        },
    },
    "tile-viewer": _manual("tile-viewer", "地图瓦片查看", "加载在线 XYZ 链接或 z/x/y 瓦片目录，打包或拆包 MBTiles，并在 XYZ、TMS、WMTS 之间转换。", uploads=True),
    "raster-clip": {
        "id": "raster-clip", "name": "栅格裁切", "description": "用 Shapefile 或框选范围裁切 GeoTIFF、DEM 和瓦片目录。可以填写服务器路径，也可以在页面上传目录。",
        "constraints": ["创建任务只能使用服务器上的绝对路径；浏览器目录上传要在页面操作", "Shapefile 优先于经纬度范围", "瓦片目录不能在原位置裁切", "只有单个 GeoTIFF 可以写在原文件旁边，且不覆盖源文件"],
        "actions": {
            "navigate": {"confirmation": "none"},
            "configure": {"schema": _object({"source_path": {"type": "string", "maxLength": 4096}, "shp_path": {"type": "string", "maxLength": 4096}, "output_path": {"type": "string", "maxLength": 4096}, "in_place": {"type": "boolean"}, "west": {"type": "number"}, "south": {"type": "number"}, "east": {"type": "number"}, "north": {"type": "number"}}), "confirmation": "none"},
            "create": {"schema": _object({"source_path": {"type": "string", "maxLength": 4096}, "shp_path": {"type": "string", "maxLength": 4096}, "output_path": {"type": "string", "maxLength": 4096}, "in_place": {"type": "boolean"}, "west": {"type": "number"}, "south": {"type": "number"}, "east": {"type": "number"}, "north": {"type": "number"}}, ("source_path",)), "confirmation": "required", "adapter": "raster-clip.create", "label": "确认并开始裁切"},
            "control": {"schema": TASK_CONTROL, "confirmation": "required", "adapter": "raster-clip.control", "label": "确认任务操作"},
            "query": {"schema": _object({"task_id": {"type": "string", "minLength": 8}}), "confirmation": "none", "adapter": "raster-clip.query", "label": "查询任务"},
            "export": {"schema": TASK_ID, "confirmation": "none", "adapter": "raster-clip.export", "label": "获取下载文件"},
        },
    },
    "vector": {
        "id": "vector", "name": "矢量格式转换", "description": "GeoJSON、KML、GPKG、DXF 与 SHP 互转。可以填写服务器目录，也可以在页面上传一个目录。坐标系自动识别，SHP 结果可在现有预览里打开。",
        "constraints": ["创建任务只能使用服务器上的绝对路径；浏览器目录上传要在页面操作", "一次只转换一个目录，不进入子目录", "读不到坐标系时失败，提示「无法识别坐标系」", "KML 写出为 EPSG:4326", "DXF 可能丢掉属性", "不能覆盖源文件"],
        "actions": {
            "navigate": {"confirmation": "none"},
            "configure": {"schema": _object({"source_path": {"type": "string", "maxLength": 4096}, "output_path": {"type": "string", "maxLength": 4096}, "in_place": {"type": "boolean"}, "target_format": {"type": "string", "enum": ["shp", "geojson", "kml", "gpkg", "dxf"]}}), "confirmation": "none"},
            "create": {"schema": _object({"source_path": {"type": "string", "maxLength": 4096}, "output_path": {"type": "string", "maxLength": 4096}, "in_place": {"type": "boolean"}, "target_format": {"type": "string", "enum": ["shp", "geojson", "kml", "gpkg", "dxf"]}}, ("source_path", "target_format")), "confirmation": "required", "adapter": "vector.create", "label": "确认并开始转换"},
            "control": {"schema": TASK_CONTROL, "confirmation": "required", "adapter": "vector.control", "label": "确认任务操作"},
            "query": {"schema": _object({"task_id": {"type": "string", "minLength": 8}}), "confirmation": "none", "adapter": "vector.query", "label": "查询任务"},
            "export": {"schema": TASK_ID, "confirmation": "none", "adapter": "vector.export", "label": "获取下载文件"},
        },
    },
    "pdf": {
        "id": "pdf", "name": "网页转 PDF", "description": "将一个或多个公开网页转换为 A4 PDF。",
        "actions": {
            "navigate": {"confirmation": "none"},
            "configure": {"schema": _object({"urls": {"type": "array", "items": {"type": "string", "format": "uri"}, "minItems": 1, "maxItems": 200}, "filename_template": {"type": "string", "maxLength": 160}}), "confirmation": "none"},
            "create": {"schema": _object({"urls": {"type": "array", "items": {"type": "string", "format": "uri"}, "minItems": 1, "maxItems": 200}, "filename_template": {"type": "string", "maxLength": 160}}, ("urls",)), "confirmation": "required", "adapter": "pdf.create", "label": "确认并开始转换"},
            "control": {"schema": _object({"task_id": {"type": "string", "minLength": 8}, "action": {"type": "string", "enum": ["retry", "delete"]}}, ("task_id", "action")), "confirmation": "required", "adapter": "pdf.control", "label": "确认任务操作"},
            "query": {"schema": _object({"task_id": {"type": "string", "minLength": 8}}), "confirmation": "none", "adapter": "pdf.query", "label": "查询任务"},
            "export": {"schema": TASK_ID, "confirmation": "none", "adapter": "pdf.export", "label": "获取下载文件"},
        },
    },
    "crawler": {
        "id": "crawler", "name": "智能网页采集", "description": "配置公开网页、分页列表/排行榜和社交数据采集。generic 逐个读取明确页面；news 会从列表分页发现详情，亦用于电影、图书、商品等排行榜或目录。",
        "constraints": ["单个列表入口需要发现多条详情时必须使用 source=news", "source=generic 不会根据 max_items 自动发现更多页面", "排行榜应使用其列表首页 URL，不应把某一条详情页作为入口"],
        "actions": {"navigate": {"confirmation": "none"}, "configure": {"schema": _object(CRAWLER_PARAMETERS), "confirmation": "none"}, "create": {"schema": _object(CRAWLER_PARAMETERS, ("source",)), "confirmation": "required", "adapter": "crawler.create", "label": "确认并开始采集"}, "query": {"schema": _object({"task_id": {"type": "string", "minLength": 8}}), "confirmation": "none", "adapter": "crawler.query", "label": "查看采集任务"}},
        "manual_reason": "创建前需在页面核对采集目标、字段和数据源授权",
    },
    "propzone": _manual("propzone", "全美行政区域 SHP 下载", "选择美国州、县、城市及下载颗粒度。"),
    "gjb": _manual("gjb", "GJB 地图转 SHP", "上传完整 GJB 文件组并转换。", uploads=True),
    "shp": _manual("shp", "SHP 文件预览", "上传并叠加预览 Shapefile。", uploads=True),
    "database": _manual("database", "数据库在线操作", "PostgreSQL、SQLite、Redis 的 CRUD 与迁移。", uploads=True, credentials=True),
    "dbx": _manual("dbx", "DBX 数据库工作台", "打开随项目启动的本地 DBX，连接并管理数据库。", credentials=True),
    "es": _manual("es", "Elasticsearch 数据迁移", "ES 索引导入与导出。", uploads=True, credentials=True),
    "nebula": _manual("nebula", "NebulaGraph 数据迁移", "NebulaGraph Space 数据导入与导出。", uploads=True, credentials=True),
    "word-batch": _manual("word-batch", "Word 批量生成", "使用 Word 模板和 Excel 批量生成文档。", uploads=True),
    "md-word": _manual("md-word", "Markdown 转 Word", "将 Markdown 与 Mermaid 转为 Word。", uploads=True),
    "image-convert": _manual("image-convert", "图片格式转换", "批量转换 HEIC、JPEG、PNG、WebP、TIFF。", uploads=True),
    "image-metadata": _manual("image-metadata", "图片元数据", "查看、编辑及导出图片元数据。", uploads=True),
    "trending": {
        "id": "trending", "name": "GitHub 每日热门榜单", "description": "查看当天或近七天 GitHub 热门报告。",
        "actions": {"navigate": {"confirmation": "none"}, "query": {"schema": _object({"date": {"type": "string", "maxLength": 10}}), "confirmation": "none", "adapter": "trending.query", "label": "查看热门榜单"}},
    },
    "pdf-toolbox": _manual("pdf-toolbox", "PDF 工具箱", "合并、拆分、提取、旋转、压缩、水印、PDF 与 Word/图片互转、文本提取和多模态 OCR；需要在页面上传文件。", uploads=True),
    "json": _manual("json", "JSON 解析工具", "粘贴或上传 JSON，格式化、树形和表格视图、JSONPath 查询、格式转换及双栏差异对比。"),
    "ontology": _manual("ontology", "本体文件可视化", "导入 RDF/XML 本体文件，以图谱画布查看类、属性和实例关系。", uploads=True),
    "docker": _manual("docker", "Docker 离线包", "生成 Docker 与 Compose 离线安装包。"),
    "compose-manager": _manual("compose-manager", "Docker 服务管理", "管理本机 Docker Compose 文件，启动、停止、重启服务并查看日志。"),
}


JSON_PARAMETERS = {'format': {'type':'string','enum':['yaml','csv','jsonl','minify','json']}, 'text': {'type':'string','maxLength':20000}, 'mode': {'type':'string','enum':['formatted','tree','table','query','convert','diff']}, 'path': {'type':'string','maxLength':500}, 'comparison': {'type':'string','maxLength':20000}}
CAPABILITIES['json']['actions']['configure'] = {'schema':_object(JSON_PARAMETERS), 'confirmation':'none'}
GENERIC_PARAMETER_PROPERTIES.update(JSON_PARAMETERS)
GENERIC_PARAMETER_PROPERTIES.update({
    "source_path": {"type": "string", "maxLength": 4096, "description": "服务器上的绝对路径。浏览器目录上传请在页面操作。"},
    "shp_path": {"type": "string", "maxLength": 4096, "description": "裁切用 Shapefile 的绝对路径，优先于经纬度范围。"},
    "output_path": {"type": "string", "maxLength": 4096, "description": "输出目录的绝对路径。原位置处理时留空。"},
    "in_place": {"type": "boolean", "description": "栅格写在原 GeoTIFF 旁边，或矢量写回源目录。不覆盖源文件。"},
    "west": {"type": "number"}, "south": {"type": "number"}, "east": {"type": "number"}, "north": {"type": "number"},
    "target_format": {"type": "string", "enum": ["shp", "geojson", "kml", "gpkg", "dxf"], "description": "矢量转换的目标格式。"},
})

PAGE_PARAMETERS = _object({
    'revision': {'type':'string','minLength':1},
    'control_id': {'type':'string','minLength':1},
    'mode': {'type':'string','enum':['fill','click']},
    'value': {'type':'string','maxLength':20000},
}, ('revision','control_id','mode'))
for capability in CAPABILITIES.values():
    capability['actions']['page'] = {'schema': PAGE_PARAMETERS, 'confirmation':'required', 'label':'确认页面操作'}


GENERIC_FUNCTIONS = [
    {"type": "function", "function": {"name": "respond_to_user", "description": "无需打开工具、配置或执行操作时，直接回答用户的问题。", "parameters": _object({"answer": {"type": "string", "minLength": 1, "maxLength": 12000}, "suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 3}}, ("answer",))}},
    {"type": "function", "function": {"name": "navigate_tool", "description": "打开最适合用户需求的门户工具页面。仅在用户要求进入页面、需要上传文件或补充配置时使用。", "parameters": _object({"tool_id": {"type": "string", "enum": list(CAPABILITIES)}} , ("tool_id",))}},
    {"type": "function", "function": {"name": "configure_tool", "description": "将明确的配置值填入工具页面；支持地图、网页转 PDF、网页采集、JSON、栅格裁切和矢量格式转换。", "parameters": _object({"tool_id": {"type": "string", "enum": ["map", "pdf", "crawler", "json", "raster-clip", "vector"]}, "parameters": _object(GENERIC_PARAMETER_PROPERTIES)}, ("tool_id", "parameters"))}},
    {"type": "function", "function": {"name": "create_task", "description": "用户明确要求执行地图下载、网页转 PDF、公开数据采集、栅格裁切或矢量格式转换时使用。栅格和矢量需要服务器上的绝对路径；浏览器里的目录上传请让用户在页面操作。采集任务需要选择 crawler 并提供 source、目标 URL/关键词、数量和保存格式；系统会要求用户确认。榜单查看等只读请求绝不能使用。", "parameters": _object({"tool_id": {"type": "string", "enum": ["map", "pdf", "crawler", "raster-clip", "vector"]}, "parameters": _object(GENERIC_PARAMETER_PROPERTIES)}, ("tool_id", "parameters"))}},
    {"type": "function", "function": {"name": "control_task", "description": "暂停、继续、重试或删除地图、PDF、栅格裁切或矢量转换的已有任务。", "parameters": _object({"tool_id": {"type": "string", "enum": ["map", "pdf", "raster-clip", "vector"]}, "task_id": {"type": "string"}, "action": {"type": "string", "enum": ["pause", "resume", "retry", "delete"]}}, ("tool_id", "task_id", "action"))}},
    {"type": "function", "function": {"name": "query_tool", "description": "读取工具的真实数据或任务状态。用户询问榜单、任务、结果、进度或当前数据时使用；没有任务 ID 时返回最近记录。", "parameters": _object({"tool_id": {"type": "string", "enum": list(CAPABILITIES)}, "parameters": _object({"task_id": {"type": "string", "minLength": 8}, "date": {"type": "string", "maxLength": 10}})}, ("tool_id",))}},
    {"type": "function", "function": {"name": "export_result", "description": "获取地图、PDF、栅格裁切或矢量转换已完成任务的下载地址。结果在外接目录时没有压缩包。", "parameters": _object({"tool_id": {"type": "string", "enum": ["map", "pdf", "raster-clip", "vector"]}, "task_id": {"type": "string"}}, ("tool_id", "task_id"))}},
    {"type":"function","function":{"name":"operate_page","description":"操作当前工具页面真实控件。只允许使用页面快照中的 revision 与 control_id，每次填写一个字段或点击一个按钮；缺少文件须让用户上传。不得猜测控件 ID。","parameters":_object({'tool_id':{'type':'string','enum':list(CAPABILITIES)},'parameters':PAGE_PARAMETERS},('tool_id','parameters'))}},
]


def public_registry():
    return deepcopy(CAPABILITIES)


def prompt_catalog():
    result = {}
    for tool_id, manifest in CAPABILITIES.items():
        result[tool_id] = {
            "name": manifest["name"], "description": manifest["description"],
            "actions": {name: {key: value for key, value in spec.items() if key in ("schema", "confirmation")} for name, spec in manifest["actions"].items()},
        }
        if manifest.get("constraints"):
            result[tool_id]["constraints"] = manifest["constraints"]
        if manifest.get("manual_reason"):
            result[tool_id]["manual_reason"] = manifest["manual_reason"]
    return result


def _validate_value(value, schema, path):
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} 必须是对象")
        allowed = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError(f"缺少参数 {name}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(allowed)
            if extra:
                raise ValueError(f"不支持参数 {sorted(extra)[0]}")
        return {name: _validate_value(item, allowed[name], f"{path}.{name}") for name, item in value.items() if name in allowed}
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} 必须是数组")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise ValueError(f"{path} 数量不符合要求")
        return [_validate_value(item, schema.get("items", {}), f"{path}[]") for item in value]
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} 必须是文字")
        value = value.strip()
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 100000):
            raise ValueError(f"{path} 长度不符合要求")
        if schema.get("format") == "uri":
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError(f"{path} 不是有效的 HTTP/HTTPS 地址")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} 必须是整数")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ValueError(f"{path} 超出允许范围")
    elif kind == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} 必须是布尔值")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} 必须是数字")
        if not math.isfinite(float(value)):
            raise ValueError(f"{path} 超出允许范围")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 的值不受支持")
    return value


def _absolute_server_path(value, label):
    text = str(value or "").strip()
    if not text:
        return ""
    if any(char in text for char in ("\x00", "\n", "\r")):
        raise ValueError("路径无效")
    if not Path(text).expanduser().is_absolute():
        raise ValueError(f"{label}必须是服务器上的绝对路径")
    return text


def validate_action(operation, tool_id, parameters=None):
    if tool_id not in CAPABILITIES:
        raise ValueError("未知工具")
    action = CAPABILITIES[tool_id]["actions"].get(operation)
    if not action:
        reason = CAPABILITIES[tool_id].get("manual_reason", "当前操作不受支持")
        raise ValueError(f"{CAPABILITIES[tool_id]['name']}暂不支持由 AI 执行：{reason}")
    values = _validate_value(parameters or {}, action.get("schema", _object({})), "parameters") if operation != "navigate" else {}
    # Cross-field constraints belong to the capability rather than the LLM.
    if tool_id == "map" and operation in ("configure", "create"):
        if values.get("zoom_min", 0) > values.get("zoom_max", 20):
            raise ValueError("起始层级不能大于结束层级")
        source = values.get("tile_source")
        max_zoom = 15 if source == "aws_terrarium" else 17 if source == "opentopomap" else 19 if source == "osm_standard" else 18 if str(source).startswith("amap_") else 20
        min_zoom = 1 if source == "bing_aerial" or str(source).startswith("amap_") else 0
        if "zoom_min" in values and values["zoom_min"] < min_zoom or "zoom_max" in values and values["zoom_max"] > max_zoom:
            raise ValueError(f"所选数据源仅支持 Z{min_zoom}–Z{max_zoom}")
        if values.get("data_type") == "dem" and (source != "aws_terrarium" or values.get("output_format") not in (None, "tif")):
            raise ValueError("DEM 仅支持 AWS Terrarium 数据源和 GeoTIFF 输出")
        width = int(values.get("png_width") or 0)
        height = int(values.get("png_height") or 0)
        if (width == 0) != (height == 0):
            raise ValueError("PNG 自定义尺寸需要同时设置宽度和高度，或都留空以保存原始瓦片")
        if width and (width < 64 or height < 64):
            raise ValueError("PNG 自定义尺寸至少为 64 像素")
        download_path = str(values.get("download_path") or "").strip()
        if download_path and not Path(download_path).expanduser().is_absolute():
            raise ValueError("下载路径必须是服务器上的绝对路径")
        if values.get("pack_mbtiles") and (values.get("data_type") == "dem" or ("output_format" in values and values.get("output_format") != "png")):
            raise ValueError("只有 PNG 影像可以打包为 MBTiles")
    if tool_id == "crawler" and operation in ("configure", "create"):
        urls = values.get("urls") or []
        maximum = values.get("max_items")
        if operation == "create" and values.get("source") in ("generic", "news") and not urls:
            raise ValueError("网页和分页列表采集必须提供至少一个入口 URL")
        if values.get("source") == "generic" and maximum not in (None, -1) and maximum > len(urls) and len(urls) <= 1:
            raise ValueError("generic 只采集明确提供的独立页面，无法从单个入口发现多条记录；列表、排行、分类或分页任务应改用 source=news，并使用列表首页 URL")
    if tool_id in ("raster-clip", "vector") and operation in ("configure", "create"):
        _absolute_server_path(values.get("source_path"), "源路径")
        _absolute_server_path(values.get("shp_path"), "Shapefile")
        _absolute_server_path(values.get("output_path"), "输出路径")
        if values.get("in_place") and str(values.get("output_path") or "").strip():
            raise ValueError("原文件裁切时不需要另填输出路径" if tool_id == "raster-clip" else "原目录转换时不需要另填输出路径")
    if tool_id == "raster-clip" and operation in ("configure", "create"):
        bounds = [values.get(key) for key in ("west", "south", "east", "north")]
        present = [item is not None for item in bounds]
        if any(present) and not all(present):
            raise ValueError("范围需要同时填写西、南、东、北")
        if all(present) and (bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
            raise ValueError("范围坐标无效")
        if operation == "create" and not str(values.get("shp_path") or "").strip() and not all(present):
            raise ValueError("请提供 Shapefile 或框选范围")
    return {
        "operation": operation, "tool_id": tool_id, "parameters": values,
        "requires_confirmation": action.get("confirmation") == "required",
        "label": action.get("label") or "执行操作", "adapter": action.get("adapter"),
    }
