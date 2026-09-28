"""采集任务的登录态落盘（目前只有抖音 cookie）。

`task_secrets` 原本只活在内存里，服务一重启就丢 —— 抖音任务会**静默**降级成匿名
请求（粉丝数一类的受限字段采不到，`enrich_profile` 直接原样返回），用户在界面上
看不出任何异常。这里把它落到任务目录下的 `secrets.json`。

与外链表（links_manager）保持一致的两条，和一条刻意相反的：
  * 原子写：先写 `.tmp` 再 `os.replace`，半截文件不会被读成配置。
  * 读不出来当没有：文件缺失/损坏/权限不对都返回空表，不抛 —— 一个附属文件打不开
    不该让整个采集任务失败。
  * **文件权限 0o600**：外链表不加锁，因为那里一条密钥都没有；这里存的 cookie 等价于
    登录凭据，按凭据对待。

密钥永远不进 tasks.json、不进导出产物、不进 API 响应（`public_task` 只暴露
`configured` 标记），所以这个文件是唯一的落点。

删除任务不用在这里另做清理：`CrawlerTaskManager.delete` 会 `rmtree` 整个任务目录，
这个文件跟着一起走。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SECRETS_FILENAME = "secrets.json"
# 白名单：只认这些键。文件是可以被手工编辑的，读回来的东西不能想当然。
ALLOWED_KEYS = ("douyin_cookie",)

# 扫码登录拿到的 cookie 不属于任何一个采集任务，但得有个落脚点 —— 借用一个固定的
# 「任务 ID」，落在 data_dir/douyin_login/ 下，读写走的还是同一套 0o600 原子写。
# 它不是一个真任务：任务 ID 是 uuid4，两者不会撞上。
GLOBAL_COOKIE_TASK_ID = "douyin_login"


def secrets_file(data_dir: Path | str, task_id: str) -> Path:
    return Path(data_dir) / str(task_id) / SECRETS_FILENAME


def load_secrets(data_dir: Path | str, task_id: str) -> dict[str, str]:
    """读该任务的密钥。任何读不出来的情况都当空表，只留一条日志。"""
    try:
        raw = json.loads(secrets_file(data_dir, task_id).read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("采集任务的密钥文件读不出来（%s），当作没有：%s", task_id, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key].strip() for key in ALLOWED_KEYS
            if isinstance(raw.get(key), str) and raw[key].strip()}


def write_secret(data_dir: Path | str, task_id: str, key: str, value: str) -> None:
    """把一条密钥并进该任务的密钥文件（保留已存的其它键）。空值视为不写入。"""
    if key not in ALLOWED_KEYS:
        raise ValueError(f"不认识的密钥：{key}")
    text = str(value or "").strip()
    if not text:
        return
    path = secrets_file(data_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**load_secrets(data_dir, task_id), key: text}
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
    # 先收紧权限再改名：反过来的话文件会有一瞬间是默认宽权限，凭据已经写在里面了。
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
