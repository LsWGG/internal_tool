"""合规的智能网页采集任务管理器。

采集器面向公开页面：使用请求限速、重试和可选代理轮换，
不绕过验证码、登录墙或其他访问控制。字段解析支持 CSS 选择器、OpenGraph/
JSON-LD 和内置数据源模板；“AI 解析”在本地用页面语义规则生成建议字段，
若配置了兼容 OpenAI API 的服务，可由前端另行接入二次解析。
"""
from __future__ import annotations

import csv
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
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from .crawler_adapters import douyin, get_adapter, news, telegram, tiktok, twitter, wechat, youtube
from .crawler_adapters.base import DiscoverContext, ManagerHttpBridge
# 这些纯函数搬到了适配器层，管理器继续以原名使用，避免大面积改名。
from .crawler_adapters.util import (
    clean_url, decode_response as _decode_response,
    normalize_row_datetimes as _normalize_row_datetimes, safe_url as _safe_url,
    structured_text as _structured_text, text as _text)
from .crawler_browser import CrawlerBrowser, browser_scope
from .crawler_fields import (BUILTIN_FIELDS, TELEGRAM_MEMBER_FIELDS, TIKTOK_COMMENT_FIELDS,
                             TIKTOK_USER_FIELDS, TWITTER_USER_FIELDS,
                             YOUTUBE_COMMENT_FIELDS, YOUTUBE_USER_FIELDS)
from .crawler_http import UA_DESKTOP_FULL, CrawlerHttpClient, default_client
from .crawler_schedule import next_runs
from .crawler_secrets import GLOBAL_COOKIE_TASK_ID, load_secrets, write_secret
from .crawler_sql_sink import validate_config as validate_sql_sink, write_rows as write_sql_rows


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


def _now():
    return datetime.now(timezone.utc).isoformat()


