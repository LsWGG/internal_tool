"""外链工具：用户自己往工具首页挂的在线工具（地址 + 名称 + 说明 + 分类 + 首字 LOGO）。

存一个 JSON 文件，整表读写 —— 与 clean 的端点池同一个形状（GET / 整表 POST / 单条 DELETE），
因为使用场景也一样：设置弹窗里一次编辑一小张表。

两处**故意**与端点池不同：
  * 文件是普通的 0644，**不加 0600**。端点池锁权限是为了藏 api_key，这里一条密钥都没有。
  * 分类不做白名单。分类表在前端 `toolCatalog.js`，服务端只存字符串；前端渲染时把认不出的
    分类归到「外链工具」组，所以就算有人手改了这个文件也长不出一个空分组。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

# 默认分类：没写分类的外链工具落在「外链工具」这一组（前端用同一个 id）。
CATEGORY_EXTERNAL = "external"
CATEGORY_DEFAULT = CATEGORY_EXTERNAL

# 嵌入检测：一次只读响应头的 GET，连不上就当「没问到」。超时给 4 秒 —— 这条请求在保存
# 的路径上，用户正等着弹窗关掉。
PROBE_TIMEOUT = 4.0
PROBE_WORKERS = 4
PROBE_UA = "Mozilla/5.0 (compatible; InternalToolPortal/1.0; +embed-check)"


class LinksError(ValueError):
    """用法/配置错误 → HTTP 400。"""


class LinksNotFound(LookupError):
    """删一条不存在的 → HTTP 404。"""


def _header_values(headers: Any, name: str) -> list[str]:
    """取一个响应头的**全部**取值。

    `requests`/urllib3 的 HTTPHeaderDict 有 getlist，普通 dict 只有 get。多个 CSP 头是
    「取交集」（每个策略都得放行），所以这里不能让后一个盖掉前一个。
    """
    getter = getattr(headers, "getlist", None)
    if callable(getter):
        return [value for value in (getter(name) or []) if value]
    value = headers.get(name) if hasattr(headers, "get") else None
    if isinstance(value, (list, tuple)):                # 普通 dict 也可以直接给一列
        return [item for item in value if item]
    return [value] if value else []


def frame_policy(headers: Any) -> tuple[bool, str]:
    """从响应头判断「这个地址允不允许被别的站点嵌」。返回 (能不能嵌, 依据)。

    依据是原样的头内容 —— 前端把它显示给用户，用户看到 `frame-ancestors 'self'` 才知道
    自己撞的是什么，而不是只拿到一句「加载失败」。

    只认浏览器真的会执行的两条：`X-Frame-Options` 的 DENY / SAMEORIGIN，和 CSP 的
    `frame-ancestors`。判断刻意保守：`frame-ancestors` 只放行 `*`，写着具体域名的
    （哪怕写的就是本系统）一律按「不能嵌」算 —— 后端不知道前端最终跑在哪个源上
    （5173、8000、还是别人反代出来的域名），猜错的代价是「明明能嵌却开了新窗口」。
    反过来，`ALLOW-FROM` 这种值现代浏览器直接忽略，这里也跟着忽略，不误报。
    """
    for value in _header_values(headers, "x-frame-options"):
        token = value.strip().lower()
        if token.startswith("deny") or token.startswith("sameorigin"):
            return False, f"X-Frame-Options: {value.strip()}"
    for value in _header_values(headers, "content-security-policy"):
        for directive in value.split(";"):
            name, _, sources = directive.strip().partition(" ")
            if name.lower() != "frame-ancestors":
                continue
            if sources.strip() == "*":          # 明确允许任何站点嵌
                continue
            return False, f"frame-ancestors {sources.strip()}".strip()
    return True, ""


def probe_frame_policy(url: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool | None, str]:
    """看一眼这个地址的响应头。返回 (能不能嵌, 依据)；**没问到**时是 (None, "")。

    只读响应头不读正文（stream=True 之后立刻 close），所以不下载页面。访问不到、超时、
    或者对方回了 4xx/5xx，都算「没问到」而不是「能嵌」—— 登录墙、反爬页、错误页也会带
    200/403 回来，拿它们的头当结论等于编。前端对 None 的处理就是今天的行为（照嵌，
    页面上留着说明和「在新窗口打开」），所以这条探测失败不会让任何东西变坏。
    """
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=True, stream=True,
                                headers={"User-Agent": PROBE_UA, "Accept": "text/html,*/*"})
    except requests.RequestException as exc:
        logger.info("嵌入检测没问到 %s：%s", url, exc)
        return None, ""
    try:
        if response.status_code >= 400:
            logger.info("嵌入检测拿到 %s（%s），当作没问到", response.status_code, url)
            return None, ""
        return frame_policy(response.raw.headers)
    finally:
        response.close()


class ExternalLink(BaseModel):
    id: str = ""
    name: str
    description: str = ""
    url: str
    category: str = CATEGORY_DEFAULT
    # 嵌入检测的三个值，别把 None 和 True 混为一谈：
    #   True  问过了，响应头里没有禁止嵌入的声明
    #   False 问过了，站点明确不让嵌（frame_policy 里是它的原话）
    #   None  还没问过（这个功能上线前存下来的，或者上次没问到）
    embeddable: bool | None = None
    frame_policy: str = ""

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("名称不能为空")
        return text

    @field_validator("url")
    @classmethod
    def _url_is_embeddable(cls, value: str) -> str:
        """只收 http/https。

        这个地址会被前端原样写进 iframe 的 `src`，所以 `javascript:` / `data:` 这类
        scheme 是**注入面**，不是「填错了而已」。仓库里其它取用户网址的地方用的是同一套
        手写校验（`crawler_manager._safe_url`），这里不引入 `HttpUrl`：全仓库没人用它，
        而且它对内网地址（`http://127.0.0.1:8080`）的写法比这里的用户预期更严。
        """
        text = (value or "").strip()
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("地址必须以 http:// 或 https:// 开头")
        return text

    @field_validator("category")
    @classmethod
    def _category_not_blank(cls, value: str) -> str:
        return (value or "").strip() or CATEGORY_DEFAULT


def _readable(exc: Exception) -> str:
    """pydantic 的报错是多行的（还带一节文档地址），挑前几条说人话。"""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    items = errors()
    parts = ["/".join(str(bit) for bit in item.get("loc") or ()) + f"：{item.get('msg')}"
             for item in items[:3]]
    if len(items) > 3:
        parts.append(f"还有 {len(items) - 3} 处")
    return "；".join(parts) or str(exc)


def load_links(path: Path) -> list[ExternalLink]:
    """读外链工具表。损坏的文件当空表处理（并留日志），不抛 —— 首页打不开更糟。"""
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("外链工具文件读不出来（%s），当作空表：%s", path, exc)
        return []
    items = raw.get("links") if isinstance(raw, dict) else raw
    links: list[ExternalLink] = []
    for item in items or []:
        try:
            links.append(ExternalLink.model_validate(item))
        except Exception as exc:                       # 单条坏了不该毁掉整张表
            logger.warning("外链工具里有一条读不出来，已跳过：%s", exc)
    return links


def save_links(path: Path, links: Sequence[ExternalLink]) -> None:
    """原子写外链工具表（先写 `.tmp` 再 `os.replace`）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "links": [item.model_dump() for item in links]},
                         ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
    os.replace(tmp, path)


