"""用户函数源码的格式化：调 ruff 把代码排成规范的样子。

为什么在服务端做：ruff 是这个生态里事实上的标准，而它本身是个 Python 程序 —— 前端要自己
格式化，就得再背一份 WASM 版本（另一份实现、另一套版本号、行为还不保证一样）。这里调的
是 venv 里装的**那一份**，和 `/meta` 把生成器目录发给前端是同一个理由：界面能做的和后端
能跑的必须是同一份东西。

只格式化（`ruff format`），不跑 `ruff check --fix`：后者会顺手删掉没用到的 import ——
那是在改用户的代码，不只是排版。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

FORMAT_TIMEOUT_S = 10.0
"""格式化一段源码的正常耗时是毫秒级。10 秒都没回来说明 ruff 卡住了，不该拖着请求不放。"""

_SAFE_NAME = re.compile(r"[^0-9A-Za-z_]+")

__all__ = ["FORMAT_TIMEOUT_S", "format_source", "ruff_command"]


def ruff_command() -> list[str] | None:
    """找一个能用的 ruff：`CLEAN_RUFF` → 本解释器的 `python -m ruff` → PATH → venv 隔壁。

    先试 `python -m ruff` 而不是去翻 `bin/`：服务进程的 `sys.executable` 就是 venv 的解释器，
    「模块在不在」是确定的；而可执行文件的名字各平台不一样（Windows 是 `ruff.exe`），
    路径拼出来的东西错了还很难看出来。
    """
    override = os.environ.get("CLEAN_RUFF", "").strip()
    if override:
        return [override] if Path(override).exists() or shutil.which(override) else None
    try:
        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
    except (ImportError, ValueError):  # pragma: no cover - 取决于环境
        pass
    found = shutil.which("ruff")
    if found:
        return [found]
    adjacent = Path(sys.executable).with_name("ruff.exe" if os.name == "nt" else "ruff")
    return [str(adjacent)] if adjacent.is_file() else None


def format_source(source: str, name: str = "") -> tuple[str, str]:
    """把源码排成规范的样子。返回 `(格式化后的源码, 错误说明)`；出错时源码是空串。

    失败**不抛异常**：调用方（`CleanManager.format_function`）要把「为什么不能格式化」
    原样告诉用户，而 ruff 的报错自带插入符图示，是最有用的那部分信息。
    """
    command = ruff_command()
    if command is None:
        return "", (
            "未找到 ruff，无法格式化。请安装项目依赖后重启服务"
            "（uv pip install -r backend/requirements.txt）。"
        )
    # `--stdin-filename` 只是给 ruff 认语言/找配置用的标签，不是真要去读这个文件；
    # 但它是用户输入，所以照样白名单化 —— 别让一个函数名变成路径的一部分。
    label = _SAFE_NAME.sub("_", name or "").strip("_")[:40] or "function"
    try:
        completed = subprocess.run(  # noqa: S603 - 参数全是常量或白名单化的名字
            [
                *command,
                "format",
                "--stdin-filename",
                f"{label}.py",
                # 不加这个，ruff 会在服务进程的 cwd（backend/）留下 .ruff_cache/
                "--no-cache",
                # 不要加 `--quiet`：成功时它没有额外输出可压（stdout 就是格式化后的源码），
                # 失败时却会把唯一有用的那句 `error: Failed to parse …:1:7: …` 一起吞掉。
                "-",
            ],
            input=source,
            capture_output=True,
            text=True,
            timeout=FORMAT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "", f"ruff 超过 {FORMAT_TIMEOUT_S:.0f} 秒没有返回，已放弃（源码没有改动）。"
    except OSError as exc:
        return "", f"无法运行 ruff：{exc}"
    if completed.returncode != 0:
        # 与 md_word_manager / mermaid_manager 报错尾部同一套做法：留最后一段，前面多半是噪
        detail = (completed.stderr or completed.stdout or "").strip()
        return "", detail[-1600:] or f"ruff 退出码 {completed.returncode}"
    return completed.stdout, ""