class CrawlerTaskManager:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / "tasks.json"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="crawler-task")
        self.controls = {}
        self.task_secrets = {}
        self.active_runs = {}
        self._dynamic_hosts = set()
        self.http = CrawlerHttpClient()
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
        request = task.get("request") or {}
        source = request.get("source")
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
        if not rows and source in ("tiktok", "douyin") and request.get("tiktok_mode") == "comments":
            # 评论接口受限时仍返回视频占位行，允许用户继续配置字段和创建任务。
            #
            # 这个分支此前引用的是方法里根本没有的 source/request/targets/names，
            # 走到这儿必崩 NameError；现在按任务请求把目标和字段重新解出来
            # （与 _discover_tiktok_comments 用 _tiktok_video_url 取视频链接同源）。
            names = [str(field.get("name") if isinstance(field, dict) else field).strip()
                     for field in request.get("fields") or []]
            names = [name for name in dict.fromkeys(names) if name]
            values = [request.get("keyword") or "", *(request.get("urls") or [])]
            rows = []
            for value in values:
                # 只认解析得出的视频链接：解不出来的值（比如用户填了搜索词）不能
                # 硬塞进 url 列冒充一条数据。
                target = self._tiktok_video_url(value)
                if not target:
                    continue
                row = {name: "" for name in names}
                row["url"] = target          # 占位行至少要带上视频地址
                rows.append(row)
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
        for key in ("telegram_api_hash", "telegram_session", "douyin_cookie"):
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
    def builtin_fields(source, twitter_mode=None, tiktok_mode=None, telegram_mode=None, youtube_mode=None):
        if source == "twitter" and twitter_mode == "user":
            names = TWITTER_USER_FIELDS
        elif source in ("tiktok", "douyin") and tiktok_mode == "user":
            names = TIKTOK_USER_FIELDS
        elif source in ("tiktok", "douyin") and tiktok_mode == "comments":
            names = TIKTOK_COMMENT_FIELDS
        elif source == "youtube" and youtube_mode == "user":
            names = YOUTUBE_USER_FIELDS
        elif source == "youtube" and youtube_mode == "comments":
            names = YOUTUBE_COMMENT_FIELDS
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
        youtube_mode = str(payload.get("youtube_mode") or "keyword").strip().lower()
        if youtube_mode not in ("keyword", "comments", "user", "videos"):
            youtube_mode = "keyword"
        telegram_mode = str(payload.get("telegram_mode") or "channel").strip().lower()
        if telegram_mode not in ("channel", "group", "members", "search"):
            telegram_mode = "channel"
        if source == "wechat" and not account_name and not urls:
            raise ValueError("请输入公众号名称或微信文章链接")
        if source in ("twitter", "youtube", "tiktok", "douyin", "telegram") and not keyword:
            if source == "twitter" and twitter_mode != "keyword":
                message = "请输入 X 账号"
            elif source in ("tiktok", "douyin"):
                platform = "抖音" if source == "douyin" else "TikTok"
                message = {"user": f"请输入 {platform} 账号", "videos": f"请输入 {platform} 账号",
                           "comments": f"请输入 {platform} 视频链接"}.get(tiktok_mode, f"请输入 {platform} 搜索关键词")
            elif source == "youtube":
                message = {"user": "请输入 YouTube 账号", "videos": "请输入 YouTube 账号", "comments": "请输入 YouTube 视频链接"}.get(youtube_mode, "请输入 YouTube 搜索关键词")
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
            fields = self.builtin_fields(source, twitter_mode, tiktok_mode, telegram_mode, youtube_mode)
        proxies = [str(x).strip() for x in (payload.get("proxies") or []) if str(x).strip()]
        douyin_cookie = str(payload.get("douyin_cookie") or "").strip()
        if source == "douyin" and not douyin_cookie:
            douyin_cookie = self._saved_douyin_cookie()
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
        youtube_label = {"keyword": "YouTube 关键词视频", "comments": "YouTube 视频评论", "user": "YouTube 账号信息", "videos": "YouTube 账号视频"}.get(youtube_mode)
        telegram_label = {"channel": "Telegram 频道", "group": "Telegram 群组", "members": "Telegram 群组成员", "search": "Telegram 全平台搜索"}.get(telegram_mode)
        label = payload.get("name") or (f"{twitter_label} · {label_target}" if source == "twitter" else (f"{tiktok_label} · {label_target}" if source in ("tiktok", "douyin") else (f"{youtube_label} · {label_target}" if source == "youtube" else (f"{telegram_label} · {label_target}" if source == "telegram" else f"{SOURCE_NAMES.get(source, '网页')}采集 · {label_target}"))))
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
                              "youtube_mode": youtube_mode,
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
            if douyin_cookie:
                # Login state is only needed at execution time. Never write it
                # into tasks.json, exports, or API task responses.
                self.task_secrets[task_id] = {"douyin_cookie": douyin_cookie}
                # 同时落盘一份（0o600），否则服务重启后任务会静默降级成匿名请求。
                write_secret(self.data_dir, task_id, "douyin_cookie", douyin_cookie)
            self.controls[task_id] = {"paused": False, "cancelled": False}
            self._save()
        if cron:
            self._update(task_id, status="scheduled", message="等待定时执行")
        else:
            self.pool.submit(self._run, task_id)
        return self.public_task(record)

    def _request(self, url, proxies, timeout=30, session=None, headers=None,
                 retries=None, rate=True):
        """请求咽喉：真正的行为在 `CrawlerHttpClient`（重试退避、按域名限流、代理轮换）。

        这里刻意保留成一层薄委托 —— 测试会整体替换 `manager._request` 来隔离网络，
        调用点也全部经由它，行为改动只需要落在 `crawler_http` 一个地方。
        """
        client = getattr(self, "http", None) or default_client()
        return client.get(url, proxies or [], timeout=timeout, session=session,
                          headers=headers, retries=retries, rate=rate)

    @staticmethod
    def _clean_url(value):
        """微信等调用点仍在用；实现在 crawler_adapters/util.py。"""
        return clean_url(value)

    # 新闻的实现已经搬到 crawler_adapters/news.py，这里只留委托，
    # 既有调用方（预览、任务运行、测试里的 patch）照旧。
    def _article_links(self, page_url, soup, selector=""):
        return news.article_links(page_url, soup, selector)

    def _next_news_page(self, page_url, soup, selector=""):
        return news.next_news_page(page_url, soup, selector)

    def _fetch_listing_html(self, url, request, browser=None):
        return news.listing_html(url, request, browser, ManagerHttpBridge(self))

    def _discover_news_urls(self, seeds, request, task_id=None, browser=None):
        return news.discover(seeds, self._discover_context(request, task_id, browser))
    # 微信的实现已经搬到 crawler_adapters/wechat.py，这里只留委托，
    # 既有调用方（预览、任务运行、测试里的 patch）照旧。
    def _resolve_sogou_wechat_url(self, href, request, session=None, referer=""):
        return wechat.resolve_sogou_url(href, request, session, referer, ManagerHttpBridge(self))

    def _discover_wechat_sogou_urls(self, account_name, request, task_id=None):
        return wechat.sogou_urls(account_name, self._discover_context(request, task_id))

    def _discover_wechat_mobile_urls(self, account_name, request, task_id=None):
        return wechat.mobile_urls(account_name, self._discover_context(request, task_id))

    def _discover_wechat_public_search_urls(self, account_name, request, task_id=None, browser=None):
        return wechat.public_search_urls(account_name, self._discover_context(request, task_id, browser))

    def _discover_wechat_urls(self, account_name, request, task_id=None, browser=None):
        return wechat.discover(account_name, self._discover_context(request, task_id, browser))


    def _discover_context(self, request, task_id=None, browser=None):
        """适配器与管理器之间的边界对象。

        data_dir 走 getattr：测试里的 manager 替身只补它关心的属性，
        不该为了构造这个边界对象被迫凑齐全部字段。
        """
        return DiscoverContext(task_id=task_id, request=request, browser=browser,
                               http=ManagerHttpBridge(self), update=self._update,
                               data_dir=getattr(self, "data_dir", None),
                               checkpoint=self._checkpoint, fetch_html=self._fetch_html,
                               fetch_listing_html=self._fetch_listing_html)

    def _discover_for(self, source, value, request, task_id=None, browser=None):
        """关键词类数据源的发现入口：全部走适配器协议。"""
        adapter = get_adapter(source)
        if adapter.discover is None:
            raise RuntimeError(f"{source} 没有可用的发现实现")
        return adapter.discover(value, self._discover_context(request, task_id, browser))

    # YouTube 的实现已经搬到 crawler_adapters/youtube.py，这里只留委托，
    # 既有调用方（预览、任务运行、测试里的 patch）照旧。
    def _discover_youtube_urls(self, keyword, request, task_id=None):
        return youtube.search_urls(keyword, self._discover_context(request, task_id))

    def _discover_youtube_data(self, value, request, task_id=None):
        return youtube.discover(value, self._discover_context(request, task_id))

    # X / Twitter 的实现已经搬到 crawler_adapters/twitter.py，这里只留委托。
    def _discover_twitter_posts(self, keyword, request, task_id=None):
        return twitter.posts(keyword, self._discover_context(request, task_id))

    # TikTok / 抖音的实现已经搬到 crawler_adapters/tiktok.py 与 douyin.py，这里只留委托，
    # 既有调用方（预览、任务运行、测试里的 patch）照旧。
    @staticmethod
    def _tiktok_username(value):
        return tiktok.username(value)

    @staticmethod
    def _tiktok_video_url(value):
        return tiktok.video_url(value)

    @staticmethod
    def _tiktok_video_row(info):
        return tiktok.video_row(info)

    @staticmethod
    def _tiktok_user_from_html(username, html):
        return tiktok.user_from_html(username, html)

    @staticmethod
    def _tiktok_comment_rows(items, video_url):
        return tiktok.comment_rows(items, video_url)

    def _tiktok_extract(self, url, request, flat=False):
        return tiktok.extract(url, request, flat)

    def _tiktok_oembed_row(self, url, request):
        return tiktok.oembed_row(url, request, ManagerHttpBridge(self))

    def _discover_tiktok_index_urls(self, keyword, request, maximum):
        return tiktok.index_urls(keyword, request, maximum, ManagerHttpBridge(self))

    def _discover_tiktok_browser_urls(self, keyword, request, maximum, browser=None):
        return tiktok.browser_urls(keyword, request, maximum, browser)

    def _discover_tiktok_user(self, account, request, task_id=None, browser=None):
        return tiktok.user(account, self._discover_context(request, task_id, browser))

    def _discover_tiktok_account_videos(self, account, request, task_id=None):
        return tiktok.account_videos(account, self._discover_context(request, task_id))

    def _discover_tiktok_keyword_videos(self, keyword, request, task_id=None, browser=None):
        return tiktok.keyword_videos(keyword, self._discover_context(request, task_id, browser))

    def _discover_tiktok_comments(self, value, request, task_id=None, browser=None):
        return tiktok.comments(value, self._discover_context(request, task_id, browser))

    def _discover_douyin_data(self, value, request, task_id=None, browser=None):
        return douyin.discover(value, self._discover_context(request, task_id, browser))

    def _discover_tiktok_data(self, value, request, task_id=None, browser=None):
        """tiktok 与 douyin 共用一个入口；抖音的分发实现在 crawler_adapters/douyin.py。"""
        if request.get("source") == "douyin":
            return self._discover_douyin_data(value, request, task_id, browser=browser)
        return tiktok.discover(value, self._discover_context(request, task_id, browser))


    # Telegram 的实现已经搬到 crawler_adapters/telegram.py，这里只留委托。
    @staticmethod
    def _telegram_target(value):
        return telegram.parse_target(value)

    @staticmethod
    def _telegram_public_rows(html, chat, maximum):
        return telegram.public_rows(html, chat, maximum)

    def _telegram_credentials(self, request):
        return telegram.credentials(request)

    def _discover_telegram_data(self, value, request, task_id=None):
        return telegram.discover(value, self._discover_context(request, task_id))

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
        # 点选是交互路径，一次调用里每一步都可能要动态渲染 —— 收敛成同一个浏览器，
        # 用户点一次「预览」不再冷启动好几次 Chromium。
        with browser_scope(None, request.get("proxies")) as preview_browser:
            if source == "twitter":
                raise ValueError("X 数据通过公开索引和官方嵌入页面返回固定字段，无需网页点选")
            if source == "youtube":
                if not keyword:
                    raise ValueError("请先输入 YouTube 搜索关键词")
                url = self._discover_youtube_urls(keyword, request)[0]
                request["dynamic"] = True
            elif source == "news":
                url = _safe_url(url)
                soup = BeautifulSoup(self._fetch_listing_html(url, request, browser=preview_browser),
                                     "html.parser")
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
                    articles = self._discover_wechat_urls(account_name, request,
                                                          browser=preview_browser)
                    for item in articles:
                        candidate = str(item.get("detail_url") or "")
                        if urlparse(candidate).hostname != "mp.weixin.qq.com":
                            continue
                        try:
                            candidate_html = self._fetch_html(candidate, request,
                                                              browser=preview_browser)
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
            html = loaded_html if loaded_html is not None else self._fetch_html(
                url, request, browser=preview_browser)
            soup = BeautifulSoup(html, "html.parser")
            # 普通静态请求若只拿到“载入中”骨架，自动用浏览器完成一次渲染，
            # 无需非技术用户预先知道并勾选“动态内容”。
            if source not in ("youtube", "wechat") and not request.get("dynamic") \
                    and self._preview_is_loading_shell(soup):
                dynamic_request = {**request, "dynamic": True}
                html = self._fetch_html(url, dynamic_request, browser=preview_browser)
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
        douyin_cookie = str(payload.get("douyin_cookie") or "").strip()
        if source == "douyin" and not douyin_cookie:
            douyin_cookie = self._saved_douyin_cookie()
        request = {
            "source": source, "urls": urls, "keyword": keyword, "account_name": account_name,
            "twitter_mode": str(payload.get("twitter_mode") or "keyword"),
            "tiktok_mode": str(payload.get("tiktok_mode") or "keyword"),
            "youtube_mode": str(payload.get("youtube_mode") or "keyword"),
            "telegram_mode": str(payload.get("telegram_mode") or "channel"),
            "telegram_api_id": str(payload.get("telegram_api_id") or "").strip(),
            "telegram_api_hash": str(payload.get("telegram_api_hash") or "").strip(),
            "telegram_session": str(payload.get("telegram_session") or "").strip(),
            "douyin_cookie": douyin_cookie,
            "proxies": payload.get("proxies") or [], "dynamic": bool(payload.get("dynamic")),
            "max_items": sample_size,
            "delay_seconds": max(0.2, float(payload.get("delay_seconds") or 0.4)),
            "article_link_selector": str(payload.get("article_link_selector") or "").strip(),
            "next_page_selector": str(payload.get("next_page_selector") or "").strip(),
        }
        # 前端明确传入 fields（包括空数组）时必须严格遵守，不能用默认字段
        # 回填，否则“选择要保存的内容”和预览会出现不一致。
        fields = payload.get("fields") if "fields" in payload else self.builtin_fields(
            source, request["twitter_mode"], request["tiktok_mode"], request["telegram_mode"], request["youtube_mode"]
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
                platform = "抖音" if source == "douyin" else "TikTok"
                message = {"user": f"请先输入 {platform} 账号", "videos": f"请先输入 {platform} 账号",
                           "comments": f"请先输入 {platform} 视频链接"}.get(
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

        # 同 preview：发现与取样共用一次调用里的同一个浏览器。
        with browser_scope(None, request.get("proxies")) as preview_browser:
            if source == "news":
                targets = self._discover_news_urls(urls, request, browser=preview_browser)[:sample_size]
            elif source == "wechat":
                targets = []
                if account_name:
                    targets.extend(self._discover_wechat_urls(account_name, request,
                                                              browser=preview_browser)[:sample_size])
                known = {item.get("url") for item in targets if isinstance(item, dict)}
                targets.extend(url for url in urls if url not in known)
                targets = targets[:sample_size]
            elif source in ("youtube", "twitter", "telegram"):
                targets = self._discover_for(source, keyword, request,
                                             browser=preview_browser)[:sample_size]
            elif source in ("tiktok", "douyin"):
                try:
                    targets = self._discover_for(source, keyword, request,
                                                 browser=preview_browser)[:sample_size]
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
                        html = self._fetch_html(url, request, browser=preview_browser)
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
        youtube_values = youtube.page_values(url, page_source)
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

    def _fetch_html(self, url, request, browser=None):
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
            with browser_scope(browser, request.get("proxies")) as engine:
                context = engine.new_context(
                    user_agent=UA_DESKTOP_FULL,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1000},
                )
                try:
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
                    context.close()
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
        """实现已搬到 crawler_adapters/youtube.py，下载路径仍从这里调用。"""
        return youtube.fill_row(row, info)

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

    def _saved_douyin_cookie(self):
        """扫码登录存下来的那份抖音 cookie（没有就是空串）。

        每次都读盘、不走 `_secrets_for` 的内存缓存：用户重新扫码，图的就是覆盖掉
        那份过期的旧值，缓存会让刷新看不见。
        """
        return str(load_secrets(self.data_dir, GLOBAL_COOKIE_TASK_ID).get("douyin_cookie") or "")

    def _secrets_for(self, task_id):
        """任务的登录态：内存优先，内存没有就回落到磁盘。

        回落到磁盘是服务重启后的正路 —— cookie 不再随内存一起消失。读回来的会回填进
        `task_secrets`，省掉之后每次运行都再读一遍文件。
        """
        known = getattr(self, "task_secrets", {}).get(task_id)
        if known:
            return known
        restored = load_secrets(self.data_dir, task_id)
        if restored:
            if getattr(self, "task_secrets", None) is None:
                self.task_secrets = {}
            self.task_secrets[task_id] = restored
        return restored

    def _run(self, task_id):
        run_id = self._start_run(task_id)
        task_dir = self.data_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task = self.tasks.get(task_id) or {}
        reuse_youtube_targets = task.pop("_retry_youtube_targets", False)
        request = {**task.get("request", {}), **self._secrets_for(task_id)}
        # 一次任务共用一个采集浏览器：进程只在第一次真需要渲染时才启动，
        # 任务内几十个页面不再各自冷启动一次 Chromium。
        run_browser = CrawlerBrowser(request.get("proxies"))
        rows, errors = [], []
        try:
            source = request.get("source", "generic")
            download_videos = bool(request.get("download_videos", True)) and ((source == "youtube" and request.get("youtube_mode", "keyword") in ("keyword", "videos")) or (source in ("tiktok", "douyin") and request.get("tiktok_mode") in ("keyword", "videos")))
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
                urls = self._discover_news_urls(request.get("urls", []), request, task_id,
                                                browser=run_browser)
            elif source == "wechat":
                direct_urls = request.get("urls", [])
                discovered = []
                if request.get("account_name"):
                    try:
                        discovered = self._discover_wechat_urls(request.get("account_name", ""), request, task_id,
                                                                browser=run_browser)
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
                    urls = self._discover_for(source, request.get("keyword", ""), request, task_id,
                                              run_browser)
                    targets_file.write_text(json.dumps(urls, ensure_ascii=False), "utf-8")
            elif source in ("twitter", "tiktok", "douyin", "telegram"):
                urls = self._discover_for(source, request.get("keyword", ""), request, task_id,
                                          run_browser)
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
                    html = self._fetch_html(url, request, browser=run_browser)
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
            elif errors:
                # 非下载场景过去只报成功条数，个别 URL 抓失败就静默了 —— 用户看到
                # 「采集完成」会以为全都拿到了。逐条明细仍在 result.errors 里。
                message = f"采集完成 · 成功 {len(rows)} 条，失败 {len(errors)} 条"
            if int(request.get("max_items") or 50) == -1:
                tmp_seen = seen_file.with_suffix(".tmp")
                tmp_seen.write_text(json.dumps(sorted(seen), ensure_ascii=False), "utf-8")
                tmp_seen.replace(seen_file)
            self._update(task_id, status="completed", progress=100, current=len(urls), message=message, result=result)
        except Exception as exc:
            self._update(task_id, status="failed", message="采集失败", error=str(exc), result={"failed_count": len(errors), "errors": errors})
        finally:
            self.controls.pop(task_id, None)
            run_browser.close()
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
            self.task_secrets.pop(task_id, None)
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
