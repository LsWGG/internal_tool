import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .model_settings import resolve as resolve_model

logger = logging.getLogger(__name__)


class GitHubTrendingManager:
    URL = "https://github.com/trending?since=daily"
    # 日报历史保留天数，也是接口返回的报告条数上限。
    RETENTION_DAYS = 15
    # 翻译走大模型：一次请求翻完整份榜单，超时按整批算。
    # 连接超时给得比接口本身的响应时间宽松：这台机器的外网会短暂抽风，
    # 10 秒连不上就整份不翻太亏，翻译慢一点无所谓。
    TRANSLATION_TIMEOUT = (20, 180)
    # 外网偶发抽风是常态，重试带退避，别两次撞在一起白等一轮。
    TRANSLATION_ATTEMPTS = 3
    TRANSLATION_BACKOFF = (1.0, 3.0)

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.timezone = ZoneInfo("Asia/Shanghai")
        self._started = False

    def _today(self):
        return datetime.now(self.timezone).date().isoformat()

    def _path(self, date):
        return self.data_dir / f"{date}.json"

    @property
    def favorites_path(self):
        return self.data_dir / "favorites.json"

    def favorites(self):
        with self.lock:
            try:
                items = json.loads(self.favorites_path.read_text("utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return []
            return items if isinstance(items, list) else []

    def save_favorite(self, repository, source_date=""):
        full_name = str((repository or {}).get("full_name") or "").strip()
        url = str((repository or {}).get("url") or "").strip()
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", full_name) or not url.startswith("https://github.com/"):
            raise ValueError("仓库信息无效")
        allowed = ("name", "full_name", "url", "description", "description_zh", "language",
                   "stars", "forks", "stars_today")
        item = {key: repository.get(key) for key in allowed if repository.get(key) is not None}
        item.update({"full_name": full_name, "url": url, "source_date": str(source_date or "")[:10],
                     "favorited_at": datetime.now(self.timezone).isoformat()})
        with self.lock:
            items = self.favorites()
            previous = next((value for value in items if value.get("full_name") == full_name), None)
            if previous:
                item["favorited_at"] = previous.get("favorited_at") or item["favorited_at"]
            items = [value for value in items if value.get("full_name") != full_name]
            items.insert(0, item)
            temporary = self.favorites_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
            temporary.replace(self.favorites_path)
        return item

    def remove_favorite(self, owner, name):
        full_name = f"{owner}/{name}"
        with self.lock:
            items = self.favorites()
            remaining = [item for item in items if item.get("full_name") != full_name]
            if len(remaining) == len(items):
                return False
            temporary = self.favorites_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), "utf-8")
            temporary.replace(self.favorites_path)
            return True

    @staticmethod
    def parse(html):
        soup = BeautifulSoup(html, "html.parser")
        repositories = []
        for rank, article in enumerate(soup.select("article.Box-row"), 1):
            link = article.select_one("h2 a")
            if not link:
                continue
            full_name = re.sub(r"\s+", "", link.get_text(" ", strip=True)).strip("/")
            if "/" not in full_name:
                continue
            description = article.select_one("p")
            language = article.select_one("[itemprop=programmingLanguage]")
            counters = article.select("a.Link--muted")
            today = article.select_one("span.d-inline-block.float-sm-right")
            number = lambda node: re.sub(r"[^0-9,]", "", node.get_text(" ", strip=True)) if node else ""
            repositories.append({
                "rank": rank, "name": full_name.split("/", 1)[1], "full_name": full_name,
                "url": "https://github.com/" + full_name,
                "description": description.get_text(" ", strip=True) if description else "暂无简介",
                "language": language.get_text(" ", strip=True) if language else "未标注",
                "stars": number(counters[0]) if counters else "0",
                "forks": number(counters[1]) if len(counters) > 1 else "0",
                "stars_today": number(today) if today else "0",
            })
        if not repositories:
            raise ValueError("GitHub Trending 页面中未解析到仓库，页面结构可能已变化")
        return repositories

    def refresh(self):
        if not self.refresh_lock.acquire(blocking=False):
            raise RuntimeError("热门报告正在生成，请稍后刷新")
        try:
            response = requests.get(self.URL, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 GitHubTrendingReport/1.0",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            response.raise_for_status()
            repositories = self.parse(response.text)
            translated = self._translate_descriptions(repositories)
            generated_at = datetime.now(self.timezone).isoformat()
            report = {"date": self._today(), "generated_at": generated_at,
                      "title": f"GitHub 每日热门榜单 · {self._today()}",
                      "repository_count": len(repositories), "translation": translated,
                      "repositories": repositories}
            with self.lock:
                self._path(report["date"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
                self._cleanup()
            return report
        finally:
            self.refresh_lock.release()

    @staticmethod
    def _translation_pairs(content):
        """从模型回复里取出 {编号: 中文}，容忍代码块包裹和个别坏行。"""
        text = str(content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"[\[{].*[\]}]", text, re.S)
            if not match:
                return {}
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
        items = payload.get("translations") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return {}
        pairs = {}
        for entry in items:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("id"))
            except (TypeError, ValueError):
                continue
            value = str(entry.get("zh") or "").strip()
            if value:
                pairs[index] = value
        return pairs

    @classmethod
    def _translate_descriptions(cls, repositories):
        """用大模型把仓库简介译成中文；整批一次请求。

        失败不阻断日报：译不出来的行原样保留英文，调用方据 status 决定是否提示。
        """
        targets = [(index, item["description"]) for index, item in enumerate(repositories)
                   if item["description"] != "暂无简介"]
        for item in repositories:
            item["description_zh"] = item["description"]
        if not targets:
            return {"status": "completed", "translated_count": 0}
        try:
            url, api_key, model = resolve_model("assistant")
        except Exception as exc:
            return {"status": "fallback", "translated_count": 0,
                    "message": f"读取大模型配置失败：{str(exc)[:200]}"}
        if not url or not model:
            return {"status": "fallback", "translated_count": 0,
                    "message": "未配置大模型，本次报告未翻译。请在「模型设置」中为 assistant 用途填写接口地址与模型名。"}
        prompt = (
            "把下面每一行 GitHub 仓库简介翻译成简体中文。\n"
            "每行格式是「编号<TAB>英文原文」，请逐行对应翻译。\n"
            '只输出 JSON 对象：{"translations": [{"id": 编号, "zh": "中文翻译"}]}。\n'
            "编号原样保留，不要合并、跳过或新增任何一行。\n"
            "项目名、技术术语、缩写保留英文原样（如 Kubernetes、LLM、CLI、API）。\n"
            "译文要简洁准确，符合中文技术社区的表达习惯。\n\n"
            + "\n".join(f"{index}\t{text}" for index, text in targets)
        )
        body = {"model": model, "temperature": 0.2, "max_tokens": 4096,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "你是资深技术翻译，只输出 JSON。"},
                             {"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        failure = ""
        # 大模型偶发空返回、截断，或者外网抽风；批量请求本身很便宜，重试带退避。
        for attempt in range(cls.TRANSLATION_ATTEMPTS):
            if attempt:
                time.sleep(cls.TRANSLATION_BACKOFF[min(attempt - 1, len(cls.TRANSLATION_BACKOFF) - 1)])
            try:
                response = requests.post(url, headers=headers, json=body,
                                         timeout=cls.TRANSLATION_TIMEOUT)
                response.raise_for_status()
                content = (response.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
                pairs = cls._translation_pairs(content)
                for index, value in pairs.items():
                    if 0 <= index < len(repositories):
                        repositories[index]["description_zh"] = value
                translated_count = sum(1 for index, text in targets
                                       if repositories[index].get("description_zh") not in (text, None, ""))
                if translated_count == len(targets):
                    return {"status": "completed", "translated_count": translated_count}
                if translated_count:
                    return {"status": "fallback", "translated_count": translated_count,
                            "message": f"大模型只返回了 {translated_count}/{len(targets)} 条翻译，其余保留英文原文。"}
                failure = "大模型没有返回可用的翻译内容" if not pairs else "大模型返回的翻译条目编号无法对应"
                logger.warning("热门日报翻译第 %s 次尝试未取到可用的翻译内容", attempt + 1)
            except requests.RequestException as exc:
                # 异常原文必须落日志：页面上只说「请求失败或超时」时，没有这行就无从查起。
                logger.warning("热门日报翻译第 %s 次请求失败（接口 %s）", attempt + 1, url, exc_info=True)
                failure = cls._request_failure_message(exc)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                logger.warning("热门日报翻译第 %s 次返回格式不兼容", attempt + 1, exc_info=True)
                failure = f"大模型返回格式不兼容：{str(exc)[:160]}"
        return {"status": "fallback", "translated_count": 0, "message": failure[:300]}

    @classmethod
    def _request_failure_message(cls, exc):
        """把连接类异常说成人能看懂的一句话，不泄露密钥。"""
        if isinstance(exc, requests.Timeout):
            return (f"大模型请求超时：{cls.TRANSLATION_TIMEOUT[0]} 秒内没连上或 "
                    f"{cls.TRANSLATION_TIMEOUT[1]} 秒内没返回，已重试 {cls.TRANSLATION_ATTEMPTS} 次。请检查网络或「模型设置」里的接口地址。")
        status = exc.response.status_code if exc.response is not None else None
        if status:
            return f"大模型请求失败：HTTP {status}。请检查「模型设置」里的接口地址、密钥和模型名。"
        reason = str(exc).split(":")[0].strip() or exc.__class__.__name__
        return f"大模型连接失败（{reason}）：已重试 {cls.TRANSLATION_ATTEMPTS} 次，请检查网络或接口地址。"

    def _cleanup(self):
        files = sorted(self.data_dir.glob("????-??-??.json"), reverse=True)
        for path in files[self.RETENTION_DAYS:]:
            path.unlink(missing_ok=True)

    def dates(self):
        with self.lock:
            result = []
            for path in sorted(self.data_dir.glob("????-??-??.json"), reverse=True)[:self.RETENTION_DAYS]:
                try:
                    report = json.loads(path.read_text("utf-8"))
                    result.append({"date": report["date"], "generated_at": report["generated_at"],
                                   "repository_count": report["repository_count"]})
                except (json.JSONDecodeError, KeyError):
                    continue
            return result

    def get(self, date):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return None
        try:
            return json.loads(self._path(date).read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def start_daily(self):
        with self.lock:
            if self._started:
                return
            self._started = True

        def run():
            while True:
                try:
                    if not self._path(self._today()).exists():
                        self.refresh()
                except Exception as exc:
                    print(f"[GitHub Trending] 生成日报失败：{exc}")
                now = datetime.now(self.timezone)
                tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), self.timezone)
                threading.Event().wait(max(60, (tomorrow - now).total_seconds() + 60))

        threading.Thread(target=run, name="github-trending-daily", daemon=True).start()
