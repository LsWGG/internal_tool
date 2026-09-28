"""Telegram 数据源：公开频道页面解析，以及需要授权会话的 API 采集。

原先散在 CrawlerTaskManager 上的同名方法整体搬到这里，逻辑未变。
"""
from __future__ import annotations

import asyncio
import os
import re
import time

import requests
from bs4 import BeautifulSoup

from .util import decode_response, structured_text, text


def parse_target(value):
    text = str(value or "").strip()
    match = re.search(r"(?:https?://)?(?:t|telegram)\.me/(?:s/|joinchat/|\+)?([A-Za-z0-9_+-]+)", text, re.I)
    if match:
        return match.group(1), f"https://t.me/{match.group(1)}"
    if re.fullmatch(r"@[A-Za-z0-9_]{5,}", text):
        return text[1:], f"https://t.me/{text[1:]}"
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", text):
        return text, f"https://t.me/{text}"
    return text, text

def public_rows(html, chat, maximum):
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
            "message_id": message_id, "text": structured_text(text_node) if text_node else "",
            "author": text(author_node), "published_at": time_node.get("datetime", "") if time_node else "",
            "views": text(views_node), "forwards": text(forwards_node), "replies": text(replies_node),
            "media": "\n".join(media), "chat": chat, "url": url,
        }
        if row["text"] or row["media"]:
            rows.append(row)
    return rows[-maximum:]

def credentials(request):
    api_id = str(request.get("telegram_api_id") or os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = str(request.get("telegram_api_hash") or os.getenv("TELEGRAM_API_HASH") or "").strip()
    session = str(request.get("telegram_session") or os.getenv("TELEGRAM_SESSION") or "").strip()
    if not api_id or not api_hash or not session:
        raise ValueError("此模式需要 Telegram API ID、API Hash 和已授权会话字符串")
    try:
        return int(api_id), api_hash, session
    except ValueError as exc:
        raise ValueError("Telegram API ID 必须是数字") from exc

async def api_collect_async(value, request, mode, maximum):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise RuntimeError("Telegram API 采集组件未安装，请安装 Telethon") from exc
    api_id, api_hash, session = credentials(request)
    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram 会话已失效，请重新生成授权会话")
        if mode == "members":
            target, _ = parse_target(value)
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
            client.iter_messages((await client.get_entity(parse_target(value)[0])), limit=maximum)
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

def api_collect(value, request, mode, maximum):
    try:
        return asyncio.run(api_collect_async(value, request, mode, maximum))
    except (ValueError, RuntimeError):
        raise
    except Exception as exc:
        message = str(exc).splitlines()[-1]
        if re.search(r"flood|wait", message, re.I):
            raise RuntimeError("Telegram 请求过于频繁，请稍后重试") from exc
        raise RuntimeError(f"Telegram API 采集失败：{message[:240]}") from exc

def discover(value, ctx):
    request, task_id = ctx.request, ctx.task_id
    mode = str(request.get("telegram_mode") or "channel").lower()
    requested = int(request.get("max_items") or 50)
    maximum = 500 if requested < 0 else min(500, max(1, requested))
    if task_id:
        labels = {"channel": "频道消息", "group": "群组消息", "members": "群组成员", "search": "全平台结果"}
        ctx.update(task_id, status="running", message=f"正在查找 Telegram {labels.get(mode, '数据')}", progress=10)
    rows = []
    if mode in ("channel", "group"):
        target, public_url = parse_target(value)
        if not target or not re.fullmatch(r"[A-Za-z0-9_]{5,}", target):
            raise ValueError("请输入公开频道或群组链接，例如 https://t.me/example")
        before = ""
        seen_ids = set()
        network_error = None
        try:
            while len(rows) < maximum:
                ctx.checkpoint(task_id)
                page_url = f"https://t.me/s/{target}" + (f"?before={before}" if before else "")
                response = ctx.http.get(page_url, request.get("proxies", []), timeout=25)
                page_rows = public_rows(decode_response(response), target, maximum)
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
            rows = api_collect(public_url, request, mode, maximum)
        if not rows:
            if network_error and re.search(r"timeout|timed out|connect", str(network_error), re.I):
                raise RuntimeError("无法连接 Telegram 公开页面，请在高级设置中配置可访问 Telegram 的代理")
            raise RuntimeError("没有读取到公开消息；请确认链接公开可访问，私有群组需配置 Telegram 授权会话")
    else:
        rows = api_collect(value, request, mode, maximum)
    return [{"url": row.get("url") or f"telegram:{mode}:{index}", "detail_url": "", "prefill": row}
            for index, row in enumerate(rows, 1)]

