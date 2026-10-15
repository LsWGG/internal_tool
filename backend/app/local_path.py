"""Resolve a directory the operator already mounted on this machine.

Callers use the path in place. Nothing here copies the tree, and a task may
only create or delete a child whose name is the task id.
"""
from __future__ import annotations

import os
from pathlib import Path


def resolve_directory(raw: str, *, writable: bool = False) -> Path:
    text = (raw or "").strip()
    if not text or any(char in text for char in ("\x00", "\n", "\r")):
        raise ValueError("目录路径无效")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("请填写服务器上的绝对路径")
    resolved = path.resolve()
    if resolved.parent == resolved:
        raise ValueError("不能使用磁盘根目录")
    if not resolved.is_dir():
        raise ValueError("目录不存在或不是文件夹")
    if writable and not os.access(resolved, os.W_OK | os.X_OK):
        raise ValueError("目录不可写")
    return resolved


def resolve_file(raw: str, *, suffixes: tuple[str, ...] = (), writable: bool = False) -> Path:
    text = (raw or "").strip()
    if not text or any(char in text for char in ("\x00", "\n", "\r")):
        raise ValueError("文件路径无效")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("请填写服务器上的绝对路径")
    resolved = path.resolve()
    if resolved.parent == resolved:
        raise ValueError("不能使用磁盘根目录")
    if not resolved.is_file():
        raise ValueError("文件不存在或不是文件")
    allowed = {item.lower() for item in suffixes}
    if allowed and resolved.suffix.lower() not in allowed:
        raise ValueError("文件类型不受支持")
    parent = resolved.parent
    if writable and not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError("目录不可写")
    return resolved


def resolve_path(raw: str, *, writable: bool = False) -> Path:
    """An absolute file or directory that already exists. Never the filesystem root."""
    text = (raw or "").strip()
    if not text or any(char in text for char in ("\x00", "\n", "\r")):
        raise ValueError("路径无效")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("请填写服务器上的绝对路径")
    resolved = path.resolve()
    if resolved.parent == resolved:
        raise ValueError("不能使用磁盘根目录")
    if not resolved.exists():
        raise ValueError("路径不存在")
    target = resolved if resolved.is_dir() else resolved.parent
    if writable and not os.access(target, os.W_OK | os.X_OK):
        raise ValueError("目录不可写")
    return resolved
