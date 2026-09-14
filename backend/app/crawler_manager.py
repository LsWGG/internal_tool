"""合规的智能网页采集任务管理器。

采集器面向公开页面：使用请求限速、重试和可选代理轮换，
不绕过验证码、登录墙或其他访问控制。字段解析支持 CSS 选择器、OpenGraph/
JSON-LD 和内置数据源模板；“AI 解析”在本地用页面语义规则生成建议字段，
若配置了兼容 OpenAI API 的服务，可由前端另行接入二次解析。
"""
from __future__ import annotations

import csv
import asyncio
import io
import html as html_lib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from .crawler_adapters import get_adapter
from .crawler_schedule import next_runs
from .crawler_sql_sink import validate_config as validate_sql_sink, write_rows as write_sql_rows


BUILTIN_FIELDS = {
    "generic": ["title", "description", "author", "published_at", "content", "image", "url"],
    "news": ["title", "description", "author", "published_at", "content", "image", "source", "url"],
    "twitter": ["text", "author", "published_at", "likes", "reposts", "replies", "media", "url"],
    "tiktok": ["title", "description", "author", "username", "published_at", "duration", "views", "likes", "comments", "shares", "thumbnail", "url"],
    "douyin": ["title", "description", "author", "username", "published_at", "duration", "views", "likes", "comments", "shares", "thumbnail", "url"],
    "telegram": ["message_id", "text", "author", "published_at", "views", "forwards", "replies", "media", "chat", "url"],
    "youtube": ["title", "description", "channel", "published_at", "duration", "views", "likes", "thumbnail", "url"],
    "wechat": ["title", "author", "published_at", "content", "image", "account", "url"],
}

TWITTER_USER_FIELDS = [
    "username", "display_name", "bio", "location", "followers", "following",
    "posts", "joined_at", "verified", "avatar", "url",
]

TIKTOK_USER_FIELDS = [
    "username", "display_name", "bio", "followers", "following", "videos",
    "likes", "verified", "avatar", "url",
]

TIKTOK_COMMENT_FIELDS = [
    "author", "username", "text", "published_at", "likes", "replies", "url",
]

TELEGRAM_MEMBER_FIELDS = [
    "user_id", "username", "display_name", "bio", "bot", "verified", "status", "url",
]

SOURCE_NAMES = {
    "generic": "普通网页",
    "news": "新闻文章",
    "twitter": "推文",
    "tiktok": "TikTok",
    "douyin": "抖音",
    "telegram": "Telegram",
    "youtube": "YouTube 视频",
    "wechat": "公众号文章",
}

BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="CST")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _format_beijing_datetime(value):
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


def _normalize_row_datetimes(row, fields=None):
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
            row[name] = _format_beijing_datetime(row[name])
    return row


def _safe_url(value):
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"网址无效：{value}")
    return value


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _structured_text(node):
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


def _decode_response(response):
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


class CrawlerTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="crawler-task")
        self.controls = {}
        self.active_runs = {}
        self._dynamic_hosts = set()
        try:
            self.tasks = json.loads(self.state_file.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.tasks = {}
        for task in self.tasks.values():
            request = task.get("request", {})
            # 兼容数据库曾作为附加开关保存的任务，将其迁移为单一保存方式。
            if request.get("sql_sink", {}).get("enabled") and request.get("output_format") != "postgresql":
                request["output_format"] = "postgresql"
            if task.get("status") in ("queued", "running", "paused"):
                task.update(status="failed", message="服务重启，任务已中断", error="服务重启")
                for run in reversed(task.get("runs") or []):
                    if run.get("status") in ("queued", "running", "paused"):
                        run.update(status="failed", message="服务重启，执行已中断",
                                   error="服务重启", finished_at=_now(), updated_at=_now())
                        break
        self._save()
        self.scheduler = threading.Thread(target=self._schedule_loop, daemon=True)
        self.scheduler.start()

    def _save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.state_file)

    def _update(self, task_id, **values):
        with self.lock:
            if task_id in self.tasks:
                values["updated_at"] = _now()
                task = self.tasks[task_id]
                task.update(values)
                run_id = self.active_runs.get(task_id)
                run = next((item for item in reversed(task.get("runs") or [])
                            if item.get("id") == run_id), None)
                if run:
                    for key in ("status", "progress", "current", "total", "message", "error", "result"):
                        if key in values:
                            run[key] = values[key]
                    run["updated_at"] = values["updated_at"]
                    if values.get("status") in ("completed", "failed", "cancelled"):
                        run["finished_at"] = values["updated_at"]
                self._save()

    def _start_run(self, task_id):
        with self.lock:
            task = self.tasks[task_id]
            now = _now()
            runs = task.setdefault("runs", [])
            sequence = max((int(item.get("sequence") or 0) for item in runs), default=0) + 1
            task.update(status="running", progress=0, current=0, message="开始执行",
                        error=None, result=None, updated_at=now)
            run = {"id": uuid.uuid4().hex, "sequence": sequence,
                   "trigger": task.pop("_run_trigger", "manual"), "status": "running",
                   "progress": 0, "current": 0, "total": 0, "message": "开始执行",
                   "error": None, "result": None, "started_at": now, "updated_at": now,
                   "finished_at": None}
            runs.append(run)
            if len(runs) > 100:
                expired = runs[:-100]
                del runs[:-100]
                for item in expired:
                    expired_id = str(item.get("id") or "")
                    if expired_id:
                        shutil.rmtree(self.data_dir / task_id / "runs" / expired_id,
                                      ignore_errors=True)
            self.active_runs[task_id] = run["id"]
            self._save()
            return run["id"]

    def _archive_run_result(self, task_id, run_id, result):
        filename = str((result or {}).get("file") or "")
        if not filename:
            return result
        task_dir = self.data_dir / task_id
        source = task_dir / filename
        if not source.is_file():
            return result
        run_dir = task_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / source.name
        if target.exists():
            target.unlink()
        source.replace(target)
        archived = dict(result)
        archived["file"] = str(target.relative_to(task_dir))
        if archived.get("data_file") == filename:
            archived["data_file"] = archived["file"]
        return archived

    def _cumulative_key(self, task_id, row):
        import hashlib
        source = self.tasks.get(task_id, {}).get("request", {}).get("source")
        if source == "wechat" and row.get("title"):
            value = "wechat|{}|{}|{}".format(
                str(row.get("account") or row.get("author") or "").strip(),
                str(row.get("title") or "").strip(),
                str(row.get("published_at") or "").strip(),
            )
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        url = str(row.get("url") or "").strip()
        value = url or json.dumps(row, ensure_ascii=False, sort_keys=True, default=str,
                                  separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _discovery_key(source, target):
        """返回发现阶段的稳定内容标识，供跨批次增量去重使用。"""
        if not isinstance(target, dict):
            return str(target or "")
        prefill = target.get("prefill") or {}
        # 微信原文链接中的 timestamp/signature 会随检索批次改变，不能用作
        # 持久化增量任务的文章身份。公开索引中这三个字段组合更稳定。
        if source == "wechat" and prefill.get("title"):
            return "wechat|{}|{}|{}".format(
                str(prefill.get("account") or prefill.get("author") or "").strip(),
                str(prefill.get("title") or "").strip(),
                str(prefill.get("published_at") or "").strip(),
            )
        return str(target.get("detail_url") or prefill.get("url") or target.get("url") or "")

    def _append_cumulative(self, task_id, run_id, rows):
        task_dir = self.data_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        database = task_dir / "cumulative.sqlite3"
        connection = sqlite3.connect(database, timeout=30)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS records (
                crawler_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                first_run_id TEXT, collected_at TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS imported_runs (
                run_id TEXT PRIMARY KEY, imported_at TEXT NOT NULL
            )""")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = connection.execute("SELECT value FROM metadata WHERE name='key_version'").fetchone()
            if not version or version[0] != "2":
                connection.execute("DELETE FROM records")
                connection.execute("DELETE FROM imported_runs")
                connection.execute("INSERT OR REPLACE INTO metadata VALUES ('key_version', '2')")
            inserted = 0
            for row in rows:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?)",
                    (self._cumulative_key(task_id, row), json.dumps(row, ensure_ascii=False, default=str),
                     run_id, _now()),
                )
                inserted += max(cursor.rowcount, 0)
            connection.commit()
            if run_id:
                connection.execute("INSERT OR REPLACE INTO imported_runs VALUES (?, ?)",
                                   (run_id, _now()))
                connection.commit()
            total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            return {"inserted": inserted, "duplicates": len(rows) - inserted, "total": total}
        finally:
            connection.close()

    def _read_run_rows(self, task_id, run):
        task_dir = self.data_dir / task_id
        result = run.get("result") or {}
        result_file = str(result.get("file") or "")
        filename = (result_file if Path(result_file).suffix.lower() == ".zip"
                    else str(result.get("data_file") or result_file))
        path = task_dir / filename
        if not path.is_file():
            return []
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                value = json.loads(path.read_text("utf-8"))
                return value if isinstance(value, list) else []
            if suffix == ".jsonl":
                return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
            if suffix == ".csv":
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    return list(csv.DictReader(stream))
            if suffix == ".xlsx":
                import pandas as pd
                return pd.read_excel(path).fillna("").to_dict("records")
            if suffix == ".zip":
                with zipfile.ZipFile(path) as archive:
                    name = next((name for name in archive.namelist()
                                 if Path(name).name in ("data.xlsx", "data.csv", "data.json", "data.jsonl")), "")
                    if not name:
                        return []
                    data = archive.read(name)
                    if name.endswith(".xlsx"):
                        import pandas as pd
                        return pd.read_excel(io.BytesIO(data)).fillna("").to_dict("records")
                    text = data.decode("utf-8-sig")
                    if name.endswith(".csv"):
                        return list(csv.DictReader(io.StringIO(text)))
                    if name.endswith(".jsonl"):
                        return [json.loads(line) for line in text.splitlines() if line.strip()]
                    value = json.loads(text)
                    return value if isinstance(value, list) else []
        except Exception:
            return []
        return []

    def export_cumulative(self, task_id):
        task = self.get(task_id)
        if not task:
            raise ValueError("采集任务不存在")
        database = self.data_dir / task_id / "cumulative.sqlite3"
        imported = set()
        if database.is_file():
            connection = sqlite3.connect(database, timeout=30)
            try:
                connection.execute("CREATE TABLE IF NOT EXISTS imported_runs (run_id TEXT PRIMARY KEY, imported_at TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
                version = connection.execute("SELECT value FROM metadata WHERE name='key_version'").fetchone()
                if not version or version[0] != "2":
                    connection.execute("DELETE FROM records")
                    connection.execute("DELETE FROM imported_runs")
                    connection.execute("INSERT OR REPLACE INTO metadata VALUES ('key_version', '2')")
                imported = {row[0] for row in connection.execute("SELECT run_id FROM imported_runs")}
                connection.commit()
            finally:
                connection.close()
        for run in task.get("runs") or []:
            run_id = str(run.get("id") or "")
            if run_id and run_id not in imported:
                rows = self._read_run_rows(task_id, run)
                if rows:
                    self._append_cumulative(task_id, run_id, rows)
        if not database.is_file():
            raise ValueError("当前任务还没有可导出的累计数据")
        connection = sqlite3.connect(database, timeout=30)
        try:
            rows = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM records ORDER BY collected_at, rowid")]
        finally:
            connection.close()
        if not rows and source in ("tiktok", "douyin") and request.get("tiktok_mode") == "comments" and targets:
            # 评论接口受限时仍返回视频占位行，允许用户继续配置字段和创建任务。
            rows = [{name: (url if name == "url" else "") for name in names}]
            errors = []
        if not rows:
            raise ValueError("当前任务还没有可导出的累计数据")
        task_dir = self.data_dir / task_id
        fmt = task.get("request", {}).get("output_format", "xlsx")
        fmt = "xlsx" if fmt == "postgresql" else fmt
        output = task_dir / f"all_data.{fmt}"
        if fmt == "json":
            output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
        elif fmt == "jsonl":
            output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), "utf-8")
        elif fmt == "csv":
            keys = list(dict.fromkeys(key for row in rows for key in row))
            with output.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=keys); writer.writeheader(); writer.writerows(rows)
        else:
            import pandas as pd
            pd.DataFrame(rows).to_excel(output, index=False)
        latest_result = dict(task.get("result") or {})
        cumulative = dict(latest_result.get("cumulative") or {})
        cumulative["total"] = len(rows)
        latest_result["cumulative"] = cumulative
        self._update(task_id, result=latest_result)
        if task.get("request", {}).get("source") not in ("youtube", "tiktok") or not task.get("request", {}).get("download_videos"):
            return output, len(rows)
        archive = task_dir / ("all_data_and_videos.zip" if task.get("request", {}).get("source") == "youtube" else "all_data_and_tiktok_videos.zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
            package.write(output, output.name)
            videos = task_dir / "videos"
            if videos.is_dir():
                for video in videos.iterdir():
                    if video.is_file() and video.stat().st_size > 0 and not video.name.endswith(".part"):
                        package.write(video, f"videos/{video.name}")
        return archive, len(rows)

    @staticmethod
    def public_task(task):
        if not task:
            return task
        result = json.loads(json.dumps(task, ensure_ascii=False, default=str))
        sink = result.get("request", {}).get("sql_sink")
        if isinstance(sink, dict) and sink.get("password"):
            sink["password"] = ""
            sink["password_configured"] = True
        request = result.get("request", {})
        for key in ("telegram_api_hash", "telegram_session"):
            if request.get(key):
                request[key] = ""
                request[key + "_configured"] = True
        return result

    def list(self):
        with self.lock:
            tasks = sorted(self.tasks.values(), key=lambda x: x.get("created_at", ""), reverse=True)
            return [self.public_task(task) for task in tasks]

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    @staticmethod
    def builtin_fields(source, twitter_mode=None, tiktok_mode=None, telegram_mode=None):
        if source == "twitter" and twitter_mode == "user":
            names = TWITTER_USER_FIELDS
        elif source in ("tiktok", "douyin") and tiktok_mode == "user":
            names = TIKTOK_USER_FIELDS
        elif source in ("tiktok", "douyin") and tiktok_mode == "comments":
            names = TIKTOK_COMMENT_FIELDS
        elif source == "telegram" and telegram_mode == "members":
            names = TELEGRAM_MEMBER_FIELDS
        else:
            names = BUILTIN_FIELDS.get(source, BUILTIN_FIELDS["generic"])
        return [{"name": name, "label": name.replace("_", " ").title(), "builtin": True} for name in names]

    @staticmethod
    def _selected_prefill_row(fields, prefill):
        """保持导出列与用户勾选字段完全一致，并填入结构化发现结果。"""
        names = [str(item.get("name") if isinstance(item, dict) else item).strip()
                 for item in (fields or [])]
        names = list(dict.fromkeys(name for name in names if name))
        source = prefill if isinstance(prefill, dict) else {}
        return {name: source.get(name, "") for name in names}

    def suggest(self, url, source="generic"):
        """抓取一页公开 HTML 并根据语义标签生成字段建议。"""
        url = _safe_url(url)
        response = self._request(url, [], timeout=20)
        html = _decode_response(response)
        soup = BeautifulSoup(html, "html.parser")
        suggestions = []
        article_link_selector = ""
        next_page_selector = ""
        if source == "news":
            host = urlparse(url).netloc.lower().removeprefix("www.")
            if host == "cn.nytimes.com" and soup.select_one(".sectionWrapper"):
                # 同一站点的浏览器 HTML 与直接请求 HTML 外层容器可能不同，
                # 标题标签比 sectionWrapper 更稳定，热门链接由采集过滤器排除。
                article_link_selector = "h2 a[href], h3 a[href]"
            else:
                for candidate in (
                    "main article a[href]", "article h2 a[href]", "article h3 a[href]",
                    ".news-list a[href]", ".article-list a[href]", ".story a[href]",
                ):
                    if len(soup.select(candidate)) >= 2:
                        article_link_selector = candidate
                        break
            if soup.select_one("a[rel='next'], link[rel='next']"):
                next_page_selector = "a[rel='next'], link[rel='next']"
            else:
                for anchor in soup.select("a[href]"):
                    if re.fullmatch(r"\s*(?:下一页|下页|next|next page)\s*(?:[>›»]+)?\s*", _text(anchor), re.I):
                        classes = [item for item in anchor.get("class", []) if re.fullmatch(r"[\w-]+", item)]
                        next_page_selector = "a." + ".".join(classes) if classes else ""
                        break
        selectors = {
            "title": ["h1", "meta[property='og:title']", "title"],
            "description": ["meta[name='description']", "meta[property='og:description']", "article p"],
            "author": ["meta[name='author']", "[rel='author']", ".author", ".byline"],
            "published_at": ["time", "meta[property='article:published_time']", "meta[name='date']"],
            "content": ["article", "main", ".article-content", ".post-content", ".entry-content"],
            "image": ["meta[property='og:image']", "article img", "main img"],
        }
        for name in BUILTIN_FIELDS.get(source, BUILTIN_FIELDS["generic"]):
            selector = next((sel for sel in selectors.get(name, []) if soup.select_one(sel)), "")
            if selector:
                suggestions.append({"name": name, "selector": selector, "attribute": "content" if selector.startswith("meta") else ("datetime" if name == "published_at" and selector == "time" else "text"), "required": name in ("title", "url")})
            elif name in ("url", "source"):
                suggestions.append({"name": name, "selector": "", "attribute": "value", "required": name == "url"})
        # 仅在用户显式配置兼容 OpenAI 的地址和密钥时发送页面片段；默认完全本地解析。
        llm_url, llm_key = os.getenv("CRAWLER_LLM_URL"), os.getenv("CRAWLER_LLM_API_KEY")
        if llm_url and llm_key:
            try:
                prompt = ("根据这段公开网页 HTML 返回 JSON。fields 是数组，每项包含 name、selector、attribute、required。"
                          "如果是新闻列表页，另外返回 article_link_selector 和 next_page_selector；必须只定位主新闻列表，"
                          "排除热门、推荐、导航和页脚。只选择稳定的 CSS 选择器。\n" + html[:50000])
                llm = requests.post(llm_url, headers={"Authorization": f"Bearer {llm_key}"}, json={"model": os.getenv("CRAWLER_LLM_MODEL", "gpt-4o-mini"), "messages": [{"role": "user", "content": prompt}], "temperature": 0}, timeout=45)
                llm.raise_for_status(); content = llm.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                object_match = re.search(r"\{.*\}", content, re.S)
                if object_match:
                    parsed = json.loads(object_match.group(0))
                    fields = parsed.get("fields") or []
                    if fields:
                        suggestions = [item for item in fields if isinstance(item, dict) and item.get("name")]
                    article_link_selector = str(parsed.get("article_link_selector") or article_link_selector).strip()
                    next_page_selector = str(parsed.get("next_page_selector") or next_page_selector).strip()
                else:
                    parsed = json.loads(re.search(r"\[.*\]", content, re.S).group(0))
                    if isinstance(parsed, list) and parsed:
                        suggestions = [item for item in parsed if isinstance(item, dict) and item.get("name")]
            except Exception:
                pass
        return {"url": url, "source": source, "fields": suggestions, "title": _text(soup.title),
                "article_link_selector": article_link_selector,
                "next_page_selector": next_page_selector}

    def create(self, payload):
        source = payload.get("source", "generic") if isinstance(payload, dict) else "generic"
        get_adapter(source).validate(payload)
        if source not in BUILTIN_FIELDS:
            source = "generic"
        urls = [_safe_url(item) for item in (payload.get("urls") or [])]
        if not urls and payload.get("url"):
            urls = [_safe_url(payload["url"])]
        account_name = str(payload.get("account_name") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        twitter_mode = str(payload.get("twitter_mode") or "keyword").strip().lower()
        if twitter_mode not in ("user", "history", "keyword"):
            twitter_mode = "keyword"
        tiktok_mode = str(payload.get("tiktok_mode") or "keyword").strip().lower()
        if tiktok_mode not in ("keyword", "comments", "user", "videos"):
            tiktok_mode = "keyword"
        telegram_mode = str(payload.get("telegram_mode") or "channel").strip().lower()
        if telegram_mode not in ("channel", "group", "members", "search"):
            telegram_mode = "channel"
        if source == "wechat" and not account_name and not urls:
            raise ValueError("请输入公众号名称或微信文章链接")
        if source in ("twitter", "youtube", "tiktok", "douyin", "telegram") and not keyword:
            if source == "twitter" and twitter_mode != "keyword":
                message = "请输入 X 账号"
            elif source in ("tiktok", "douyin"):
                message = {"user": "请输入 TikTok 账号", "videos": "请输入 TikTok 账号",
                           "comments": "请输入 TikTok 视频链接"}.get(tiktok_mode, "请输入 TikTok 搜索关键词")
            elif source == "telegram":
                message = "请输入全平台搜索关键词" if telegram_mode == "search" else "请输入 Telegram 频道或群组"
            else:
                message = "请输入搜索关键词"
            raise ValueError(message)
        if source not in ("wechat", "twitter", "youtube", "tiktok", "douyin", "telegram") and not urls:
            raise ValueError("请至少提供一个公开网页地址")
        if len(urls) > 500:
            raise ValueError("单个任务最多 500 个网址")
        fields = payload.get("fields") or []
        if not fields:
            fields = self.builtin_fields(source, twitter_mode, tiktok_mode, telegram_mode)
        proxies = [str(x).strip() for x in (payload.get("proxies") or []) if str(x).strip()]
        fmt = payload.get("output_format", "json")
        if fmt not in ("json", "csv", "xlsx", "jsonl", "postgresql", "video_zip"):
            fmt = "json"
        download_videos = bool(payload.get("download_videos", fmt == "video_zip"))
        if fmt == "video_zip":
            fmt = "xlsx"
        interval = max(0, int(payload.get("interval_minutes") or 0))
        cron = str(payload.get("cron") or "").strip()
        sql_sink = validate_sql_sink(payload.get("sql_sink"))
        if fmt == "postgresql" and not sql_sink.get("enabled"):
            raise ValueError("请选择并配置 PostgreSQL 数据库")
        next_run = (next_runs(cron, count=1)[0] if cron else
                    (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat()
                    if interval > 0 else _now())
        if cron:
            interval = 0
        task_id = uuid.uuid4().hex
        now = _now()
        raw_max_items = payload.get("max_items", 50)
        try:
            max_items = int(str(raw_max_items))
        except (TypeError, ValueError):
            raise ValueError("最多采集请输入 1～500 之间的整数") from None
        if max_items != -1 and not 1 <= max_items <= 500:
            raise ValueError("最多采集请输入 1～500 之间的整数，-1 表示不限量增量采集")
        youtube_quality = int(payload.get("youtube_quality") or 720)
        if youtube_quality not in (360, 720, 1080):
            youtube_quality = 720
        label_target = keyword if source in ("twitter", "youtube", "tiktok", "douyin", "telegram") else (account_name if source == "wechat" and account_name else (urlparse(urls[0]).netloc if urls else ""))
        twitter_label = {"user": "X 用户信息", "history": "X 历史推文", "keyword": "X 关键词推文"}.get(twitter_mode)
        tiktok_label = {"keyword": "TikTok 关键词视频", "comments": "TikTok 视频评论", "user": "TikTok 账号信息", "videos": "TikTok 账号视频"}.get(tiktok_mode)
        telegram_label = {"channel": "Telegram 频道", "group": "Telegram 群组", "members": "Telegram 群组成员", "search": "Telegram 全平台搜索"}.get(telegram_mode)
        label = payload.get("name") or (f"{twitter_label} · {label_target}" if source == "twitter" else (f"{tiktok_label} · {label_target}" if source in ("tiktok", "douyin") else (f"{telegram_label} · {label_target}" if source == "telegram" else f"{SOURCE_NAMES.get(source, '网页')}采集 · {label_target}")))
        record = {"id": task_id, "name": str(label)[:120], "status": "queued", "progress": 0,
                  "current": 0, "total": 0 if source in ("news", "wechat", "twitter", "youtube", "tiktok", "telegram") else len(urls), "message": "等待采集", "error": None,
                  "created_at": now, "updated_at": now, "next_run_at": next_run,
                  "schedule_paused": False,
                  "request": {"urls": urls, "source": source, "fields": fields, "proxies": proxies,
                              "output_format": fmt, "interval_minutes": interval, "cron": cron,
                              "sql_sink": sql_sink,
                              "delay_seconds": max(0.2, float(payload.get("delay_seconds") or 1.0)),
                              "dynamic": bool(payload.get("dynamic", False)),
                              "account_name": account_name, "keyword": keyword, "max_items": max_items,
                              "twitter_mode": twitter_mode,
                              "tiktok_mode": tiktok_mode,
                              "telegram_mode": telegram_mode,
                              "telegram_api_id": str(payload.get("telegram_api_id") or "").strip(),
                              "telegram_api_hash": str(payload.get("telegram_api_hash") or "").strip(),
                              "telegram_session": str(payload.get("telegram_session") or "").strip(),
                              "persistent": max_items == -1,
                              "download_videos": download_videos,
                              "youtube_quality": youtube_quality,
                              "article_link_selector": str(payload.get("article_link_selector") or "").strip(),
                              "next_page_selector": str(payload.get("next_page_selector") or "").strip()},
                  "result": None, "runs": []}
        with self.lock:
            self.tasks[task_id] = record
            self.controls[task_id] = {"paused": False, "cancelled": False}
            self._save()
        if cron:
            self._update(task_id, status="scheduled", message="等待定时执行")
        else:
            self.pool.submit(self._run, task_id)
        return self.public_task(record)

    def _request(self, url, proxies, timeout=30, session=None, headers=None):
        proxy = None
        if proxies:
            proxy = proxies[int(time.time() * 1000) % len(proxies)]
            if not proxy.startswith(("http://", "https://", "socks5://")):
                proxy = "http://" + proxy
        request_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        request_headers.update(headers or {})
        client = session or requests
        response = client.get(url, headers=request_headers,
                              proxies={"http": proxy, "https": proxy} if proxy else None,
                              timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        return response

    @staticmethod
    def _clean_url(value):
        parsed = urlparse(str(value or ""))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))

    @staticmethod
    def _same_site(left, right):
        def host(value):
            return urlparse(value).netloc.lower().split(":")[0].removeprefix("www.")
        return host(left) == host(right)

    def _article_links(self, page_url, soup, selector=""):
        """从新闻列表识别详情链接，排除栏目、翻页、登录和静态资源链接。"""
        if selector:
            selectors = [selector]
        elif urlparse(page_url).netloc.lower().removeprefix("www.") == "cn.nytimes.com":
            # 该站的栏目正文位于 sectionWrapper；推荐榜虽然也使用标题标签，
            # 但在此容器之外，不能作为当前栏目文章采集。
            selectors = [
                ".sectionWrapper h1 a[href]", ".sectionWrapper h2 a[href]",
                ".sectionWrapper h3 a[href]", ".sectionWrapper h4 a[href]",
                # 服务端返回的精简 HTML 偶尔没有 sectionWrapper；通用选择器
                # 作为降级，热门榜仍由 URL 和祖先容器规则排除。
                "article a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]",
                ".story a[href]", ".story-body a[href]", "li a[href]",
            ]
        else:
            selectors = [
                "main article a[href]", "main h1 a[href]", "main h2 a[href]", "main h3 a[href]",
                "[role='main'] article a[href]", "[role='main'] h2 a[href]", "[role='main'] h3 a[href]",
                "article a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]",
                ".news-list a[href]", ".news_list a[href]", ".article-list a[href]",
                ".article_list a[href]", ".list a[href]", ".content-list a[href]",
                "li a[href]",
            ]
        anchors = []
        for item in selectors:
            try:
                anchors.extend(soup.select(item))
            except Exception:
                continue
        ignored_text = re.compile(r"^(首页|上一页|下一页|末页|更多|登录|注册|next|previous|prev|more|\d+)$", re.I)
        ignored_path = re.compile(r"\.(?:jpg|jpeg|png|gif|svg|webp|css|js|pdf|zip|mp4)(?:$|\?)", re.I)
        # 列表页的侧栏和页脚经常也使用 article/list 等类名。它们不是当前
        # 栏目的文章，若不排除会造成“配置 50 条，第一页只凑出几十条”的假象。
        ignored_sections = re.compile(
            r"/(?:mostviewed|most-viewed|popular|recommended|recommend|topic|topics|"
            r"slideshow|slideshows|interactive|video|videos|tag|tags)(?:/|$)", re.I,
        )
        found, seen = [], set()
        for anchor in anchors:
            href = str(anchor.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = self._clean_url(urljoin(page_url, href))
            if url in seen or url == self._clean_url(page_url) or not self._same_site(page_url, url) or ignored_path.search(url):
                continue
            text = _text(anchor)
            if not text or ignored_text.match(text.strip()):
                continue
            parsed = urlparse(url)
            path = parsed.path.lower()
            if ignored_sections.search(path) or re.search(
                    r"(?:^|[?&])utm_(?:source|campaign)=.*(?:most.?viewed|popular)",
                    parsed.query, re.I):
                continue
            score = 0
            if len(text.strip()) >= 8:
                score += 1
            if anchor.find_parent(["article", "h1", "h2", "h3"]):
                score += 2
            parent_classes = " ".join(sum((node.get("class", []) for node in anchor.parents if getattr(node, "attrs", None)), []))[:800].lower()
            if re.search(r"sidebar|footer|nav(?:igation)?|most.?viewed|popular|hot.?story|recommend|related|pagination", parent_classes):
                continue
            if re.search(r"news|article|story|post|item|title|headline|content|list", parent_classes):
                score += 1
            if re.search(r"/(?:20\d{2}[/_-]\d{1,2}|20\d{6}|news|article|story|post|content)/", path):
                score += 2
            if re.search(r"\.(?:s?html?|aspx?)$", path):
                score += 1
            if parsed.query and re.search(r"(?:^|&)(?:page|p|start|offset)=\d+", parsed.query, re.I):
                score -= 2
            if score >= 2:
                seen.add(url)
                found.append(url)
        return found

    def _next_news_page(self, page_url, soup, selector=""):
        candidates = []
        if selector:
            try:
                candidates.extend(soup.select(selector))
            except Exception:
                pass
        candidates.extend(soup.select("a[rel='next'], link[rel='next']"))
        for anchor in soup.select("a[href]"):
            if re.fullmatch(r"\s*(?:下一页|下页|后页|next|next page)\s*(?:[>›»]+)?\s*|\s*[>›»]+\s*", _text(anchor), re.I):
                candidates.append(anchor)
        # 有些站点只显示页码，不提供 rel=next 或“下一页”文本。仅在明确的
        # 分页容器内寻找大于当前页的最小页码，避免误把正文数字当作分页。
        current_match = re.search(r"/page/(\d+)(?:/|$)", urlparse(page_url).path, re.I)
        current_number = int(current_match.group(1)) if current_match else 1
        numbered = []
        for anchor in soup.select(
                ".pagination a[href], .pager a[href], .paging a[href], nav[aria-label*='pag'] a[href]"):
            text = _text(anchor).strip()
            if text.isdigit() and int(text) > current_number:
                numbered.append((int(text), anchor))
        if numbered:
            candidates.append(min(numbered, key=lambda item: item[0])[1])
        # 兼容 /栏目/2/ 形式的纯数字分页。候选 URL 必须严格位于当前栏目
        # 路径的下一层，防止把文章日期或导航数字误判为页码。
        parsed_page = urlparse(page_url)
        page_path = parsed_page.path
        short_match = re.search(r"/(\d+)/?$", page_path)
        short_current = int(short_match.group(1)) if short_match else 1
        short_base = page_path[:short_match.start()] + "/" if short_match else page_path.rstrip("/") + "/"
        short_numbered = []
        for anchor in soup.select("a[href]"):
            label = _text(anchor).strip()
            if not label.isdigit() or int(label) <= short_current:
                continue
            candidate = self._clean_url(urljoin(page_url, anchor.get("href")))
            candidate_path = urlparse(candidate).path
            if re.fullmatch(re.escape(short_base) + r"\d+/", candidate_path):
                short_numbered.append((int(label), anchor))
        if short_numbered:
            candidates.append(min(short_numbered, key=lambda item: item[0])[1])
        for node in candidates:
            href = node.get("href")
            if href:
                url = self._clean_url(urljoin(page_url, href))
                if self._same_site(page_url, url) and url != self._clean_url(page_url):
                    return url
        # 目录型新闻栏目常采用 /section/page/N/，但首页不渲染可识别的
        # 下一页控件。纽约时报中文网等站点即需要此回退。后续页无文章时，
        # 发现循环在没有新增文章或出现重复页面时停止。
        parsed = urlparse(page_url)
        path = parsed.path
        match = re.search(r"/page/(\d+)(/?)$", path, re.I)
        if match:
            next_path = path[:match.start(1)] + str(int(match.group(1)) + 1) + path[match.end(1):]
        elif (parsed.netloc.lower().removeprefix("www.") == "cn.nytimes.com"
              and (short_match := re.search(r"/(\d+)(/?)$", path))):
            next_path = (path[:short_match.start(1)] + str(int(short_match.group(1)) + 1)
                         + path[short_match.end(1):])
        elif path.endswith("/") and not re.search(r"\.(?:s?html?|aspx?)$", path, re.I):
            if parsed.netloc.lower().removeprefix("www.") == "cn.nytimes.com":
                next_path = f"{path}2/"
            else:
                next_path = f"{path}page/2/"
        else:
            return ""
        return urlunparse((parsed.scheme, parsed.netloc, next_path, parsed.params, parsed.query, ""))

    def _fetch_listing_html(self, url, request):
        """新闻列表优先尝试浏览器滚动，以兼容无限滚动；不可用时退回普通请求。"""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                launch = {"headless": True}
                proxies = request.get("proxies") or []
                if proxies:
                    proxy = str(proxies[0]).strip()
                    launch["proxy"] = {"server": proxy if "://" in proxy else "http://" + proxy}
                browser = playwright.chromium.launch(**launch)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                stable = 0
                previous_height = 0
                while True:
                    for label in ("加载更多", "查看更多", "更多新闻", "Load more"):
                        button = page.get_by_text(label, exact=False).last
                        try:
                            if button.is_visible(timeout=150):
                                button.click(timeout=1000)
                                break
                        except Exception:
                            pass
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)
                    height = page.evaluate("document.body.scrollHeight")
                    stable = stable + 1 if height == previous_height else 0
                    previous_height = height
                    if stable >= 2:
                        break
                    maximum = int(request.get("max_items") or 50)
                    if maximum > 0 and len(self._article_links(
                            url, BeautifulSoup(page.content(), "html.parser"),
                            request.get("article_link_selector", ""))) >= maximum:
                        break
                content = page.content()
                browser.close()
                return content
        except Exception:
            return _decode_response(self._request(url, request.get("proxies", [])))

    def _discover_news_urls(self, seeds, request, task_id=None):
        maximum = int(request.get("max_items") or 50)
        if maximum < 0: maximum = 10**9
        queue, visited, articles = list(seeds), set(), []
        while queue and len(articles) < maximum:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            self._checkpoint(task_id)
            self._update(task_id, status="running", message=f"正在查找新闻文章 · 第 {len(visited)} 页", progress=10)
            try:
                listing_html = self._fetch_listing_html(page_url, request)
            except Exception:
                # 第一页失败应正常报告错误；推导出的后续页不存在或临时失败时，
                # 保留此前已经发现的文章，避免整项任务丢失。
                if articles:
                    break
                raise
            soup = BeautifulSoup(listing_html, "html.parser")
            previous_count = len(articles)
            for url in self._article_links(page_url, soup, request.get("article_link_selector", "")):
                if url not in articles:
                    articles.append(url)
                    if len(articles) >= maximum:
                        break
            # 推导出的分页若被站点重定向回栏目首页，会得到完全相同的链接；
            # 此时停止当前分页链，继续处理其他栏目。
            if len(articles) == previous_count:
                continue
            next_page = self._next_news_page(page_url, soup, request.get("next_page_selector", ""))
            if next_page and next_page not in visited:
                queue.append(next_page)
        if not articles:
            raise RuntimeError("没有在列表页中识别到新闻详情。请确认输入的是公开新闻列表页，或在高级设置中填写新闻链接规则。")
        return articles[:maximum]

    @staticmethod
    def _decode_script_url(value):
        value = str(value or "").replace("\\/", "/")
        # 不能对整条 URL 使用 html.unescape：`&timestamp` 会被按无分号的
        # `&times` 实体解码成 `×tamp`，从而破坏微信签名参数。
        value = re.sub(r"(?i)&amp;", "&", value)
        value = re.sub(
            r"&#(?:x[0-9a-fA-F]+|\d+);?",
            lambda item: html_lib.unescape(item.group(0)), value,
        )
        value = value.replace("&quot;", '"').replace("&apos;", "'")
        value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda item: chr(int(item.group(1), 16)), value)
        value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda item: chr(int(item.group(1), 16)), value)
        if value.startswith("//"):
            value = "https:" + value
        return value.strip().strip("'\" ;)")

    def _resolve_sogou_wechat_url(self, href, request, session=None, referer=""):
        """在同一搜索会话内执行搜狗中转，并只返回真实微信文章地址。"""
        public_url = urljoin("https://weixin.sogou.com", href)
        response = self._request(
            public_url, request.get("proxies", []), timeout=20, session=session,
            headers={"Referer": referer or "https://weixin.sogou.com/"},
        )

        # HTTP 重定向链中的 Location 或最终地址有时已经是微信原文。
        candidates = [response.url]
        for item in [*response.history, response]:
            location = item.headers.get("Location")
            if location:
                candidates.append(urljoin(item.url, location))

        content = _decode_response(response)
        # 常见中转页把 URL 拆成 `url = '...'`、`url += '...'` 多段后再跳转。
        chunks = [match[1] for match in re.findall(
            r"(?:var\s+)?url\s*(?:\+)?=\s*(['\"])(.*?)\1", content, re.I | re.S
        )]
        if chunks:
            candidates.append("".join(chunks))

        # 同时兼容直接写入脚本、location 跳转、Meta Refresh 和普通链接的页面。
        candidates.extend(re.findall(
            r"https?(?::|%3A)(?:\\?/|%2F){2}mp\.weixin\.qq\.com[^\s'\"<>]+", content, re.I
        ))
        candidates.extend(match[1] for match in re.findall(
            r"(?:location\.(?:replace|assign)|location\.href\s*=)\s*\(?\s*(['\"])(.*?)\1", content, re.I | re.S
        ))
        soup = BeautifulSoup(content, "html.parser")
        refresh = soup.select_one("meta[http-equiv]")
        if refresh and str(refresh.get("http-equiv", "")).lower() == "refresh":
            match = re.search(r"url\s*=\s*(.+)$", str(refresh.get("content") or ""), re.I)
            if match:
                candidates.append(match.group(1))
        candidates.extend(node.get("href", "") for node in soup.select("a[href*='mp.weixin.qq.com']"))

        for candidate in candidates:
            candidate = self._decode_script_url(candidate)
            candidate = re.sub(r"(?i)%3a", ":", candidate)
            candidate = re.sub(r"(?i)%2f", "/", candidate)
            parsed = urlparse(candidate)
            if parsed.hostname and parsed.hostname.lower() == "mp.weixin.qq.com":
                return self._clean_url(candidate)
        return ""

    def _discover_wechat_sogou_urls(self, account_name, request, task_id=None):
        """按公众号名称检索搜狗微信公开索引，不访问登录态或绕过验证码。"""
        maximum = int(request.get("max_items") or 50)
        if maximum < 0: maximum = 10**9
        articles, seen = [], set()
        normalized_name = re.sub(r"\s+", "", account_name).lower()
        session = requests.Session()
        page_number = 0
        # 搜狗公开索引有短时频控；最多退避重试两次，不尝试绕过验证码。
        while len(articles) < maximum:
            page_number += 1
            self._checkpoint(task_id)
            self._update(task_id, status="running", message=f"正在查找“{account_name}”的公开文章 · 第 {page_number} 页", progress=10)
            search_url = f"https://weixin.sogou.com/weixin?type=2&query={quote_plus(account_name)}&page={page_number}"
            response = None
            soup = None
            items = []
            attempts = 3 if page_number == 1 else 1
            for attempt in range(attempts):
                try:
                    if attempt:
                        session.close()
                        session = requests.Session()
                        self._update(task_id, message=f"公开索引暂时无结果，正在重试（{attempt + 1}/{attempts}）")
                        time.sleep(2 + attempt * 3)
                    response = self._request(
                        search_url, request.get("proxies", []), timeout=25, session=session,
                        headers={"Referer": "https://weixin.sogou.com/", "Accept-Language": "zh-CN,zh;q=0.9"},
                    )
                    soup = BeautifulSoup(_decode_response(response), "html.parser")
                    page_text = soup.get_text(" ", strip=True)
                    captcha = soup.select_one("#seccodeImage, .verify-wrap, .vcode-box, input[name='cpt']")
                    captcha = captcha or "/antispider/" in response.url
                    captcha = captcha or "此验证码用于确认这些请求是您的正常行为" in page_text
                    captcha = captcha or ("请先验证" in page_text and "验证码" in page_text)
                    if captcha:
                        break
                    items = soup.select(".news-box li, ul.news-list li, .news-list li")
                    if not captcha and (items or page_number > 1):
                        break
                except requests.RequestException:
                    if attempt == attempts - 1:
                        raise
            page_text = soup.get_text(" ", strip=True) if soup else ""
            captcha = soup.select_one("#seccodeImage, .verify-wrap, .vcode-box, input[name='cpt']") if soup else None
            captcha = captcha or (response is not None and "/antispider/" in response.url)
            captcha = captcha or "此验证码用于确认这些请求是您的正常行为" in page_text
            captcha = captcha or ("请先验证" in page_text and "验证码" in page_text)
            if captcha:
                raise RuntimeError("公众号公开搜索暂时需要人工验证。请稍后重试，或直接粘贴微信文章链接；系统不会绕过验证码。")
            if not items:
                break
            matched_on_page = 0
            for item in items:
                account = _text(item.select_one(".account, .s-p, .account-name, [uigs*='account']"))
                if account and normalized_name not in re.sub(r"\s+", "", account).lower():
                    continue
                link = item.select_one("h3 a[href], .txt-box a[href], a[href*='/link?url=']")
                if not link:
                    continue
                public_url = self._clean_url(urljoin(search_url, link.get("href")))
                try:
                    detail_url = self._resolve_sogou_wechat_url(
                        link.get("href"), request, session=session, referer=search_url
                    )
                except Exception:
                    detail_url = ""
                unique_url = detail_url or public_url
                if unique_url and unique_url not in seen:
                    timestamp_node = item.select_one(".s-p .s2 script")
                    timestamp_match = re.search(r"timeConvert\(['\"]?(\d+)", timestamp_node.get_text() if timestamp_node else "")
                    published_at = ""
                    if timestamp_match:
                        try:
                            published_at = datetime.fromtimestamp(int(timestamp_match.group(1)), timezone.utc).isoformat()
                        except (ValueError, OSError):
                            pass
                    prefill = {
                        "title": _text(item.select_one("h3")) or _text(link), "description": _text(item.select_one(".txt-info")),
                        "author": account or account_name, "account": account or account_name,
                        "published_at": published_at, "content": "", "image": "",
                        # 导出字段只保存真实微信原文；中转页仅供内部去重，避免交付失效链接。
                        "url": detail_url,
                    }
                    if not detail_url:
                        prefill["collection_note"] = "仅公开索引；详情访问受平台限制"
                    seen.add(unique_url)
                    articles.append({"url": detail_url or public_url, "detail_url": detail_url, "prefill": prefill})
                    matched_on_page += 1
                    if len(articles) >= maximum:
                        return articles
            if matched_on_page == 0 and page_number > 1:
                break
            time.sleep(request.get("delay_seconds", 1.0))
        if not articles:
            raise RuntimeError(f"没有查找到“{account_name}”发布的公开历史文章。请核对公众号全称；微信没有开放按名称读取完整历史文章的公共接口。")
        return articles

    def _discover_wechat_mobile_urls(self, account_name, request, task_id=None):
        """使用搜狗微信移动端索引；其访问策略与桌面入口相互独立。"""
        maximum = int(request.get("max_items") or 50)
        if maximum < 0:
            maximum = 500
        normalized_name = re.sub(r"\s+", "", account_name).lower()
        session = requests.Session()
        articles, seen = [], set()
        self._update(task_id, status="running", progress=10,
                     message="桌面索引需要验证，正在切换移动端公开索引")
        for page_number in range(1, 6):
            self._checkpoint(task_id)
            search_url = (
                "https://weixin.sogou.com/weixinwap?type=2&query="
                f"{quote_plus(account_name)}&page={page_number}"
            )
            response = self._request(
                search_url, request.get("proxies", []), timeout=25, session=session,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 Mobile/15E148"
                    ),
                    "Referer": "https://weixin.sogou.com/",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            soup = BeautifulSoup(_decode_response(response), "html.parser")
            page_text = soup.get_text(" ", strip=True)
            if "/antispider/" in response.url or "此验证码用于确认这些请求是您的正常行为" in page_text:
                raise RuntimeError("移动端公开索引也触发了访问验证")
            items = soup.select("li")
            if not items:
                break
            added = 0
            for item in items:
                account_node = item.select_one(".s2[data-sourcename], .s2")
                account = str(account_node.get("data-sourcename") or _text(account_node)) if account_node else ""
                if re.sub(r"\s+", "", account).lower() != normalized_name:
                    continue
                link = item.select_one("h4 a[href^='/link?'], a[data-uigs^='article_title_'][href]")
                if not link:
                    continue
                try:
                    detail_url = self._resolve_sogou_wechat_url(
                        link.get("href"), request, session=session, referer=search_url
                    )
                except Exception:
                    detail_url = ""
                if not detail_url or detail_url in seen:
                    continue
                date_node = item.select_one(".s3[data-lastmodified], .s3")
                published_at = str(date_node.get("data-lastmodified") or _text(date_node)) if date_node else ""
                articles.append({
                    "url": detail_url,
                    "detail_url": detail_url,
                    "prefill": {
                        "title": _text(link),
                        "description": _text(item.select_one("[data-type='article_summary']")),
                        "author": account,
                        "account": account,
                        "published_at": _format_beijing_datetime(published_at),
                        "content": "",
                        "image": "",
                        "url": detail_url,
                    },
                })
                seen.add(detail_url)
                added += 1
                if len(articles) >= maximum:
                    return articles
            if added == 0 and page_number > 1:
                break
            time.sleep(max(0.5, float(request.get("delay_seconds") or 1.0)))
        if not articles:
            raise RuntimeError(f"移动端公开索引没有找到“{account_name}”发布的文章")
        return articles

    def _discover_wechat_public_search_urls(self, account_name, request, task_id=None):
        """通过通用公开索引发现微信原文并逐篇校验公众号。"""
        maximum = int(request.get("max_items") or 50)
        if maximum < 0:
            maximum = 500
        normalized_name = re.sub(r"\s+", "", account_name).lower()
        query = quote_plus(f'site:mp.weixin.qq.com/s "{account_name}"')
        search_url = f"https://html.duckduckgo.com/html/?q={query}"
        self._update(task_id, status="running", progress=10,
                     message="主索引需要验证，正在切换备用公开索引")
        last_error = None
        response = None
        for attempt in range(2):
            try:
                if attempt:
                    time.sleep(3)
                response = self._request(
                    search_url, request.get("proxies", []), timeout=25,
                    headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                )
                break
            except requests.RequestException as exc:
                last_error = exc
        if response is None:
            raise RuntimeError(f"备用公开索引连接失败：{last_error}")

        soup = BeautifulSoup(_decode_response(response), "html.parser")
        candidates = []
        for link in soup.select(".result__a[href], a[href]"):
            href = str(link.get("href") or "").strip()
            parsed = urlparse(urljoin(search_url, href))
            candidate = parse_qs(parsed.query).get("uddg", [""])[0]
            if not candidate and parsed.hostname == "mp.weixin.qq.com":
                candidate = parsed.geturl()
            candidate = self._clean_url(candidate)
            if urlparse(candidate).hostname == "mp.weixin.qq.com" and candidate not in candidates:
                candidates.append(candidate)

        articles = []
        for candidate in candidates[:30]:
            self._checkpoint(task_id)
            try:
                html = self._fetch_html(candidate, {**request, "dynamic": False})
            except Exception:
                continue
            article = BeautifulSoup(html, "html.parser")
            account = _text(article.select_one("#js_name, .profile_nickname, .account"))
            if re.sub(r"\s+", "", account).lower() != normalized_name:
                continue
            title = _text(article.select_one("#activity-name, meta[property='og:title'], h1"))
            published_at = _text(article.select_one("#publish_time, time"))
            if not published_at:
                timestamp = re.search(r"(?:var\s+)?ct\s*=\s*['\"](\d{9,13})", html)
                if timestamp:
                    published_at = timestamp.group(1)
            articles.append({
                "url": candidate,
                "detail_url": candidate,
                "prefill": {
                    "title": title,
                    "author": account,
                    "account": account,
                    "published_at": _format_beijing_datetime(published_at),
                    "url": candidate,
                },
            })
            if len(articles) >= maximum:
                break
        if not articles:
            raise RuntimeError(f"备用公开索引也没有找到“{account_name}”的可验证原文")
        return articles

    def _discover_wechat_urls(self, account_name, request, task_id=None):
        """使用主、备用公开索引发现文章，避免单一搜索入口波动中断采集。"""
        cache_file = self.data_dir / task_id / "wechat_discovery_cache.json" if task_id else None
        # 公众号定时任务若每几分钟直接重查公开索引，很容易触发出口 IP 验证。
        # 近期成功结果可安全复用；真正的索引刷新保持至少 30 分钟间隔。
        if cache_file and int(request.get("max_items") or 50) == -1:
            try:
                cached = json.loads(cache_file.read_text("utf-8"))
                if time.time() - float(cached.get("fetched_at") or 0) < 1800:
                    articles = cached.get("articles") or []
                    if articles:
                        self._update(task_id, status="running", progress=10,
                                     message="正在复用近期索引结果，避免频繁访问触发验证")
                        return articles
            except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            articles = self._discover_wechat_sogou_urls(account_name, request, task_id)
        except (RuntimeError, requests.RequestException) as primary_error:
            try:
                articles = self._discover_wechat_mobile_urls(account_name, request, task_id)
            except (RuntimeError, requests.RequestException) as mobile_error:
                try:
                    articles = self._discover_wechat_public_search_urls(account_name, request, task_id)
                except (RuntimeError, requests.RequestException) as fallback_error:
                    raise RuntimeError(
                        "公众号公开索引均不可用。"
                        f"桌面入口：{primary_error}；移动入口：{mobile_error}；"
                        f"通用备用入口：{fallback_error}"
                    ) from fallback_error
        if cache_file and articles:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"fetched_at": time.time(), "articles": articles},
                                            ensure_ascii=False), "utf-8")
            temporary.replace(cache_file)
        return articles

    def _discover_youtube_urls(self, keyword, request, task_id=None):
        maximum = int(request.get("max_items") or 50)
        if maximum < 0: maximum = 10**9
        if task_id:
            self._update(task_id, status="running", message=f"正在 YouTube 搜索“{keyword}”", progress=10)
        api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        video_ids = []
        if api_key:
            response = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "snippet", "type": "video", "q": keyword, "maxResults": min(maximum, 50), "key": api_key},
                timeout=30,
            )
            response.raise_for_status()
            video_ids = [item.get("id", {}).get("videoId") for item in response.json().get("items", [])]
        else:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(keyword)}&hl=zh-CN"
            html = _decode_response(self._request(search_url, request.get("proxies", []), timeout=30))
            video_ids = re.findall(r'"videoId"\s*:\s*"([\w-]{11})"', html)
        video_ids = list(dict.fromkeys(item for item in video_ids if item))[:maximum]
        if not video_ids:
            raise RuntimeError("YouTube 没有返回公开视频结果。可稍后重试，或由管理员配置 YOUTUBE_API_KEY。")
        return [f"https://www.youtube.com/watch?v={video_id}" for video_id in video_ids]

    @staticmethod
    def _twitter_username(value):
        text = str(value or "").strip()
        match = re.search(r"(?:https?://(?:www\.)?(?:x|twitter)\.com/|(?:^|\s)(?:from:|@))([A-Za-z0-9_]{1,15})", text, re.I)
        if not match and re.fullmatch(r"[A-Za-z0-9_]{1,15}", text):
            match = re.match(r"([A-Za-z0-9_]{1,15})", text)
        return match.group(1) if match else ""

    @staticmethod
    def _walk_json(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from CrawlerTaskManager._walk_json(child)
        elif isinstance(value, list):
            for child in value:
                yield from CrawlerTaskManager._walk_json(child)

    def _twitter_profile_document(self, username, request):
        response = self._request(
            f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}?lang=zh-cn",
            request.get("proxies", []), timeout=18,
            headers={"Referer": "https://platform.twitter.com/"},
        )
        html = _decode_response(response)
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("#__NEXT_DATA__")
        data = {}
        if node:
            try:
                data = json.loads(node.get_text())
            except json.JSONDecodeError:
                pass
        return html, soup, data

    def _discover_twitter_user(self, account, request, task_id=None):
        accounts = [item.strip() for item in re.split(r"[\n,，;；]+", str(account or "")) if item.strip()]
        if len(accounts) > 1:
            rows = []
            for item in accounts[:500]:
                rows.extend(self._discover_twitter_user(item, request, task_id))
            return rows
        username = self._twitter_username(account)
        if not username:
            raise ValueError("请输入有效的 X 用户名，例如 @OpenAI")
        if task_id:
            self._update(task_id, status="running", message=f"正在读取 @{username} 的公开用户信息", progress=10)
        html, soup, data = self._twitter_profile_document(username, request)
        normalized = username.lower()
        users = []
        for item in self._walk_json(data):
            handle = str(item.get("screen_name") or item.get("username") or "").lstrip("@").lower()
            if handle == normalized:
                users.append(item)
        user = max(users, key=lambda item: sum(key in item for key in (
            "name", "description", "followers_count", "friends_count", "statuses_count",
            "profile_image_url_https", "created_at", "verified",
        )), default={})
        title_node = soup.select_one("meta[property='og:title']")
        page_title = str(title_node.get("content") or "") if title_node else _text(soup.title)
        page_description = ""
        description_node = soup.select_one("meta[property='og:description'], meta[name='description']")
        if description_node:
            page_description = str(description_node.get("content") or "")
        avatar_node = soup.select_one("meta[property='og:image']")
        avatar = str(avatar_node.get("content") or "") if avatar_node else ""
        display_name = str(user.get("name") or "").strip()
        if not display_name and page_title:
            display_name = re.sub(r"\s*\(@[^)]+\).*", "", page_title).strip()
        row = {
            "username": username,
            "display_name": display_name or username,
            "bio": str(user.get("description") or page_description).strip(),
            "location": str(user.get("location") or "").strip(),
            "followers": user.get("followers_count", ""),
            "following": user.get("friends_count", ""),
            "posts": user.get("statuses_count", ""),
            "joined_at": user.get("created_at", ""),
            "verified": user.get("verified", ""),
            "avatar": user.get("profile_image_url_https") or user.get("profile_image_url") or avatar,
            "url": f"https://x.com/{username}",
        }
        if not user and not page_title and username.lower() not in html.lower():
            raise RuntimeError(f"没有读取到 @{username} 的公开用户信息")
        return [{"url": row["url"], "detail_url": "", "prefill": row}]

    def _discover_twitter_posts(self, keyword, request, task_id=None):
        """无需 API Token，从公开索引发现推文并通过 X oEmbed 读取内容。"""
        mode = str(request.get("twitter_mode") or "keyword").lower()
        if mode == "user":
            return self._discover_twitter_user(keyword, request, task_id)
        if mode == "history":
            username = self._twitter_username(keyword)
            if not username:
                raise ValueError("请输入有效的 X 用户名，例如 @OpenAI")
            keyword = f"@{username}"
        requested = int(request.get("max_items") or 50)
        maximum = 500 if requested < 0 else min(500, max(1, requested))
        if task_id:
            self._update(task_id, status="running", message=f"正在公开索引中查找“{keyword}”", progress=10)

        status_pattern = re.compile(
            r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)", re.I
        )
        candidates = []

        def add_candidates(value):
            value = html_lib.unescape(str(value or "")).replace("\\/", "/")
            for username, status_id in status_pattern.findall(value):
                url = f"https://x.com/{username}/status/{status_id}"
                if url not in candidates:
                    candidates.append(url)

        add_candidates(keyword)
        direct_input = bool(candidates)
        account_match = re.search(r"(?:^|\s)(?:from:|@)([A-Za-z0-9_]{1,15})(?:\s|$)", keyword, re.I)
        bare_account = mode == "history" and not account_match and bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}", keyword.strip()))
        if bare_account:
            account_match = re.match(r"([A-Za-z0-9_]{1,15})", keyword.strip())
        if account_match:
            username = account_match.group(1)
            try:
                profile = self._request(
                    f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}?lang=zh-cn",
                    request.get("proxies", []), timeout=18,
                    headers={"Referer": "https://platform.twitter.com/"},
                )
                profile_html = _decode_response(profile)
                # 公开时间线内部同时使用绝对链接和 /账号/status/id 相对路径。
                add_candidates(profile_html)
                for status_id in re.findall(rf"/{re.escape(username)}/status/(\d+)", profile_html, re.I):
                    add_candidates(f"https://x.com/{username}/status/{status_id}")
            except requests.RequestException:
                pass

        if not direct_input and len(candidates) < maximum:
            query = quote_plus(f"site:x.com status {keyword}")
            search_urls = [
                f"https://html.duckduckgo.com/html/?q={query}",
                f"https://www.google.com/search?q={query}&num=50&hl=zh-CN",
            ]
            for search_url in search_urls:
                try:
                    response = self._request(
                        search_url, request.get("proxies", []), timeout=18,
                        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
                    )
                except requests.RequestException:
                    continue
                page = BeautifulSoup(_decode_response(response), "html.parser")
                for link in page.select("a[href]"):
                    href = urljoin(search_url, str(link.get("href") or ""))
                    parsed = urlparse(href)
                    href = parse_qs(parsed.query).get("uddg", [href])[0]
                    if href.startswith("/url?"):
                        href = parse_qs(urlparse(href).query).get("q", [href])[0]
                    add_candidates(href)
                add_candidates(str(page))
                if len(candidates) >= maximum:
                    break

        targets = []
        for post_url in candidates[:maximum]:
            self._checkpoint(task_id)
            payload = None
            embed_url = post_url.replace("https://x.com/", "https://twitter.com/")
            for endpoint in ("https://publish.twitter.com/oembed", "https://publish.x.com/oembed"):
                try:
                    response = self._request(
                        endpoint + "?" + urlencode({
                            "url": embed_url, "omit_script": "true", "dnt": "true", "lang": "zh-cn",
                        }),
                        request.get("proxies", []), timeout=15,
                        headers={"Referer": "https://platform.twitter.com/"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("html"):
                        break
                except (requests.RequestException, ValueError):
                    continue
            if not payload:
                continue
            embed = BeautifulSoup(str(payload.get("html") or ""), "html.parser")
            text_node = embed.select_one("blockquote p")
            date_link = embed.select("blockquote a[href]")
            text = _text(text_node)
            if not text:
                continue
            published_at = _text(date_link[-1]) if date_link else ""
            author = str(payload.get("author_name") or "").strip()
            username_match = status_pattern.search(post_url)
            username = username_match.group(1) if username_match else ""
            # 账号时间线可附带关键词，按正文做本地筛选；from:/@账号部分不参与匹配。
            filter_text = "" if bare_account else re.sub(
                r"(?:^|\s)(?:from:|@)[A-Za-z0-9_]{1,15}(?:\s|$)", " ", keyword
            ).strip()
            if account_match and filter_text and filter_text.lower() not in text.lower():
                continue
            targets.append({"url": post_url, "detail_url": "", "prefill": {
                "text": text, "author": author or username,
                "published_at": published_at, "likes": "", "reposts": "", "replies": "",
                "media": "", "url": str(payload.get("url") or post_url).replace("twitter.com/", "x.com/"),
            }})
            if len(targets) >= maximum:
                break
        if not targets:
            raise RuntimeError(
                f"公开网页索引暂未找到与“{keyword}”相关且可嵌入的推文。"
                "可尝试输入 @账号、from:账号 关键词，或稍后重试。"
            )
        return targets

    @staticmethod
    def _tiktok_username(value):
        text = html_lib.unescape(str(value or "")).strip()
        match = re.search(r"(?:https?://(?:www\.)?tiktok\.com/)?@([A-Za-z0-9._]{2,24})", text, re.I)
        if not match and re.fullmatch(r"[A-Za-z0-9._]{2,24}", text):
            match = re.match(r"([A-Za-z0-9._]{2,24})", text)
        return match.group(1) if match else ""

    @staticmethod
    def _tiktok_video_url(value):
        match = re.search(
            r"https?://(?:www\.)?(?:tiktok\.com/@[A-Za-z0-9._]+/video/\d+|douyin\.com/video/\d+|v\.douyin\.com/[A-Za-z0-9_-]+)",
            html_lib.unescape(str(value or "")), re.I,
        )
        return match.group(0).split("?", 1)[0] if match else ""

    @staticmethod
    def _tiktok_video_row(info):
        info = info or {}
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or info.get("url") or "")
        if not webpage_url.startswith("http") and info.get("id") and info.get("uploader_id"):
            webpage_url = f"https://www.tiktok.com/@{info['uploader_id']}/video/{info['id']}"
        return {
            "title": info.get("title") or info.get("fulltitle") or "",
            "description": info.get("description") or "",
            "author": info.get("uploader") or info.get("creator") or "",
            "username": info.get("uploader_id") or info.get("channel_id") or "",
            "published_at": info.get("timestamp") or info.get("upload_date") or "",
            "duration": info.get("duration_string") or info.get("duration") or "",
            "views": info.get("view_count", ""),
            "likes": info.get("like_count", ""),
            "comments": info.get("comment_count", ""),
            "shares": info.get("repost_count", info.get("share_count", "")),
            "thumbnail": info.get("thumbnail") or "",
            "url": webpage_url,
        }

    def _tiktok_extract(self, url, request, flat=False):
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("TikTok 采集组件未安装，请先安装 yt-dlp") from exc
        requested = int(request.get("max_items") or 50)
        maximum = 500 if requested < 0 else min(500, max(1, requested))
        options = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "socket_timeout": 30, "retries": 3, "playlistend": maximum,
            "extract_flat": "in_playlist" if flat else False,
        }
        # yt-dlp handles both TikTok and Douyin URLs; keep the extractor
        # generic so shared task/export logic remains unchanged.
        proxies = request.get("proxies") or []
        if proxies:
            proxy = str(proxies[0]).strip()
            options["proxy"] = proxy if "://" in proxy else "http://" + proxy
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                return downloader.extract_info(url, download=False) or {}
        except Exception as exc:
            message = str(exc).splitlines()[-1]
            if re.search(r"login|captcha|verify|sign in", message, re.I):
                raise RuntimeError("TikTok 要求登录或访问验证，无法读取该公开内容") from exc
            raise RuntimeError(f"TikTok 暂时无法访问：{message[:240]}。请检查网络或代理配置") from exc

    @classmethod
    def _tiktok_user_from_html(cls, username, html):
        soup = BeautifulSoup(html or "", "html.parser")
        documents = []
        for node in soup.select("script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, script[type='application/json']"):
            try:
                documents.append(json.loads(node.string or node.get_text() or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
        normalized = username.lower()
        candidates = []
        for document in documents:
            for item in cls._walk_json(document):
                handle = str(item.get("uniqueId") or item.get("unique_id") or item.get("username") or "").lstrip("@").lower()
                if handle == normalized:
                    candidates.append(item)
                nested_user = item.get("user") if isinstance(item.get("user"), dict) else None
                nested_stats = item.get("stats") if isinstance(item.get("stats"), dict) else None
                if nested_user:
                    nested_handle = str(nested_user.get("uniqueId") or nested_user.get("username") or "").lstrip("@").lower()
                    if nested_handle == normalized:
                        candidates.append({**nested_user, **(nested_stats or {})})
        user = max(candidates, key=lambda item: sum(key in item for key in (
            "nickname", "signature", "followerCount", "followingCount", "videoCount", "heartCount",
        )), default={})
        stats = next((item for item in candidates if any(key in item for key in (
            "followerCount", "followingCount", "videoCount", "heartCount",
        ))), {})
        row = {
            "username": user.get("uniqueId") or user.get("unique_id") or username,
            "display_name": user.get("nickname") or username,
            "bio": user.get("signature") or "",
            "followers": stats.get("followerCount", user.get("followerCount", "")),
            "following": stats.get("followingCount", user.get("followingCount", "")),
            "videos": stats.get("videoCount", user.get("videoCount", "")),
            "likes": stats.get("heartCount", stats.get("heart", user.get("heartCount", ""))),
            "verified": user.get("verified", ""),
            "avatar": user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb") or "",
            "url": f"https://www.tiktok.com/@{username}",
        }
        return row if candidates else None

    def _discover_tiktok_user(self, account, request, task_id=None):
        accounts = [item.strip() for item in re.split(r"[\n,，;；]+", str(account or "")) if item.strip()]
        if len(accounts) > 1:
            rows = []
            for item in accounts[:500]:
                rows.extend(self._discover_tiktok_user(item, request, task_id))
            return rows
        username = self._tiktok_username(account)
        if not username:
            raise ValueError("请输入有效的 TikTok 账号，例如 @tiktok")
        if task_id:
            self._update(task_id, status="running", message=f"正在读取 @{username} 的公开账号信息", progress=10)
        profile_url = f"https://www.tiktok.com/@{username}"
        try:
            html = self._fetch_html(profile_url, {**request, "dynamic": True, "preview_mode": True})
            row = self._tiktok_user_from_html(username, html)
        except Exception:
            row = None
        if not row:
            info = self._tiktok_extract(profile_url, request, flat=True)
            entry = next((item for item in (info.get("entries") or []) if item), {})
            row = {
                "username": entry.get("uploader_id") or username,
                "display_name": entry.get("uploader") or info.get("uploader") or username,
                "bio": info.get("description") or "", "followers": "", "following": "",
                "videos": info.get("playlist_count") or "", "likes": "", "verified": "",
                "avatar": info.get("thumbnail") or entry.get("thumbnail") or "", "url": profile_url,
            }
        return [{"url": profile_url, "detail_url": "", "prefill": row}]

    def _discover_tiktok_account_videos(self, account, request, task_id=None):
        accounts = [item.strip() for item in re.split(r"[\n,，;；]+", str(account or "")) if item.strip()]
        if len(accounts) > 1:
            targets, seen = [], set()
            for item in accounts[:500]:
                for target in self._discover_tiktok_account_videos(item, request, task_id):
                    url = target.get("url", "") if isinstance(target, dict) else str(target)
                    if url and url not in seen:
                        seen.add(url)
                        targets.append(target)
            return targets
        username = self._tiktok_username(account)
        if not username:
            raise ValueError("请输入有效的 TikTok 账号，例如 @tiktok")
        if task_id:
            self._update(task_id, status="running", message=f"正在查找 @{username} 的公开视频", progress=10)
        info = self._tiktok_extract(f"https://www.tiktok.com/@{username}", request, flat=True)
        targets = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            row = self._tiktok_video_row(entry)
            url = row["url"] or self._tiktok_video_url(entry.get("url"))
            if not url and entry.get("id"):
                url = f"https://www.tiktok.com/@{username}/video/{entry['id']}"
            if url:
                row["url"] = url
                targets.append({"url": url, "detail_url": "", "prefill": row})
        if not targets:
            raise RuntimeError(f"没有读取到 @{username} 的公开视频，请检查账号或代理配置")
        return targets

    def _tiktok_oembed_row(self, url, request):
        """使用 TikTok 官方公开 oEmbed 补全搜索结果，不依赖登录态。"""
        response = self._request(
            "https://www.tiktok.com/oembed?" + urlencode({"url": url}),
            request.get("proxies", []), timeout=18,
            headers={"Accept": "application/json"},
        )
        payload = response.json()
        author_url = str(payload.get("author_url") or "")
        username = self._tiktok_username(author_url)
        return {
            "title": payload.get("title") or "",
            "description": payload.get("title") or "",
            "author": payload.get("author_name") or username,
            "username": username,
            "published_at": "", "duration": "", "views": "", "likes": "",
            "comments": "", "shares": "",
            "thumbnail": payload.get("thumbnail_url") or "",
            "url": url,
        }

    def _discover_tiktok_index_urls(self, keyword, request, maximum):
        """TikTok 站内搜索不可用时，从公开网页索引发现视频链接。"""
        # 不同搜索引擎对 `site:`/`inurl:` 组合的处理并不一致；先用
        # 精确查询，再用宽查询兜底，避免因为搜索语法被忽略而得到空结果。
        queries = [
            f"site:tiktok.com/@ inurl:/video/ {keyword}",
            f"site:tiktok.com {keyword} video",
            f"TikTok {keyword}",
        ]
        endpoints = []
        for query_text in queries:
            query = quote_plus(query_text)
            endpoints.extend([
                f"https://html.duckduckgo.com/html/?q={query}",
                f"https://www.google.com/search?q={query}&num=50&hl=zh-CN",
                f"https://www.bing.com/search?q={query}&count=50",
            ])
        # 搜索引擎经常返回相对链接、转义斜杠或不带协议的 TikTok 链接。
        # 统一在这里恢复为可访问的公开视频地址，避免因链接表现形式变化而误判为空。
        pattern = re.compile(
            r"(?:(?:https?:)?//)?(?:www\.|m\.)?tiktok\.com/@[A-Za-z0-9._]+/video/\d+",
            re.I,
        )
        urls = []

        def add(value):
            text = unquote(html_lib.unescape(str(value or ""))).replace("\\u002F", "/").replace("\\/", "/")
            for found in pattern.findall(text):
                clean = found.split("?", 1)[0]
                if clean.startswith("//"):
                    clean = "https:" + clean
                elif not clean.startswith("http"):
                    clean = "https://" + clean
                clean = clean.replace("https://m.tiktok.com/", "https://www.tiktok.com/")
                if clean not in urls:
                    urls.append(clean)

        for endpoint in endpoints:
            if len(urls) >= maximum:
                break
            try:
                response = self._request(
                    endpoint, request.get("proxies", []), timeout=18,
                    headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
                )
            except requests.RequestException:
                continue
            page_html = _decode_response(response)
            add(page_html)
            soup = BeautifulSoup(page_html, "html.parser")
            for anchor in soup.select("a[href]"):
                href = str(anchor.get("href") or "")
                parsed = urlparse(href)
                for key in ("uddg", "q", "url", "u"):
                    value = parse_qs(parsed.query).get(key, [])
                    if value:
                        add(value[0])
                add(href)
        return urls[:maximum]

    def _discover_tiktok_browser_urls(self, keyword, request, maximum):
        """从浏览器 DOM 和站内公开搜索响应中同时发现视频。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []
        urls = []

        def add(url):
            clean = self._tiktok_video_url(url)
            if clean and clean not in urls:
                urls.append(clean)

        def add_payload(payload):
            for item in self._walk_json(payload):
                video_id = str(item.get("id") or item.get("aweme_id") or "")
                author = item.get("author") if isinstance(item.get("author"), dict) else {}
                username = str(author.get("uniqueId") or author.get("unique_id") or "")
                if video_id.isdigit() and username:
                    add(f"https://www.tiktok.com/@{username}/video/{video_id}")

        with sync_playwright() as playwright:
            launch = {"headless": True}
            proxies = request.get("proxies") or []
            if proxies:
                proxy = str(proxies[0]).strip()
                launch["proxy"] = {"server": proxy if "://" in proxy else "http://" + proxy}
            browser = playwright.chromium.launch(**launch)
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
                    locale="zh-CN", viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()

                def handle_response(response):
                    if "search" not in response.url.lower():
                        return
                    try:
                        if "json" in str(response.headers.get("content-type") or "").lower():
                            add_payload(response.json())
                    except Exception:
                        pass

                page.on("response", handle_response)
                page.goto(
                    f"https://www.tiktok.com/search?q={quote_plus(keyword)}",
                    wait_until="domcontentloaded", timeout=45000,
                )
                for _ in range(8):
                    page.wait_for_timeout(1000)
                    for href in page.locator('a[href*="/video/"]').evaluate_all(
                            "nodes => nodes.map(node => node.href)"):
                        add(href)
                    if len(urls) >= maximum:
                        break
                    page.mouse.wheel(0, 850)
                return urls[:maximum]
            finally:
                browser.close()

    def _discover_tiktok_keyword_videos(self, keyword, request, task_id=None):
        keyword = str(keyword or "").strip()
        direct = self._tiktok_video_url(keyword)
        if direct:
            try:
                row = self._tiktok_video_row(self._tiktok_extract(direct, request))
            except RuntimeError:
                row = self._tiktok_oembed_row(direct, request)
            row["url"] = row["url"] or direct
            return [{"url": direct, "detail_url": "", "prefill": row}]
        if not keyword:
            raise ValueError("请输入 TikTok 搜索关键词")
        if task_id:
            self._update(task_id, status="running", message=f"正在搜索 TikTok 关键词“{keyword}”", progress=10)
        requested = int(request.get("max_items") or 50)
        maximum = 500 if requested < 0 else min(500, max(1, requested))
        search_urls = [
            f"https://www.tiktok.com/search?q={quote_plus(keyword)}",
            f"https://www.tiktok.com/search?lang=en&q={quote_plus(keyword)}",
        ]
        html = ""
        for search_url in search_urls:
            try:
                html = self._fetch_html(search_url, {**request, "dynamic": True, "preview_mode": True})
            except Exception:
                html = ""
            html = html_lib.unescape(html).replace("\\u002F", "/").replace("\\/", "/")
            if re.search(r"(?:tiktok\.com|/video/)" , html, re.I):
                break
        pattern = re.compile(r"(?:https?://(?:www\.)?tiktok\.com)?(/@[A-Za-z0-9._]+/video/\d+)", re.I)
        urls = list(dict.fromkeys("https://www.tiktok.com" + path for path in pattern.findall(html)))
        if len(urls) < maximum:
            try:
                for url in self._discover_tiktok_browser_urls(keyword, request, maximum):
                    if url not in urls:
                        urls.append(url)
                    if len(urls) >= maximum:
                        break
            except Exception:
                pass
        if len(urls) < maximum:
            for url in self._discover_tiktok_index_urls(keyword, request, maximum):
                if url not in urls:
                    urls.append(url)
                if len(urls) >= maximum:
                    break
        targets = []
        for url in urls[:maximum]:
            self._checkpoint(task_id)
            try:
                row = self._tiktok_video_row(self._tiktok_extract(url, request))
            except RuntimeError:
                try:
                    row = self._tiktok_oembed_row(url, request)
                except (requests.RequestException, ValueError):
                    row = {"url": url}
            row["url"] = row.get("url") or url
            targets.append({"url": url, "detail_url": "", "prefill": row})
        if not targets:
            raise RuntimeError(
                "TikTok 当前未返回可公开访问的视频结果（站内搜索可能需要登录或受地区限制）。"
                "请粘贴一个 TikTok 视频链接，或在高级设置中配置可用代理后重试。"
            )
        return targets

    @staticmethod
    def _tiktok_comment_rows(items, video_url):
        rows, seen = [], set()
        for index, item in enumerate(items or [], 1):
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            username = str(item.get("username") or "").lstrip("@").strip()
            key = str(item.get("id") or f"{username}|{text}")
            if not text or key in seen:
                continue
            seen.add(key)
            rows.append({
                "author": item.get("author") or username, "username": username, "text": text,
                "published_at": item.get("published_at") or "", "likes": item.get("likes") or "",
                "replies": item.get("replies") or "", "url": f"{video_url}#comment-{item.get('id') or index}",
            })
        return rows

    def _discover_tiktok_comments(self, value, request, task_id=None):
        video_url = self._tiktok_video_url(value)
        if not video_url:
            raise ValueError("请输入完整的 TikTok 视频链接")
        if task_id:
            self._update(task_id, status="running", message="正在加载公开视频评论", progress=10)
        requested = int(request.get("max_items") or 50)
        maximum = 500 if requested < 0 else min(500, max(1, requested))
        # 先尝试 TikTok 公开评论接口（页面 DOM 不稳定时仍可获取评论）。
        video_id = video_url.rstrip("/").split("/")[-1].split("?")[0]
        try:
            api = f"https://www.tiktok.com/api/comment/list/?aid=1988&aweme_id={video_id}&count={min(maximum,100)}&cursor=0"
            response = self._request(api, request.get("proxies", []), timeout=20,
                                     headers={"Referer": video_url, "User-Agent": "Mozilla/5.0"})
            payload = response.json()
            comments = payload.get("comments") or payload.get("data", {}).get("comments") or []
            if comments:
                items = [{"id": item.get("cid"), "text": item.get("text") or item.get("share_info", {}).get("desc"),
                          "username": (item.get("user") or {}).get("unique_id") or (item.get("user") or {}).get("nickname"),
                          "author": (item.get("user") or {}).get("nickname"), "likes": item.get("digg_count"),
                          "published_at": item.get("create_time")} for item in comments]
                rows = self._tiktok_comment_rows(items, video_url)[:maximum]
                if rows:
                    return [{"url": row["url"], "detail_url": "", "prefill": row} for row in rows]
        except Exception:
            pass
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                launch = {"headless": True}
                proxies = request.get("proxies") or []
                if proxies:
                    proxy = str(proxies[0]).strip()
                    launch["proxy"] = {"server": proxy if "://" in proxy else "http://" + proxy}
                browser = playwright.chromium.launch(**launch)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
                    page.goto(video_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2200)
                    items = []
                    stable = 0
                    while len(items) < maximum and stable < 3:
                        current = page.locator('[data-e2e="comment-level-1"], [data-e2e="comment-item"], div[class*="DivCommentItemContainer"], div[class*="CommentItem"]').evaluate_all("""nodes => nodes.map((node, index) => ({
                          id: node.getAttribute('data-comment-id') || node.id || '',
                          username: (node.querySelector('[data-e2e="comment-username-1"], a[href^="/@"]')?.textContent || '').trim(),
                          author: (node.querySelector('[data-e2e="comment-username-1"], a[href^="/@"]')?.textContent || '').trim(),
                          text: (node.querySelector('[data-e2e="comment-level-1"] p, [data-e2e="comment-text"], p')?.textContent || node.textContent || '').trim(),
                          published_at: (node.querySelector('[data-e2e="comment-time-1"], time, span[class*="SpanCreatedTime"]')?.textContent || '').trim(),
                          likes: (node.querySelector('[data-e2e="comment-like-count"], span[class*="SpanLikeCount"]')?.textContent || '').trim(),
                          replies: (node.querySelector('[data-e2e="view-more-1"], div[class*="DivReplyActionContainer"]')?.textContent || '').trim()
                        }))""")
                        stable = stable + 1 if len(current) <= len(items) else 0
                        items = current
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1000)
                    content = page.content()
                    if not items and re.search(r"captcha|verify|challenge|robot.?check|验证码", content, re.I):
                        # TikTok 评论接口经常要求登录/验证。保留目标视频作为可预览记录，
                        # 避免整个预览流程卡死；正式采集时会在结果中标注评论不可用。
                        return [{"url": video_url, "detail_url": "", "prefill": {
                            "url": video_url, "text": "", "author": "", "username": "",
                            "published_at": "", "likes": "", "replies": "",
                            "collection_note": "TikTok 要求登录或访问验证，未能读取公开评论"
                        }}]
                finally:
                    browser.close()
        except ImportError as exc:
            raise RuntimeError("评论采集需要安装 Playwright 浏览器运行环境") from exc
        rows = self._tiktok_comment_rows(items, video_url)[:maximum]
        if not rows:
            # 页面可正常展示评论时，评论接口可能延迟或 DOM 结构变化；
            # 不再将其升级为预览失败，返回视频占位记录供字段点选。
            return [{"url": video_url, "detail_url": "", "prefill": {
                "url": video_url, "collection_note": "页面未返回可解析的评论内容，请稍后重试"
            }}]
        return [{"url": row["url"], "detail_url": "", "prefill": row} for row in rows]

    def _discover_tiktok_data(self, value, request, task_id=None):
        mode = str(request.get("tiktok_mode") or "keyword").lower()
        if mode == "user":
            return self._discover_tiktok_user(value, request, task_id)
        if mode == "videos":
            return self._discover_tiktok_account_videos(value, request, task_id)
        if mode == "comments":
            return self._discover_tiktok_comments(value, request, task_id)
        return self._discover_tiktok_keyword_videos(value, request, task_id)

    @staticmethod
    def _telegram_target(value):
        text = str(value or "").strip()
        match = re.search(r"(?:https?://)?(?:t|telegram)\.me/(?:s/|joinchat/|\+)?([A-Za-z0-9_+-]+)", text, re.I)
        if match:
            return match.group(1), f"https://t.me/{match.group(1)}"
        if re.fullmatch(r"@[A-Za-z0-9_]{5,}", text):
            return text[1:], f"https://t.me/{text[1:]}"
        if re.fullmatch(r"[A-Za-z0-9_]{5,}", text):
            return text, f"https://t.me/{text}"
        return text, text

    @staticmethod
    def _telegram_public_rows(html, chat, maximum):
        soup = BeautifulSoup(html or "", "html.parser")
        rows = []
        for item in soup.select(".tgme_widget_message_wrap"):
            message = item.select_one(".tgme_widget_message") or item
            post = str(message.get("data-post") or "")
            message_id = post.rsplit("/", 1)[-1] if "/" in post else ""
            text_node = item.select_one(".tgme_widget_message_text")
            author_node = item.select_one(".tgme_widget_message_author_name")
            time_node = item.select_one("time")
            views_node = item.select_one(".tgme_widget_message_views")
            forwards_node = item.select_one(".tgme_widget_message_forwards")
            replies_node = item.select_one(".tgme_widget_message_replies")
            link = item.select_one("a.tgme_widget_message_date")
            url = str(link.get("href") or "") if link else (f"https://t.me/{post}" if post else "")
            media = []
            for image in item.select(".tgme_widget_message_photo_wrap, img"):
                style = str(image.get("style") or "")
                found = re.search(r"url\(['\"]?([^)'\"]+)", style)
                value = found.group(1) if found else str(image.get("src") or "")
                if value and value not in media:
                    media.append(value)
            row = {
                "message_id": message_id, "text": _structured_text(text_node) if text_node else "",
                "author": _text(author_node), "published_at": time_node.get("datetime", "") if time_node else "",
                "views": _text(views_node), "forwards": _text(forwards_node), "replies": _text(replies_node),
                "media": "\n".join(media), "chat": chat, "url": url,
            }
            if row["text"] or row["media"]:
                rows.append(row)
        return rows[-maximum:]

    def _telegram_credentials(self, request):
        api_id = str(request.get("telegram_api_id") or os.getenv("TELEGRAM_API_ID") or "").strip()
        api_hash = str(request.get("telegram_api_hash") or os.getenv("TELEGRAM_API_HASH") or "").strip()
        session = str(request.get("telegram_session") or os.getenv("TELEGRAM_SESSION") or "").strip()
        if not api_id or not api_hash or not session:
            raise ValueError("此模式需要 Telegram API ID、API Hash 和已授权会话字符串")
        try:
            return int(api_id), api_hash, session
        except ValueError as exc:
            raise ValueError("Telegram API ID 必须是数字") from exc

    async def _telegram_api_collect_async(self, value, request, mode, maximum):
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:
            raise RuntimeError("Telegram API 采集组件未安装，请安装 Telethon") from exc
        api_id, api_hash, session = self._telegram_credentials(request)
        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram 会话已失效，请重新生成授权会话")
            if mode == "members":
                target, _ = self._telegram_target(value)
                entity = await client.get_entity(target)
                rows = []
                async for user in client.iter_participants(entity, limit=maximum):
                    status = type(user.status).__name__.replace("UserStatus", "") if user.status else ""
                    rows.append({
                        "user_id": user.id, "username": user.username or "",
                        "display_name": " ".join(part for part in (user.first_name, user.last_name) if part),
                        "bio": "", "bot": bool(user.bot), "verified": bool(user.verified), "status": status,
                        "url": f"https://t.me/{user.username}" if user.username else "",
                    })
                return rows
            iterator = client.iter_messages(None, search=value, limit=maximum) if mode == "search" else \
                client.iter_messages((await client.get_entity(self._telegram_target(value)[0])), limit=maximum)
            rows = []
            async for message in iterator:
                chat = await message.get_chat()
                sender = await message.get_sender()
                username = getattr(chat, "username", None)
                url = f"https://t.me/{username}/{message.id}" if username else ""
                rows.append({
                    "message_id": message.id, "text": message.message or "",
                    "author": " ".join(part for part in (getattr(sender, "first_name", ""), getattr(sender, "last_name", "")) if part) or getattr(sender, "username", ""),
                    "published_at": message.date, "views": message.views or "", "forwards": message.forwards or "",
                    "replies": getattr(message.replies, "replies", "") if message.replies else "",
                    "media": type(message.media).__name__ if message.media else "",
                    "chat": getattr(chat, "title", "") or username or "", "url": url,
                })
            return rows
        finally:
            await client.disconnect()

    def _telegram_api_collect(self, value, request, mode, maximum):
        try:
            return asyncio.run(self._telegram_api_collect_async(value, request, mode, maximum))
        except (ValueError, RuntimeError):
            raise
        except Exception as exc:
            message = str(exc).splitlines()[-1]
            if re.search(r"flood|wait", message, re.I):
                raise RuntimeError("Telegram 请求过于频繁，请稍后重试") from exc
            raise RuntimeError(f"Telegram API 采集失败：{message[:240]}") from exc

    def _discover_telegram_data(self, value, request, task_id=None):
        mode = str(request.get("telegram_mode") or "channel").lower()
        requested = int(request.get("max_items") or 50)
        maximum = 500 if requested < 0 else min(500, max(1, requested))
        if task_id:
            labels = {"channel": "频道消息", "group": "群组消息", "members": "群组成员", "search": "全平台结果"}
            self._update(task_id, status="running", message=f"正在查找 Telegram {labels.get(mode, '数据')}", progress=10)
        rows = []
        if mode in ("channel", "group"):
            target, public_url = self._telegram_target(value)
            if not target or not re.fullmatch(r"[A-Za-z0-9_]{5,}", target):
                raise ValueError("请输入公开频道或群组链接，例如 https://t.me/example")
            before = ""
            seen_ids = set()
            network_error = None
            try:
                while len(rows) < maximum:
                    self._checkpoint(task_id)
                    page_url = f"https://t.me/s/{target}" + (f"?before={before}" if before else "")
                    response = self._request(page_url, request.get("proxies", []), timeout=25)
                    page_rows = self._telegram_public_rows(_decode_response(response), target, maximum)
                    fresh = [row for row in page_rows if row.get("message_id") not in seen_ids]
                    if not fresh:
                        break
                    rows = fresh + rows
                    seen_ids.update(row.get("message_id") for row in fresh if row.get("message_id"))
                    numeric_ids = [int(row["message_id"]) for row in page_rows if str(row.get("message_id", "")).isdigit()]
                    if not numeric_ids:
                        break
                    next_before = str(min(numeric_ids))
                    if next_before == before:
                        break
                    before = next_before
                    if len(rows) < maximum:
                        time.sleep(request.get("delay_seconds", 1.0))
                rows = rows[-maximum:]
            except requests.RequestException as exc:
                network_error = exc
                rows = rows[-maximum:]
            if not rows and all(request.get(key) or os.getenv(key.upper()) for key in (
                    "telegram_api_id", "telegram_api_hash", "telegram_session")):
                rows = self._telegram_api_collect(public_url, request, mode, maximum)
            if not rows:
                if network_error and re.search(r"timeout|timed out|connect", str(network_error), re.I):
                    raise RuntimeError("无法连接 Telegram 公开页面，请在高级设置中配置可访问 Telegram 的代理")
                raise RuntimeError("没有读取到公开消息；请确认链接公开可访问，私有群组需配置 Telegram 授权会话")
        else:
            rows = self._telegram_api_collect(value, request, mode, maximum)
        return [{"url": row.get("url") or f"telegram:{mode}:{index}", "detail_url": "", "prefill": row}
                for index, row in enumerate(rows, 1)]

    def capabilities(self):
        return {"youtube_keyword": True, "youtube_api_configured": bool(os.getenv("YOUTUBE_API_KEY")),
                "twitter_keyword": True, "twitter_api_configured": True,
                "twitter_token_required": False,
                "twitter_mode": "public_index_oembed",
                "tiktok": True, "tiktok_token_required": False,
                "tiktok_modes": ["keyword", "comments", "user", "videos"],
                "telegram": True, "telegram_modes": ["channel", "group", "members", "search"],
                "telegram_api_configured": bool(os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH") and os.getenv("TELEGRAM_SESSION"))}

    @staticmethod
    def _wechat_preview_content(soup):
        content = soup.select_one("#js_content")
        if content and (content.get_text(strip=True) or content.select_one("img[data-src], img[src], img[data-original]")):
            return content
        return None

    def _prepare_wechat_preview(self, soup, url):
        """静态预览替代微信脚本的显示和懒加载操作，保留原 DOM 供选择器提取。"""
        content = self._wechat_preview_content(soup)
        if content is None:
            raise RuntimeError("微信原文未返回正文，文章可能已失效或暂时无法访问，请稍后重试或更换文章链接。")
        # 微信先隐藏正文，等脚本初始化后才显示；静态副本不会执行这些脚本。
        # 只展开正文及其祖先，保留文章内部的排版和有意隐藏的元素。
        for node in [content, *content.parents]:
            if node.name == "[document]":
                continue
            node.attrs.pop("hidden", None)
            node.attrs.pop("aria-hidden", None)
            node["style"] = str(node.get("style") or "").rstrip("; ") + (
                ";visibility:visible!important;opacity:1!important;display:block!important;"
                "height:auto!important;max-height:none!important;overflow:visible!important;"
                "content-visibility:visible!important;"
            )
        for img in content.select("img"):
            original_attribute = next(
                (attr for attr in ("data-src", "data-original", "data-lazy-src", "src") if img.get(attr)),
                None,
            )
            if not original_attribute:
                continue
            image_url = urljoin(url, str(img[original_attribute]).strip())
            if urlparse(image_url).scheme not in ("http", "https"):
                continue
            if urlparse(image_url).hostname in ("mmbiz.qpic.cn", "mmbiz.qlogo.cn"):
                image_url = image_url.replace("http://", "https://", 1)
            img["src"] = image_url
            # 点选后仍从原网页的懒加载属性提取真实地址，而非占位 src。
            img["data-crawler-attribute"] = original_attribute
            img["loading"] = "eager"
            img["referrerpolicy"] = "no-referrer"
            for attr in ("srcset", "sizes", "hidden", "aria-hidden"):
                img.attrs.pop(attr, None)
            img["style"] = str(img.get("style") or "").rstrip("; ") + (
                ";visibility:visible!important;opacity:1!important;"
                "display:block!important;max-width:100%!important;height:auto!important;max-height:none!important;"
            )

    @staticmethod
    def _preview_is_loading_shell(soup):
        """判断静态响应是否只有等待客户端脚本填充的骨架屏。"""
        body = soup.body or soup
        text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
        loading = bool(re.search(r"(?:载入|加载)中\s*(?:\.{2,}|…)?|loading\s*(?:\.{2,}|…)?", text, re.I))
        meaningful = re.sub(r"(?:载入|加载)中\s*(?:\.{2,}|…)?|loading\s*(?:\.{2,}|…)?", "", text, flags=re.I)
        return loading and len(meaningful.strip()) < 160

    @staticmethod
    def _prepare_generic_preview(soup, url):
        """移除失去脚本后不会自行消失的遮罩，并恢复懒加载图片。"""
        loading_text = re.compile(r"^\s*(?:(?:载入|加载)中\s*(?:\.{2,}|…)?|loading\s*(?:\.{2,}|…)?)\s*$", re.I)
        for node in list(soup.find_all(["div", "span", "p", "section"])):
            identity = " ".join([str(node.get("id") or ""), *node.get("class", [])]).lower()
            if loading_text.fullmatch(node.get_text(" ", strip=True)) and (
                    not node.find(True) or re.search(r"load|spinner|mask|skeleton|waiting", identity)):
                node.decompose()
        for image in soup.select("img"):
            attribute = next((name for name in (
                "data-src", "data-original", "data-lazy-src", "data-url", "src"
            ) if image.get(name)), None)
            if not attribute:
                continue
            value = urljoin(url, str(image.get(attribute) or "").strip())
            if urlparse(value).scheme not in ("http", "https"):
                continue
            image["src"] = value
            image["data-crawler-attribute"] = attribute
            image["loading"] = "lazy"
            image.attrs.pop("hidden", None)
            image.attrs.pop("aria-hidden", None)
            image["style"] = str(image.get("style") or "").rstrip("; ") + \
                ";visibility:visible!important;opacity:1!important;max-width:100%!important;height:auto!important"

    def preview(self, payload):
        """返回移除脚本和表单的页面副本，供前端安全点选字段。"""
        source = str(payload.get("source") or "generic")
        request = {
            "proxies": payload.get("proxies") or [],
            # 页面点选始终以真实浏览器渲染结果为准，与任务是否启用动态采集
            # 无关。否则静态响应和用户平时打开的页面会出现明显差异。
            "dynamic": True,
            "preview_mode": True,
            "max_items": 5,
            "delay_seconds": .2,
        }
        url = str(payload.get("url") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        account_name = str(payload.get("account_name") or "").strip()
        loaded_html = None
        if source == "twitter":
            raise ValueError("X 数据通过公开索引和官方嵌入页面返回固定字段，无需网页点选")
        if source == "youtube":
            if not keyword:
                raise ValueError("请先输入 YouTube 搜索关键词")
            url = self._discover_youtube_urls(keyword, request)[0]
            request["dynamic"] = True
        elif source == "news":
            url = _safe_url(url)
            soup = BeautifulSoup(self._fetch_listing_html(url, request), "html.parser")
            links = self._article_links(url, soup)
            if not links:
                raise ValueError("没有从新闻列表页找到可预览的详情文章")
            url = links[0]
        elif source == "wechat":
            if url:
                url = _safe_url(url)
            else:
                if not account_name:
                    raise ValueError("请先输入公众号名称")
                articles = self._discover_wechat_urls(account_name, request)
                for item in articles:
                    candidate = str(item.get("detail_url") or "")
                    if urlparse(candidate).hostname != "mp.weixin.qq.com":
                        continue
                    try:
                        candidate_html = self._fetch_html(candidate, request)
                    except requests.RequestException:
                        continue
                    if self._wechat_preview_content(BeautifulSoup(candidate_html, "html.parser")) is not None:
                        url, loaded_html = candidate, candidate_html
                        break
                if loaded_html is None:
                    raise RuntimeError(
                        f"已找到“{account_name}”的公开文章索引，"
                        "但候选原文未返回可预览的正文，请稍后重试或更换文章链接。"
                    )
        else:
            url = _safe_url(url)
        html = loaded_html if loaded_html is not None else self._fetch_html(url, request)
        soup = BeautifulSoup(html, "html.parser")
        # 普通静态请求若只拿到“载入中”骨架，自动用浏览器完成一次渲染，
        # 无需非技术用户预先知道并勾选“动态内容”。
        if source not in ("youtube", "wechat") and not request.get("dynamic") \
                and self._preview_is_loading_shell(soup):
            dynamic_request = {**request, "dynamic": True}
            html = self._fetch_html(url, dynamic_request)
            soup = BeautifulSoup(html, "html.parser")
        if source == "youtube":
            # YouTube 的正文依赖大量客户端脚本；移除脚本后只剩骨架屏，无法可靠点选。
            # 使用官方嵌入播放器展示真实视频，并在下方保留可点选的结构化字段。
            values = self._extract(url, soup, self.builtin_fields("youtube"))
            video_match = re.search(
                r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})",
                url,
            )
            video_id = video_match.group(1) if video_match else ""
            safe_url = html_lib.escape(url, quote=True)
            if video_id:
                safe_video_id = html_lib.escape(video_id, quote=True)
                player = (
                    '<section class="youtube-player" data-crawler-no-pick>'
                    '<div class="youtube-player-frame">'
                    f'<iframe src="https://www.youtube-nocookie.com/embed/{safe_video_id}" '
                    'title="YouTube 视频播放器" '
                    'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
                    'gyroscope; picture-in-picture; web-share; fullscreen" allowfullscreen></iframe>'
                    '</div><div class="youtube-player-meta">'
                    '<div><b>实际视频预览</b><small>可直接播放当前搜索到的代表视频</small></div>'
                    f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer" '
                    'data-crawler-no-pick>打开 YouTube 原始页面 ↗</a>'
                    '</div></section>'
                )
            else:
                player = (
                    '<section class="youtube-player youtube-player-unavailable" data-crawler-no-pick>'
                    '<div><b>暂时无法识别视频编号</b><small>可在 YouTube 原始页面查看视频</small></div>'
                    f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer" '
                    'data-crawler-no-pick>打开 YouTube 原始页面 ↗</a></section>'
                )
            labels = {
                "title": "视频标题", "description": "视频简介", "channel": "频道名称",
                "published_at": "发布时间", "duration": "视频时长", "views": "播放量",
                "likes": "点赞数", "thumbnail": "封面图片", "url": "视频地址",
            }
            cards = []
            for name in BUILTIN_FIELDS["youtube"]:
                value = values.get(name, "")
                shown = str(value or "暂未从代表视频中解析到该内容")
                selector = html_lib.escape(f"__builtin__:{name}", quote=True)
                label = labels.get(name, name)
                if name == "thumbnail" and value:
                    content = f'<img src="{html_lib.escape(str(value), quote=True)}" alt="{label}">'
                else:
                    content = f'<div class="value">{html_lib.escape(shown)}</div>'
                cards.append(
                    f'<article data-crawler-selector="{selector}" data-crawler-attribute="value">'
                    f'<small>{html_lib.escape(label)}</small>{content}</article>'
                )
            preview_html = """<!doctype html><html><head><meta charset="utf-8"><style>
                *{box-sizing:border-box}body{margin:0;padding:32px;color:#273c55;background:#f5f8fc;
                font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:crosshair}
                header{max-width:980px;margin:0 auto 18px}header b{font-size:20px}header p{margin:5px 0;color:#74859a}
                .youtube-player{max-width:980px;margin:0 auto 22px;overflow:hidden;border:1px solid #d6e1ef;
                border-radius:15px;background:#fff;box-shadow:0 10px 28px rgba(47,78,120,.08);cursor:default}
                .youtube-player-frame{position:relative;width:100%;aspect-ratio:16/9;background:#111827}
                .youtube-player-frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
                .youtube-player-meta{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 16px}
                .youtube-player-meta b,.youtube-player-meta small{display:block}.youtube-player-meta small{margin-top:2px;color:#74859a}
                .youtube-player a{flex:none;padding:8px 13px;border:1px solid #bfd0e5;border-radius:9px;color:#315fa9;
                background:#f7faff;font-weight:700;text-decoration:none}.youtube-player a:hover{background:#edf4ff}
                .youtube-player-unavailable{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px}
                .youtube-player-unavailable b,.youtube-player-unavailable small{display:block}
                main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;max-width:980px;margin:auto}
                article{min-width:0;padding:16px;border:1px solid #dce5f0;border-radius:12px;background:#fff}
                article:first-child,article:nth-child(2),article:nth-child(8){grid-column:1/-1}
                small{display:block;margin-bottom:7px;color:#5472a4;font-weight:700}.value{white-space:pre-wrap;word-break:break-word}
                img{display:block;width:min(100%,640px);max-height:360px;margin:auto;border-radius:9px;object-fit:contain;background:#edf2f8}
                @media(max-width:700px){body{padding:18px}main{grid-template-columns:1fr}article{grid-column:1!important}
                .youtube-player-meta,.youtube-player-unavailable{align-items:flex-start;flex-direction:column}}
            </style></head><body><header><b>YouTube 原始视频预览</b><p>上方可直接播放代表视频；下方字段可点击加入采集任务。</p></header>""" + player + "<main>" + "".join(cards) + "</main></body></html>"
            return {"url": url, "title": str(values.get("title") or "YouTube 视频"), "html": preview_html}
        if source == "wechat":
            self._prepare_wechat_preview(soup, url)
        else:
            loading_shell = self._preview_is_loading_shell(soup)
            self._prepare_generic_preview(soup, url)
            if loading_shell and len(_text(soup.body or soup)) < 80:
                raise RuntimeError("目标网站未返回可点选的正文，只返回了加载页面；请稍后重试或直接添加自定义字段规则。")
        for node in soup.select("script, iframe, object, embed, form, input, button, textarea, select, noscript, meta[http-equiv]"):
            node.decompose()
        for node in soup.find_all(True):
            for attr in list(node.attrs):
                if attr.lower().startswith("on") or attr.lower() in ("srcdoc", "integrity", "nonce"):
                    node.attrs.pop(attr, None)
            if node.name == "a":
                node["href"] = "#"
            elif node.get("src"):
                node["src"] = urljoin(url, node.get("src"))
            if node.name == "link" and node.get("href"):
                node["href"] = urljoin(url, node.get("href"))
        if soup.head:
            style = soup.new_tag("style")
            style.string = "html{scroll-behavior:auto}body{cursor:crosshair!important}*{box-sizing:border-box}a{pointer-events:auto}"
            soup.head.append(style)
        return {"url": url, "title": _text(soup.title), "html": str(soup)}

    def data_preview(self, payload):
        """采集前抓取少量样本，返回适合前端自适应展示的数据。"""
        source = str(payload.get("source") or "generic")
        if source not in BUILTIN_FIELDS:
            source = "generic"
        urls = [_safe_url(item) for item in (payload.get("urls") or []) if str(item).strip()]
        keyword = str(payload.get("keyword") or "").strip()
        account_name = str(payload.get("account_name") or "").strip()
        # 预览只用于确认字段和展现形式，最多读取三条，避免误触发大批量采集。
        sample_size = min(3, max(1, int(payload.get("sample_size") or 3)))
        request = {
            "source": source, "urls": urls, "keyword": keyword, "account_name": account_name,
            "twitter_mode": str(payload.get("twitter_mode") or "keyword"),
            "tiktok_mode": str(payload.get("tiktok_mode") or "keyword"),
            "telegram_mode": str(payload.get("telegram_mode") or "channel"),
            "telegram_api_id": str(payload.get("telegram_api_id") or "").strip(),
            "telegram_api_hash": str(payload.get("telegram_api_hash") or "").strip(),
            "telegram_session": str(payload.get("telegram_session") or "").strip(),
            "proxies": payload.get("proxies") or [], "dynamic": bool(payload.get("dynamic")),
            "max_items": sample_size,
            "delay_seconds": max(0.2, float(payload.get("delay_seconds") or 0.4)),
            "article_link_selector": str(payload.get("article_link_selector") or "").strip(),
            "next_page_selector": str(payload.get("next_page_selector") or "").strip(),
        }
        # 前端明确传入 fields（包括空数组）时必须严格遵守，不能用默认字段
        # 回填，否则“选择要保存的内容”和预览会出现不一致。
        fields = payload.get("fields") if "fields" in payload else self.builtin_fields(
            source, request["twitter_mode"], request["tiktok_mode"], request["telegram_mode"]
        )
        fields = [field for field in fields if not isinstance(field, dict) or field.get("enabled", True) is not False]
        if not fields:
            raise ValueError("请先在“选择要保存的内容”中勾选至少一个字段")
        names = []
        for field in fields:
            name = str(field.get("name") if isinstance(field, dict) else field).strip()
            if name and name not in names:
                names.append(name)
        if source in ("twitter", "youtube", "tiktok", "douyin", "telegram") and not keyword:
            if source in ("tiktok", "douyin"):
                message = {"user": "请先输入 TikTok 账号", "videos": "请先输入 TikTok 账号",
                           "comments": "请先输入 TikTok 视频链接"}.get(
                               request.get("tiktok_mode"), "请先输入 TikTok 搜索关键词"
                           )
            elif source == "telegram":
                message = "请先输入全平台搜索关键词" if request.get("telegram_mode") == "search" else "请先输入 Telegram 频道或群组"
            elif source == "twitter" and request.get("twitter_mode") != "keyword":
                message = "请先输入 X 账号"
            else:
                message = "请先输入搜索关键词"
            raise ValueError(message)
        if source == "wechat" and not account_name and not urls:
            raise ValueError("请先输入公众号名称或文章链接")
        if source not in ("twitter", "youtube", "tiktok", "douyin", "telegram", "wechat") and not urls:
            raise ValueError("请先输入网页或新闻列表页地址")

        if source == "news":
            targets = self._discover_news_urls(urls, request)[:sample_size]
        elif source == "wechat":
            targets = []
            if account_name:
                targets.extend(self._discover_wechat_urls(account_name, request)[:sample_size])
            known = {item.get("url") for item in targets if isinstance(item, dict)}
            targets.extend(url for url in urls if url not in known)
            targets = targets[:sample_size]
        elif source == "youtube":
            targets = self._discover_youtube_urls(keyword, request)[:sample_size]
        elif source == "twitter":
            targets = self._discover_twitter_posts(keyword, request)[:sample_size]
        elif source in ("tiktok", "douyin"):
            try:
                targets = self._discover_tiktok_data(keyword, request)[:sample_size]
            except RuntimeError as exc:
                # 评论页面可公开浏览但接口偶发返回登录提示；仍允许用视频链接完成字段预览。
                if request.get("tiktok_mode") == "comments":
                    video_url = self._tiktok_video_url(keyword)
                    if video_url:
                        targets = [{"url": video_url, "detail_url": "", "prefill": {
                            "url": video_url, "collection_note": str(exc)}}]
                    else:
                        raise
                else:
                    raise
        elif source == "telegram":
            targets = self._discover_telegram_data(keyword, request)[:sample_size]
        else:
            targets = urls[:sample_size]

        rows, errors = [], []
        for target in targets:
            url = target.get("url", "") if isinstance(target, dict) else target
            try:
                if isinstance(target, dict) and target.get("prefill") and not target.get("detail_url"):
                    row = self._selected_prefill_row(fields, target["prefill"])
                else:
                    html = self._fetch_html(url, request)
                    row = self._extract(url, BeautifulSoup(html, "html.parser"), fields)
                    if isinstance(target, dict):
                        for key, value in target.get("prefill", {}).items():
                            if key == "collection_note" or (key in row and not row[key]):
                                row[key] = value
                # 预览是配置结果，不是调试视图。丢弃发现流程或内置解析器
                # 附带的 URL、collection_note 等未勾选字段。
                row = {name: row.get(name, "") for name in names}
                rows.append(_normalize_row_datetimes(row, fields))
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)})

        if not rows:
            raise RuntimeError(errors[0]["error"] if errors else "没有找到可预览的采集数据")
        names = [name for name in names if any(name in row for row in rows)]
        # 根据来源和样本内容选择默认展现：视频/社交优先专用卡片，长正文使用文章卡片，
        # 其他内容根据字段数量和图片信息在卡片与表格之间切换。
        has_long_text = any(len(str(row.get("content") or row.get("description") or "")) > 240 for row in rows)
        has_title = any(row.get("title") for row in rows)
        has_image = any(row.get("image") or row.get("thumbnail") for row in rows)
        selected = set(names)
        if source == "twitter":
            mode = "social"
        elif source == "youtube" and selected.intersection({"title", "description", "thumbnail", "url"}):
            mode = "video"
        elif source == "twitter" and selected.intersection({"text", "media", "author", "url"}):
            mode = "social"
        elif source in ("tiktok", "douyin") and request.get("tiktok_mode") == "comments":
            mode = "social"
        elif source in ("tiktok", "douyin") and selected.intersection({"title", "description", "thumbnail", "url"}):
            mode = "video"
        elif source == "telegram" and request.get("telegram_mode") != "members":
            mode = "social"
        elif has_long_text:
            mode = "article"
        elif has_title and has_image:
            mode = "cards"
        else:
            mode = "table"
        return {"source": source, "mode": mode, "rows": rows, "columns": names,
                "sampled": len(rows), "total_hint": len(targets), "errors": errors}

    def _checkpoint(self, task_id):
        control = self.controls.get(task_id, {})
        while control.get("paused") and not control.get("cancelled"):
            time.sleep(.2)
        if control.get("cancelled"):
            raise RuntimeError("任务已取消")

    def _extract(self, url, soup, fields):
        result = {"url": url}
        page_source = str(soup)
        youtube_values = {}
        if "youtube.com" in urlparse(url).netloc.lower():
            for field_name, json_name in (("channel", "ownerChannelName"), ("views", "viewCount")):
                match = re.search(rf'"{json_name}"\s*:\s*"((?:\\.|[^"\\])*)"', page_source)
                if match:
                    try:
                        youtube_values[field_name] = json.loads(f'"{match.group(1)}"')
                    except json.JSONDecodeError:
                        youtube_values[field_name] = match.group(1)
        automatic_selectors = {
            "title": ["meta[property='og:title']", "#activity-name", "h1", "title"],
            "description": ["meta[name='description']", "meta[property='og:description']", "article p"],
            "author": ["meta[name='author']", "#js_name", "[rel='author']", ".author", ".byline"],
            "channel": ["link[itemprop='name']", "span[itemprop='author'] link[itemprop='name']", "#channel-name"],
            "published_at": ["meta[property='article:published_time']", "meta[itemprop='datePublished']", "#publish_time", "time", "meta[name='date']"],
            "content": ["#js_content", "article", "main", ".article-content", ".article-body", ".article-text", ".post-content", ".entry-content", "#cont_1_1_2", ".cont_1_1_2", "#content", ".content"],
            # 正文图片优先于 OG 封面图，且后续会收集所有匹配节点。
            "image": ["#js_content img", "article img", "main img", "meta[property='og:image']"],
            "thumbnail": ["meta[property='og:image']", "link[itemprop='thumbnailUrl']"],
            "duration": ["meta[itemprop='duration']"],
            "views": ["meta[itemprop='interactionCount']"],
            "account": ["#js_name", ".profile_nickname", ".account"],
        }
        for field in fields:
            if isinstance(field, str):
                field = {"name": field}
            name = str(field.get("name") or "field")
            selector = str(field.get("selector") or "").strip()
            attr = str(field.get("attribute") or "text")
            lookup_name = name
            if selector.startswith("__builtin__:"):
                lookup_name = selector.partition(":")[2]
                selector = ""
            if lookup_name == "url" and not selector:
                result[name] = url
                continue
            if lookup_name == "source" and not selector:
                result[name] = urlparse(url).netloc
                continue
            if lookup_name in youtube_values and not selector:
                result[name] = youtube_values[lookup_name]
                continue
            if not selector:
                selector = next((candidate for candidate in automatic_selectors.get(lookup_name, []) if soup.select_one(candidate)), "")
                if selector.startswith("meta"):
                    attr = "content"
                elif lookup_name == "published_at" and selector == "time":
                    attr = "datetime"
                elif lookup_name in ("image", "thumbnail") and selector:
                    attr = "data-src" if soup.select_one(selector) and soup.select_one(selector).get("data-src") else "src"
            # image 字段可能对应正文中的多张图片。全部提取并去重，以换行分隔，
            # 便于在 CSV/Excel 中阅读；兼容微信常见的 data-src/data-original 懒加载属性。
            if lookup_name == "image" and selector:
                values = []
                for node in soup.select(selector):
                    value = ""
                    if node.name == "meta":
                        value = node.get("content", "")
                    else:
                        for attr_name in ("data-src", "data-original", "data-lazy-src", "src"):
                            value = node.get(attr_name, "")
                            if value:
                                break
                        if not value and node.get("srcset"):
                            value = str(node.get("srcset")).split(",")[0].strip().split(" ")[0]
                    value = urljoin(url, str(value or "").strip())
                    if value and value not in values:
                        values.append(value)
                result[name] = "\n".join(values)
                continue
            node = soup.select_one(selector) if selector else None
            if not node:
                result[name] = ""
            elif attr in ("text", "value"):
                result[name] = _structured_text(node) if lookup_name == "content" else _text(node)
            else:
                result[name] = node.get(attr, "")
        return _normalize_row_datetimes(result, fields)

    def _fetch_html(self, url, request):
        """动态页面可选使用 Playwright；未安装浏览器时给出明确错误。"""
        host = urlparse(url).netloc.lower()
        dynamic = bool(request.get("dynamic")) or host in getattr(self, "_dynamic_hosts", set())
        if not dynamic:
            html = _decode_response(self._request(url, request.get("proxies", [])))
            if not self._preview_is_loading_shell(BeautifulSoup(html, "html.parser")):
                return html
            # 页面点选已证明该地址需要浏览器渲染；正式任务遇到同类验证页
            # 时也必须自动切换，否则自定义选择器只能得到空值。
            if not hasattr(self, "_dynamic_hosts"):
                self._dynamic_hosts = set()
            self._dynamic_hosts.add(host)
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                launch = {"headless": True}
                proxies = request.get("proxies") or []
                if proxies:
                    proxy = str(proxies[0]).strip()
                    launch["proxy"] = {"server": proxy if "://" in proxy else "http://" + proxy}
                browser = playwright.chromium.launch(**launch)
                try:
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/128.0.0.0 Safari/537.36"
                        ),
                        locale="zh-CN",
                        viewport={"width": 1440, "height": 1000},
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    requested_host = urlparse(url).hostname
                    current_host = urlparse(page.url).hostname
                    # 豆瓣等站点会跳到一个由普通浏览器自动计算并提交的等待页。
                    # 不解析或伪造挑战，只等待页面自身脚本完成正常导航。
                    if current_host == "sec.douban.com" or page.locator("form#sec").count():
                        try:
                            page.wait_for_url(
                                lambda value: urlparse(value).hostname == requested_host,
                                wait_until="domcontentloaded", timeout=30000,
                            )
                        except Exception as exc:
                            raise RuntimeError("网站浏览器验证在 30 秒内未完成，请稍后重试") from exc
                    # TikTok 搜索首屏先返回页面骨架，视频卡片通常在数秒后由
                    # 客户端接口写入 DOM。等待真实视频链接，避免过早截取空页面。
                    if host.endswith("tiktok.com") and urlparse(url).path.startswith("/search"):
                        try:
                            page.wait_for_selector('a[href*="/video/"]', state="attached", timeout=15000)
                        except Exception:
                            # 后续公开索引回退仍可继续工作；这里不把等待超时当成崩溃。
                            pass
                        page.wait_for_timeout(700)
                    else:
                        page.wait_for_timeout(1800)
                    if request.get("preview_mode"):
                        # 触发浏览器视口以下的懒加载内容，再回到顶部生成快照。
                        # 点选页面因此与用户实际滚动浏览后的内容保持一致。
                        for ratio in (.35, .7, 1):
                            page.evaluate("ratio => window.scrollTo(0, document.body.scrollHeight * ratio)", ratio)
                            page.wait_for_timeout(450)
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(250)
                    return page.content()
                finally:
                    browser.close()
        except ImportError as exc:
            raise RuntimeError("动态采集需要安装 Playwright 浏览器运行环境") from exc

    def _download_youtube_video(self, url, videos_dir, index, total, request, task_id):
        """下载单个公开视频并返回下载器元数据与最终文件。"""
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("YouTube 视频下载组件未安装，请安装 yt-dlp") from exc

        quality = int(request.get("youtube_quality") or 720)
        ffmpeg_path = os.getenv("YOUTUBE_FFMPEG_PATH", "").strip() or shutil.which("ffmpeg")
        if ffmpeg_path:
            format_selector = (
                f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best"
            )
        else:
            format_selector = f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best"

        videos_dir.mkdir(parents=True, exist_ok=True)
        last_update = [0.0]

        def progress_hook(status):
            self._checkpoint(task_id)
            if status.get("status") not in ("downloading", "finished"):
                return
            now = time.monotonic()
            if status.get("status") != "finished" and now - last_update[0] < .6:
                return
            last_update[0] = now
            downloaded = float(status.get("downloaded_bytes") or 0)
            expected = float(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
            fraction = min(1.0, downloaded / expected) if expected else 0.0
            progress = round(20 + ((index - 1) + fraction) / max(total, 1) * 75, 1)
            self._update(
                task_id, status="running", progress=progress, current=index - 1,
                message=f"正在下载第 {index}/{total} 个视频 · {quality}p",
            )

        options = {
            "format": format_selector,
            "outtmpl": str(videos_dir / f"{index:03d}_%(id)s_%(title).100B.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "color": "never",
            "continuedl": True,
            "overwrites": False,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_retries": 3,
            "socket_timeout": 30,
            "skip_unavailable_fragments": False,
            "concurrent_fragment_downloads": 2,
            "windowsfilenames": True,
            "progress_hooks": [progress_hook],
        }
        # 优先使用项目安装的 Deno，服务进程的 PATH 可能与终端不同。
        deno = Path(sys.executable).parent / ("deno.exe" if os.name == "nt" else "deno")
        deno_path = str(deno) if deno.is_file() else shutil.which("deno")
        if deno_path:
            options["js_runtimes"] = {"deno": {"path": deno_path}}
        warnings = []
        def clean_error(message):
            return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(message)).strip()

        class DownloadLogger:
            def debug(self, message):
                pass

            def warning(self, message):
                warnings.append(clean_error(message))

            def error(self, message):
                pass

        options["logger"] = DownloadLogger()
        completed_paths = []
        options["post_hooks"] = [completed_paths.append]
        if ffmpeg_path:
            options.update(merge_output_format="mp4", ffmpeg_location=ffmpeg_path)
        proxies = request.get("proxies") or []
        if proxies:
            proxy = str(proxies[(index - 1) % len(proxies)]).strip()
            options["proxy"] = proxy if proxy.startswith(("http://", "https://", "socks5://")) else "http://" + proxy
        cookies_file = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
        if cookies_file:
            options["cookiefile"] = cookies_file

        for attempt in range(3):
            self._checkpoint(task_id)
            completed_paths.clear()
            warnings.clear()
            try:
                # 重新解析页面以刷新签名及播放地址，不能仅重试同一个失效的媒体 URL。
                with yt_dlp.YoutubeDL(options) as downloader:
                    info = downloader.extract_info(url, download=True)
                break
            except yt_dlp.utils.DownloadError as exc:
                self._checkpoint(task_id)
                detail = clean_error(exc)
                transient = bool(re.search(
                    r"HTTP (?:Error )?(?:403|408|429|5\d\d)|timed? out|timeout|connection (?:reset|aborted)|"
                    r"remote end closed|temporary failure|unable to download video data",
                    detail, re.IGNORECASE,
                ))
                if not transient or attempt == 2:
                    hint = ""
                    if "403" in detail:
                        hint = "视频服务器拒绝访问（403），重新解析播放地址后仍失败。"
                    elif "429" in detail:
                        hint = "视频服务器请求过于频繁（429），请稍后重试。"
                    diagnostics = "；".join(warnings[-3:])
                    raise RuntimeError(hint + detail + (f"；解析提示：{diagnostics}" if diagnostics else "")) from exc
                self._update(task_id, message=f"第 {index}/{total} 个视频下载中断，正在重新解析重试（{attempt + 1}/2）")
                for _ in range((attempt + 1) * 10):
                    self._checkpoint(task_id)
                    time.sleep(.2)
        # 只接受下载器完成合并后的文件，避免把残留的音频或视频分轨误打包。
        candidates = [Path(path) for path in completed_paths
                      if isinstance(path, (str, os.PathLike)) and Path(path).is_file()
                      and Path(path).stat().st_size > 0]
        # yt-dlp post hook 在不同版本可能传入 info dict 而非文件路径，
        # 因此同时从输出目录扫描最终媒体文件，避免误报“未生成完整文件”。
        if not candidates:
            candidates = [path for path in videos_dir.iterdir()
                          if path.is_file() and path.stat().st_size > 0
                          and path.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov", ".avi"}
                          and not re.search(r"\.[a-z]\d+\.", path.name, re.I)]
        if not candidates:
            raise RuntimeError("视频下载未生成完整文件，请重试")
        return info or {}, candidates[-1]

    @staticmethod
    def _fill_youtube_row(row, info):
        upload_date = str(info.get("upload_date") or "")
        if len(upload_date) == 8 and upload_date.isdigit():
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        values = {
            "title": info.get("title"), "description": info.get("description"),
            "channel": info.get("channel") or info.get("uploader"),
            "published_at": info.get("timestamp") or upload_date,
            "duration": info.get("duration_string") or info.get("duration"),
            "views": info.get("view_count"), "likes": info.get("like_count"),
            "thumbnail": info.get("thumbnail"), "url": info.get("webpage_url"),
        }
        for key in list(row):
            if key in values and values[key] not in (None, ""):
                row[key] = values[key]
        return _normalize_row_datetimes(row)

    @classmethod
    def _fill_tiktok_download_row(cls, row, info, prefill=None):
        """合并 TikTok 下载器元数据与发现阶段数据，仅用于视频下载任务。"""
        downloaded = cls._tiktok_video_row(info)
        discovered = prefill if isinstance(prefill, dict) else {}
        for key in list(row):
            value = downloaded.get(key)
            if value in (None, ""):
                value = discovered.get(key)
            if value not in (None, ""):
                row[key] = value
        return _normalize_row_datetimes(row)

    def _run(self, task_id):
        run_id = self._start_run(task_id)
        task_dir = self.data_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task = self.tasks.get(task_id) or {}
        reuse_youtube_targets = task.pop("_retry_youtube_targets", False)
        request = task.get("request", {})
        rows, errors = [], []
        try:
            source = request.get("source", "generic")
            download_videos = bool(request.get("download_videos", True)) and (source == "youtube" or (source in ("tiktok", "douyin") and request.get("tiktok_mode") in ("keyword", "videos")))
            videos_dir = task_dir / "videos" if download_videos else None
            unit = {"news": "篇新闻", "wechat": "篇文章", "youtube": "个视频",
                    "twitter": "条推文", "tiktok": "个视频", "telegram": "条消息"}.get(source, "个网页")
            if source == "twitter" and request.get("twitter_mode") == "user":
                unit = "个用户"
            if source in ("tiktok", "douyin"):
                unit = {"user": "个账号", "comments": "条评论"}.get(
                    request.get("tiktok_mode"), "个视频"
                )
            if source == "telegram" and request.get("telegram_mode") == "members":
                unit = "个成员"
            if source == "news":
                urls = self._discover_news_urls(request.get("urls", []), request, task_id)
            elif source == "wechat":
                direct_urls = request.get("urls", [])
                discovered = []
                if request.get("account_name"):
                    try:
                        discovered = self._discover_wechat_urls(request.get("account_name", ""), request, task_id)
                    except (RuntimeError, requests.RequestException):
                        if not direct_urls:
                            raise
                known = {item.get("url") for item in discovered if isinstance(item, dict)}
                urls = discovered + [url for url in direct_urls if url not in known]
            elif source == "youtube":
                targets_file = task_dir / "youtube_targets.json"
                if reuse_youtube_targets and targets_file.is_file():
                    urls = json.loads(targets_file.read_text("utf-8"))
                else:
                    urls = self._discover_youtube_urls(request.get("keyword", ""), request, task_id)
                    targets_file.write_text(json.dumps(urls, ensure_ascii=False), "utf-8")
            elif source == "twitter":
                urls = self._discover_twitter_posts(request.get("keyword", ""), request, task_id)
            elif source in ("tiktok", "douyin"):
                urls = self._discover_tiktok_data(request.get("keyword", ""), request, task_id)
            elif source == "telegram":
                urls = self._discover_telegram_data(request.get("keyword", ""), request, task_id)
            else:
                urls = request.get("urls", [])
            if source != "generic":
                limit = int(request.get("max_items") or 50)
                if limit != -1:
                    urls = urls[:limit]
            seen_file = task_dir / "seen_urls.json"
            seen = set()
            def discovered_url(target):
                return self._discovery_key(source, target)
            if int(request.get("max_items") or 50) == -1:
                try:
                    seen = set(json.loads(seen_file.read_text("utf-8")))
                except (FileNotFoundError, json.JSONDecodeError, TypeError):
                    seen = set()
                urls = [target for target in urls if not discovered_url(target) or discovered_url(target) not in seen]
                if not urls:
                    self._update(task_id, status="completed", progress=100, current=0, total=0,
                                 message="本次无新增文章（已在详情采集前去重）", error=None,
                                 result={"row_count": 0, "failed_count": 0, "errors": [],
                                         "file": None, "sql": None})
                    return
            self._update(task_id, total=len(urls), current=0, message=f"已找到 {len(urls)} {unit}")
            completed_urls = set()
            for index, target in enumerate(urls, 1):
                self._checkpoint(task_id)
                self._update(task_id, status="running", message=f"正在处理第 {index}/{len(urls)} {unit}", current=index - 1, progress=round(20 + (index - 1) / len(urls) * 75, 1))
                url = target.get("url", "") if isinstance(target, dict) else target
                try:
                    if download_videos:
                        info, video_path = self._download_youtube_video(
                            url, videos_dir, index, len(urls), request, task_id
                        )
                        row = {
                            str(field.get("name") if isinstance(field, dict) else field): ""
                            for field in request.get("fields", [])
                        }
                        row.setdefault("url", url)
                        if source in ("tiktok", "douyin"):
                            self._fill_tiktok_download_row(
                                row, info, target.get("prefill", {}) if isinstance(target, dict) else {}
                            )
                        else:
                            self._fill_youtube_row(row, info)
                        row["video_file"] = f"videos/{video_path.name}"
                        rows.append(_normalize_row_datetimes(row, request.get("fields", [])))
                        completed_urls.add(discovered_url(target))
                        self._update(
                            task_id, current=index,
                            progress=round(20 + index / max(len(urls), 1) * 75, 1),
                            message=f"已下载 {index}/{len(urls)} 个视频",
                        )
                        time.sleep(request.get("delay_seconds", 1.0))
                        continue
                    if isinstance(target, dict) and target.get("prefill") and not target.get("detail_url"):
                        prefill = target.get("prefill", {})
                        row = self._selected_prefill_row(request.get("fields", []), prefill)
                        rows.append(_normalize_row_datetimes(row, request.get("fields", [])))
                        completed_urls.add(discovered_url(target))
                        continue
                    html = self._fetch_html(url, request)
                    row = self._extract(url, BeautifulSoup(html, "html.parser"), request.get("fields", []))
                    if isinstance(target, dict):
                        for key, value in target.get("prefill", {}).items():
                            if key == "collection_note" or (key in row and not row[key]):
                                row[key] = value
                    rows.append(_normalize_row_datetimes(row, request.get("fields", [])))
                    completed_urls.add(discovered_url(target))
                except Exception as exc:
                    errors.append({"url": url, "error": str(exc)})
                time.sleep(request.get("delay_seconds", 1.0))
            if not rows:
                raise RuntimeError(errors[0]["error"] if errors else "没有采集到数据")
            # 持久化采集源按 URL 增量去重；已处理过的内容不会重复导出。
            if int(request.get("max_items") or 50) == -1:
                fresh = [row for row in rows if row.get("url") not in seen]
                seen.update(str(row.get("url")) for row in fresh if row.get("url"))
                seen.update(value for value in completed_urls if value)
                rows = fresh
                if not rows:
                    self._update(task_id, status="completed", progress=100, current=len(urls),
                                 message="本次无新增数据（已自动去重）", error=None,
                                 result={"row_count": 0, "failed_count": len(errors),
                                         "errors": errors, "file": None, "sql": None})
                    return
            selected_names = {
                str(field.get("name") if isinstance(field, dict) else field)
                for field in request.get("fields", [])
            }
            index_only_count = sum(
                1 for row in rows
                if str(row.get("collection_note", "")).startswith("仅公开索引")
            )
            # 最终任务结果与采集前预览使用同一字段白名单。内部发现字段只
            # 用于状态统计，不得出现在 Excel/CSV/JSON 中。
            for row in rows:
                for key in list(row):
                    if key not in selected_names and not (download_videos and key == "video_file"):
                        row.pop(key, None)
            cumulative = self._append_cumulative(task_id, run_id, rows)
            sql_result = None
            output_format = request.get("output_format", "json")
            # PostgreSQL 没有可放入 ZIP 的本地数据表文件；视频交付时使用
            # Excel 快照作为配套索引，数据库写入仍按用户选择执行。
            if download_videos and output_format == "postgresql":
                output_format = "xlsx"
            if output_format == "postgresql":
                self._update(task_id, progress=97, message="正在写入 PostgreSQL")
                sql_result = write_sql_rows(request["sql_sink"], rows)
            else:
                output_name = {"json": "data.json", "jsonl": "data.jsonl", "csv": "data.csv", "xlsx": "data.xlsx"}[output_format]
                output = task_dir / output_name
                if output_format == "json":
                    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
                elif output_format == "jsonl":
                    output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), "utf-8")
                elif output_format == "csv":
                    keys = list(dict.fromkeys(k for row in rows for k in row))
                    with output.open("w", newline="", encoding="utf-8-sig") as stream:
                        writer = csv.DictWriter(stream, fieldnames=keys); writer.writeheader(); writer.writerows(rows)
                else:
                    import pandas as pd
                    pd.DataFrame(rows).to_excel(output, index=False)
            result_file = None if output_format == "postgresql" else output.name
            video_count = 0
            video_count = 0
            if download_videos:
                self._update(task_id, progress=98, message="正在打包数据表格和视频文件")
                if errors:
                    (task_dir / "download_errors.json").write_text(
                        json.dumps(errors, ensure_ascii=False, indent=2), "utf-8"
                    )
                archive = task_dir / ("youtube_videos.zip" if source == "youtube" else "tiktok_videos.zip")
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
                    package.write(output, output.name)
                    packaged = set()
                    for row in rows:
                        relative = str(row.get("video_file") or "")
                        video_path = task_dir / relative
                        if relative and relative not in packaged and video_path.is_file():
                            package.write(video_path, relative)
                            packaged.add(relative)
                    video_count = len(packaged)
                    if errors:
                        package.write(task_dir / "download_errors.json", "download_errors.json")
                result_file = archive.name
            result = {"file": result_file, "data_file": None if output_format == "postgresql" else output.name, "row_count": len(rows), "failed_count": len(errors), "index_only_count": index_only_count, "video_count": video_count, "errors": errors, "sql": sql_result, "cumulative": cumulative}
            result = self._archive_run_result(task_id, run_id, result)
            message = (f"下载完成 · {video_count} 个视频已打包" if download_videos else (f"已获取 {len(rows)} 篇公开索引（未获取正文）" if index_only_count == len(rows) else (f"采集完成 · {index_only_count} 篇仅公开索引" if index_only_count else "采集完成")))
            if download_videos and errors:
                message = f"部分下载完成 · 成功 {video_count} 个，失败 {len(errors)} 个，可重新采集重试"
            elif output_format == "postgresql":
                message = f"数据库写入完成 · 新增 {sql_result['inserted']} 条，去重 {sql_result['duplicates']} 条"
            if int(request.get("max_items") or 50) == -1:
                tmp_seen = seen_file.with_suffix(".tmp")
                tmp_seen.write_text(json.dumps(sorted(seen), ensure_ascii=False), "utf-8")
                tmp_seen.replace(seen_file)
            self._update(task_id, status="completed", progress=100, current=len(urls), message=message, result=result)
        except Exception as exc:
            self._update(task_id, status="failed", message="采集失败", error=str(exc), result={"failed_count": len(errors), "errors": errors})
        finally:
            self.controls.pop(task_id, None)
            self.active_runs.pop(task_id, None)

    def control(self, task_id, action):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            control = self.controls.setdefault(task_id, {"paused": False, "cancelled": False})
            request = task.get("request", {})
            scheduled = bool(request.get("cron") or int(request.get("interval_minutes") or 0) > 0)
            if action == "pause_schedule":
                if not scheduled or task.get("schedule_paused"):
                    raise ValueError("当前任务不支持暂停定时调度")
                task["schedule_paused"] = True
                if task.get("status") == "scheduled":
                    task["message"] = "定时任务已暂停"
            elif action == "resume_schedule":
                if not scheduled or not task.get("schedule_paused"):
                    raise ValueError("当前任务不支持恢复定时调度")
                task["schedule_paused"] = False
                cron = str(request.get("cron") or "")
                interval = int(request.get("interval_minutes") or 0)
                task["next_run_at"] = (next_runs(cron, count=1)[0] if cron else
                                       (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat())
                if task.get("status") == "scheduled":
                    task["message"] = "等待定时执行"
            elif action == "pause" and task["status"] in ("queued", "running"):
                control["paused"] = True; task.update(status="paused", message="任务已暂停")
            elif action == "resume" and task["status"] == "paused":
                control["paused"] = False; task.update(status="running", message="任务继续执行")
            else:
                raise ValueError("当前状态不支持此操作")
            task["updated_at"] = _now()
            run_id = self.active_runs.get(task_id)
            for run in reversed(task.get("runs") or []):
                if run.get("id") == run_id:
                    run.update(status=task["status"], message=task["message"], updated_at=task["updated_at"])
                    break
            self._save(); return task

    def retry(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task: return None
            if task.get("status") not in ("failed", "completed"): raise ValueError("仅失败或已完成任务可以重新采集")
            task["_retry_youtube_targets"] = task.get("request", {}).get("source") == "youtube"
            task["_run_trigger"] = "retry"
            task.update(status="queued", progress=0, current=0, message="等待重新采集", error=None, result=None, updated_at=_now())
            self.controls[task_id] = {"paused": False, "cancelled": False}; self._save()
        task_dir = self.data_dir / task_id
        if task_dir.is_dir():
            # 历史执行结果和增量去重状态属于任务本身，重新执行时必须保留。
            for path in task_dir.iterdir():
                preserved = {"runs", "seen_urls.json", "wechat_discovery_cache.json"}
                if task.get("request", {}).get("source") == "youtube":
                    preserved.update(("videos", "youtube_targets.json"))
                if path.name in preserved:
                    continue
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        self.pool.submit(self._run, task_id); return task

    def delete(self, task_id):
        with self.lock:
            task = self.tasks.pop(task_id, None)
            if not task: return False
            if task_id in self.controls: self.controls[task_id]["cancelled"] = True
            self._save()
        shutil.rmtree(self.data_dir / task_id, ignore_errors=True); return True

    def _schedule_loop(self):
        while True:
            time.sleep(15)
            now = time.time()
            with self.lock:
                scheduled_tasks = list(self.tasks.values())
            for task in scheduled_tasks:
                interval = int(task.get("request", {}).get("interval_minutes") or 0)
                cron = task.get("request", {}).get("cron", "")
                if task.get("schedule_paused") or (not cron and interval <= 0) or task.get("status") not in ("scheduled", "completed", "failed"):
                    continue
                try: due = datetime.fromisoformat(task.get("next_run_at", "")).timestamp()
                except (TypeError, ValueError): due = 0
                if due <= now:
                    with self.lock:
                        if self.tasks.get(task["id"]) is not task:
                            continue
                        task["status"] = "queued"; task["progress"] = 0; task["current"] = 0; task["message"] = "定时任务等待执行"; task["next_run_at"] = next_runs(cron, count=1)[0] if cron else datetime.fromtimestamp(now + interval * 60, timezone.utc).isoformat(); task["_run_trigger"] = "scheduled"; self.controls[task["id"]] = {"paused": False, "cancelled": False}; self._save()
                    self.pool.submit(self._run, task["id"])