class LinksManager:
    """外链工具表的读写。方法都在 `lock` 里跑（与兄弟工具一致）。

    `probe` 是注入进来的检测函数（`url -> (能不能嵌, 依据)`）：单元测试传桩，浏览器套件
    让它打真网络（目标都是本机私有实例，快且确定）。传 None 就是用真的那个。
    """

    def __init__(self, root: Path | str, probe: Callable[[str], tuple[bool | None, str]] | None = None):
        self.root = Path(root)
        self.file = self.root / "links.json"
        self.lock = threading.RLock()
        self.probe = probe_frame_policy if probe is None else probe

    def _safe_probe(self, url: str) -> tuple[bool | None, str]:
        """探测失败绝不能连累保存 —— 检测结论只是「能不能嵌」的提示，不是这条数据的完整性。"""
        try:
            return self.probe(url)
        except Exception as exc:                        # noqa: BLE001 —— 桩、DNS、TLS 什么都可能炸
            logger.warning("嵌入检测出错（%s）：%s", url, exc)
            return None, ""

    def _probe_all(self, links: Sequence[ExternalLink]) -> None:
        """并行探测。一次保存里改了好几条时，不该按顺序各等一个超时。"""
        if not links:
            return
        with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(links))) as pool:
            results = list(pool.map(lambda item: self._safe_probe(item.url), links))
        for item, (embeddable, policy) in zip(links, results):
            item.embeddable, item.frame_policy = embeddable, policy

    def list(self) -> dict[str, Any]:
        return {"links": [item.model_dump() for item in load_links(self.file)]}

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """整表替换。id 由客户端带回，缺的（新建）在这里生成。"""
        items = payload.get("links")
        if not isinstance(items, list):
            raise LinksError("links 必须是数组")
        # 现有表只用来复用结论：探测是网络动作，地址没变就别重做
        current = {item.id: item for item in load_links(self.file)}
        links: list[ExternalLink] = []
        pending: list[ExternalLink] = []
        seen: set[str] = set()
        for raw in items:
            if not isinstance(raw, Mapping):
                raise LinksError("外链工具必须是对象")
            data = dict(raw)
            # 检测结论只认后端自己探到的：整表 POST 是客户端说了算的，由它带 embeddable
            # 上来等于让浏览器决定「这个站点能不能嵌」。
            data.pop("embeddable", None)
            data.pop("frame_policy", None)
            link_id = str(data.get("id") or "").strip()
            if not link_id:
                data["id"] = link_id = uuid.uuid4().hex[:12]
            if link_id in seen:
                raise LinksError(f"外链工具 id 重复：{link_id}")
            seen.add(link_id)
            try:
                link = ExternalLink.model_validate(data)
            except Exception as exc:
                raise LinksError(
                    f"外链工具配置有误（{data.get('name') or link_id}）：{_readable(exc)}"
                ) from exc
            previous = current.get(link_id)
            if previous is not None and previous.url == link.url and previous.embeddable is not None:
                link.embeddable, link.frame_policy = previous.embeddable, previous.frame_policy
            else:
                # 新加的、改了地址的、以及**上次没问到**的（None）都在这里重探：站点当时
                # 连不上，下次保存顺手再问一次，结论会自己好起来。
                pending.append(link)
            links.append(link)
        self._probe_all(pending)
        save_links(self.file, links)
        return {"links": [item.model_dump() for item in links]}

    def probe_one(self, link_id: str) -> dict[str, Any]:
        """重新检测一条（设置弹窗里的「检测」按钮）并写回文件。

        老数据（这个功能上线前存的）都是 None，只有这个显式动作能把它们变成结论。
        """
        links = load_links(self.file)
        target = next((item for item in links if item.id == link_id), None)
        if target is None:
            raise LinksNotFound("外链工具不存在")
        target.embeddable, target.frame_policy = self._safe_probe(target.url)
        save_links(self.file, links)
        return {"link": target.model_dump()}

    def delete(self, link_id: str) -> dict[str, Any]:
        current = load_links(self.file)
        keep = [item for item in current if item.id != link_id]
        if len(keep) == len(current):
            raise LinksNotFound("外链工具不存在")
        save_links(self.file, keep)
        return {"ok": True}
