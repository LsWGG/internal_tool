"""适配器与任务管理器共享的纯函数。

这些函数原先散在 crawler_manager 顶层。搬到适配器层是为了让各数据源模块能直接引用，
而不必反向 import 任务管理器 —— 那会形成循环导入。函数体未作任何修改。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup


BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="CST")




def format_beijing_datetime(value):
    """将页面中常见的 ISO 8601、Unix 时间戳和日期转为北京时间。

    无时区的时间按北京本地时间处理；无法识别的内容保持原值，
    避免把普通文字误转换为日期。
    """
    if value in (None, "") or isinstance(value, bool):
        return value
    parsed = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return value
    else:
        text = str(value).strip()
        if not text:
            return text
        if re.fullmatch(r"\d{8}", text):
            try:
                parsed = datetime.strptime(text, "%Y%m%d")
            except ValueError:
                return value
        elif re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}", text):
            try:
                timestamp = float(text)
                if timestamp > 10_000_000_000:
                    timestamp /= 1000
                parsed = datetime.fromtimestamp(timestamp, timezone.utc)
            except (ValueError, OSError, OverflowError):
                return value
        else:
            normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(text)
                except (TypeError, ValueError, OverflowError):
                    return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    else:
        parsed = parsed.astimezone(BEIJING_TIMEZONE)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def normalize_row_datetimes(row, fields=None):
    """统一一行采集结果中的内置时间和用户点选的 datetime 字段。"""
    time_names = {"published_at", "created_at", "updated_at", "date", "time", "datetime", "timestamp"}
    for field in fields or []:
        if isinstance(field, str):
            name, attribute, selector = field, "", ""
        else:
            name = str(field.get("name") or "")
            attribute = str(field.get("attribute") or "")
            selector = str(field.get("selector") or "")
        builtin_name = selector.partition(":")[2] if selector.startswith("__builtin__:") else name
        if attribute == "datetime" or builtin_name in time_names:
            time_names.add(name)
    for name in list(row):
        lower_name = str(name).lower()
        looks_like_time = (
            name in time_names or lower_name in time_names
            or lower_name.endswith(("_at", "_date", "_time", "datetime", "timestamp"))
            or any(marker in str(name) for marker in ("发布时间", "创建时间", "更新时间", "日期"))
        )
        if looks_like_time:
            row[name] = format_beijing_datetime(row[name])
    return row


def safe_url(value):
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"网址无效：{value}")
    return value


def clean_url(value):
    parsed = urlparse(str(value or ""))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def same_site(left, right):
    def host(value):
        return urlparse(value).netloc.lower().split(":")[0].removeprefix("www.")
    return host(left) == host(right)


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def structured_text(node):
    """将正文节点转为保留段落、列表和表格换行的可读纯文本。"""
    if not node:
        return ""
    document = BeautifulSoup(str(node), "html.parser")
    for unwanted in document.select("script,style,noscript,template,svg"):
        unwanted.decompose()
    for br in document.select("br"):
        br.replace_with("\n")
    for cell in document.select("th,td"):
        cell.insert_after("\t")
    for item in document.select("li"):
        item.insert_before("\n• ")
        item.insert_after("\n")
    for block in document.select("h1,h2,h3,h4,h5,h6,p,blockquote,pre,section,article,div,tr,ul,ol"):
        block.insert_before("\n")
        block.insert_after("\n")
    lines = []
    for raw_line in document.get_text("", strip=False).splitlines():
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def decode_response(response):
    """按 BOM/页面声明优先解码，再做统计探测，避免 UTF-8 被误判为 GBK。"""
    raw = response.content or b""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")

    def normalize(value):
        value = str(value or "").strip().lower().replace("_", "-")
        aliases = {"utf8": "utf-8", "gb2312": "gb18030", "gb-2312": "gb18030", "gbk": "gb18030"}
        return aliases.get(value, value)

    # HTML 自身的声明通常比 HTTP 默认值可靠；两者只要可严格解码便直接采用。
    declared = []
    head = raw[:16384].decode("ascii", errors="ignore")
    for pattern in (
        r"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.-]+)",
        r"<meta[^>]+content\s*=\s*[\"'][^\"']*charset\s*=\s*([\w.-]+)",
        r"<\?xml[^>]+encoding\s*=\s*[\"']([\w.-]+)",
    ):
        match = re.search(pattern, head, re.I)
        if match:
            declared.append(normalize(match.group(1)))
    header = str(response.headers.get("content-type") or "")
    match = re.search(r"charset\s*=\s*[\"']?\s*([\w.-]+)", header, re.I)
    if match:
        declared.append(normalize(match.group(1)))
    ignored_defaults = {"iso-8859-1", "latin-1", "ascii"}
    for encoding in dict.fromkeys(declared):
        if not encoding or encoding in ignored_defaults:
            continue
        try:
            return raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue

    # 未声明编码时，使用 requests 已安装的 charset-normalizer/chardet 探测器。
    apparent = normalize(getattr(response, "apparent_encoding", ""))
    candidates = [apparent, "utf-8", "gb18030", "big5"]
    unique = []
    for encoding in candidates:
        if encoding and encoding not in unique:
            unique.append(encoding)
    best_text, best_score = "", float("-inf")
    for encoding in unique:
        try:
            text = raw.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            continue
        replacement = text.count("\ufffd")
        controls = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text))
        mojibake = len(re.findall(r"[ÃÂð]|锛|銆|鈥|涓[]|鏂伴椈|缇庡湅", text))
        # 不再用“汉字数量”判断编码；UTF-8 误按 GBK 解码恰恰会制造更多伪汉字。
        score = -replacement * 100 - controls * 40 - mojibake * 20
        if encoding == apparent:
            score += 8
        if encoding == "utf-8":
            score += 5
        if score > best_score:
            best_text, best_score = text, score
    return best_text


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)
