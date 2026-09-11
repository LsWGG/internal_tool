"""北京时间的五段 Cron 校验、预览和自然语言转换。"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from croniter import croniter

TZ = ZoneInfo("Asia/Shanghai")


def next_runs(expression, base=None, count=3):
    expression = str(expression or "").strip()
    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ValueError("请输入有效的五段 Cron：分 时 日 月 周")
    try:
        iterator = croniter(expression, base or datetime.now(TZ))
        return [iterator.get_next(datetime).isoformat() for _ in range(count)]
    except (ValueError, OverflowError) as exc:
        raise ValueError("该 Cron 表达式没有可执行的日期") from exc


def describe(payload):
    expression = str(payload.get("cron") or "").strip()
    text = str(payload.get("text") or "").strip()
    if text:
        if len(text) > 1000:
            raise ValueError("时间描述请控制在 1000 字以内")
        url, key = os.getenv("CRAWLER_LLM_URL"), os.getenv("CRAWLER_LLM_API_KEY")
        if not url or not key:
            raise ValueError("尚未配置大模型，请手动填写 Cron 表达式")
        try:
            response = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json={
                "model": os.getenv("CRAWLER_LLM_MODEL", "glm-4-flash"),
                "messages": [
                    {"role": "system", "content": '将时间描述转为北京时间下的标准五段 crontab，周日=0。仅返回 JSON {"cron":"..."}。无法用单条 Cron 准确表达或含糊时返回 {"error":"原因"}，不要猜测。'},
                    {"role": "user", "content": text},
                ], "temperature": 0,
            }, timeout=45)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = "\n".join(content.splitlines()[1:-1])
            result = json.loads(content)
        except Exception as exc:
            raise ValueError("时间转换失败，请重试或手动输入 Cron") from exc
        if result.get("error"):
            raise ValueError(str(result["error"]))
        expression = str(result.get("cron") or "").strip()
    return {"cron": expression, "timezone": "Asia/Shanghai", "next_runs": next_runs(expression)}
