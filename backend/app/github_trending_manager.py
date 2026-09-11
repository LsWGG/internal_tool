import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


class GitHubTrendingManager:
    URL = "https://github.com/trending?since=daily"

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
    def _translate_descriptions(repositories):
        targets = [(index, item["description"]) for index, item in enumerate(repositories)
                   if item["description"] != "暂无简介"]
        for item in repositories:
            item["description_zh"] = item["description"]
        if not targets:
            return {"status": "completed", "translated_count": 0}
        translated_count = 0

        def translate(item):
            index, text = item
            response = requests.get("https://translate.googleapis.com/translate_a/single", params={
                "client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text,
            }, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            payload = response.json()
            value = "".join(part[0] for part in payload[0] if part and part[0])
            return index, value.strip()

        errors = []
        try:
            with ThreadPoolExecutor(max_workers=5, thread_name_prefix="github-translate") as pool:
                futures = [pool.submit(translate, item) for item in targets]
                for future in as_completed(futures):
                    try:
                        index, value = future.result()
                    except Exception as exc:
                        errors.append(str(exc))
                        continue
                    if value and value.strip():
                        repositories[index]["description_zh"] = value.strip()
                        translated_count += 1
            return {"status": "completed" if not errors else "fallback",
                    "translated_count": translated_count,
                    **({"message": errors[0][:300]} if errors else {})}
        except Exception as exc:
            return {"status": "fallback", "translated_count": translated_count, "message": str(exc)[:300]}

    def _cleanup(self):
        files = sorted(self.data_dir.glob("????-??-??.json"), reverse=True)
        for path in files[7:]:
            path.unlink(missing_ok=True)

    def dates(self):
        with self.lock:
            result = []
            for path in sorted(self.data_dir.glob("????-??-??.json"), reverse=True)[:7]:
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
