"""工具分类：用户在设置里对工具首页分类做的改动（排序、改名、换图标、工具换组）。

存一个 JSON 文件，整表读写 —— 与 links / clean 端点池同一个形状（GET / 整表 POST），
因为使用场景也一样：设置弹窗里一次编辑一小张表。

只存**用户改过的东西**，三块：

  order       已知分类的完整序列（内置那几个 + 自建的），排序的唯一权威。
  categories  逐字段稀疏覆盖。`{"description": ""}` 是「用户把说明清空了」这个**有意义**的
              覆盖，所以判空只能用 `in` / `??`，不能用 `||`。反过来看：把全量记录落盘，
              代码以后改的描述和图标就永远到不了动过设置的用户。
  tools       稀疏：只记偏离代码默认落点的内置工具，拖回默认组就把这个键删掉。

两条**故意**与 `links_manager.py` 同款：

  * 文件是普通的 0644，**不加 0600**。端点池锁权限是为了藏 api_key，这里一条密钥都没有。
  * 服务端不认识「内置分类」这回事。分类表在前端 `toolCatalog.js`，这里只存字符串；
    前端合并时 order 里少了哪个内置 id，谁就按代码顺序回来 —— 所以「内置分类不许删」
    由前端执行（不给按钮），不靠服务端拦。自建分类的 id 也由前端铸（order 必须能引用
    它；links 那张表没有等价的排序字段，所以那边是服务端铸 id）。

校验是手写的，不用 pydantic：这里要的是**几条说人话的中文报错**，而 pydantic 的多行报错
还得再翻译一遍（`links_manager._readable` 干的就是这件事）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

VERSION = 1
# 与前端输入框的 maxlength 对齐；分类名会出现在首页分组标题上，说明出现在它下面。
NAME_MAX = 40
DESCRIPTION_MAX = 160
ICON_MAX = 40
ID_MAX = 40

FIELDS = ("name", "description", "icon")
_LABEL = {"name": "名称", "description": "说明", "icon": "图标"}
_LIMIT = {"name": NAME_MAX, "description": DESCRIPTION_MAX, "icon": ICON_MAX}


class CatalogError(ValueError):
    """用法/配置错误 → HTTP 400。"""


def _say(field: str, tail: str) -> str:
    """把「分类 id」和「不能为空」拼成人话。

    标签以 ASCII 字母数字结尾时要隔一个空格（「分类 id 不能为空」），中文结尾不用
    （「分类 geo 的名称不能为空」）。
    """
    return f"{field} {tail}" if field[-1:].isascii() and field[-1:].isalnum() else f"{field}{tail}"


def _text(value: Any, field: str, limit: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise CatalogError(_say(field, "必须是字符串"))
    text = value.strip()
    if not text and not allow_empty:
        raise CatalogError(_say(field, "不能为空"))
    if len(text) > limit:
        raise CatalogError(_say(field, f"不能超过 {limit} 个字"))
    return text


def _patch(raw: Any, category_id: str) -> dict[str, str]:
    """一条分类覆盖。

    只认 name / description / icon（其它键丢掉 —— 与 links 一样宽松，不 reject），而且
    **只有出现过的字段才写回去**：稀疏是这份数据的语义，不是省事。
    """
    if not isinstance(raw, Mapping):
        raise CatalogError(f"分类 {category_id} 的配置必须是对象")
    patch: dict[str, str] = {}
    for field in FIELDS:
        if field not in raw:
            continue
        patch[field] = _text(raw[field], f"分类 {category_id} 的{_LABEL[field]}",
                             _LIMIT[field], allow_empty=field == "description")
    return patch


def validate(payload: Any) -> dict[str, Any]:
    """把一个整表 POST 收拾成落盘形态。不合法就抛 `CatalogError`（→ 400）。

    `order` 必须在：它是唯一没法从别处重建的东西（前端只能拿代码里的分类去补它）。
    `categories` / `tools` 缺了就是「没有覆盖」。
    """
    if not isinstance(payload, Mapping):
        raise CatalogError("请求体必须是对象")
    catalog = payload.get("catalog")
    if not isinstance(catalog, Mapping):
        raise CatalogError("catalog 必须是对象")

    raw_order = catalog.get("order")
    if not isinstance(raw_order, list):
        raise CatalogError("catalog.order 必须是数组")
    order: list[str] = []
    for item in raw_order:
        category_id = _text(item, "分类 id", ID_MAX, allow_empty=False)
        if category_id in order:
            raise CatalogError(f"分类 id 重复：{category_id}")
        order.append(category_id)

    raw_categories = catalog.get("categories") or {}
    if not isinstance(raw_categories, Mapping):
        raise CatalogError("catalog.categories 必须是对象")
    categories = {str(key): _patch(value, str(key)) for key, value in raw_categories.items()}

    raw_tools = catalog.get("tools") or {}
    if not isinstance(raw_tools, Mapping):
        raise CatalogError("catalog.tools 必须是对象")
    tools: dict[str, str] = {}
    for key, value in raw_tools.items():
        tool_id = _text(key, "工具 id", ID_MAX, allow_empty=False)
        tools[tool_id] = _text(value, f"工具 {tool_id} 的分类", ID_MAX, allow_empty=False)

    return {"order": order, "categories": categories, "tools": tools}


def _empty() -> dict[str, Any]:
    return {"order": [], "categories": {}, "tools": {}}


def _salvage(raw: Mapping[str, Any]) -> dict[str, Any]:
    """文件被手改坏了：能救的救回来，救不了的那一条丢掉并留日志。

    与 `load_links` 里「单条坏了不该毁掉整张表」同一个姿态 —— 前端合并本来就会把缺的
    内置分类按代码顺序补回来，所以丢一条不会让首页少东西。
    """
    catalog = _empty()

    for item in raw.get("order") if isinstance(raw.get("order"), list) else []:
        try:
            category_id = _text(item, "分类 id", ID_MAX, allow_empty=False)
        except CatalogError:
            logger.warning("分类 id 读不出来，已跳过：%r", item)
            continue
        if category_id not in catalog["order"]:
            catalog["order"].append(category_id)

    raw_categories = raw.get("categories")
    if isinstance(raw_categories, Mapping):
        for key, value in raw_categories.items():
            try:
                catalog["categories"][str(key)] = _patch(value, str(key))
            except CatalogError as exc:
                logger.warning("分类 %s 的配置读不出来，已跳过：%s", key, exc)

    raw_tools = raw.get("tools")
    if isinstance(raw_tools, Mapping):
        for key, value in raw_tools.items():
            try:
                tool_id = _text(key, "工具 id", ID_MAX, allow_empty=False)
                catalog["tools"][tool_id] = _text(value, f"工具 {tool_id} 的分类", ID_MAX,
                                                  allow_empty=False)
            except CatalogError as exc:
                logger.warning("工具落点读不出来，已跳过：%s", exc)

    return catalog


def load_catalog(path: Path) -> dict[str, Any]:
    """读分类表。文件不在 / 损坏 / 版本不认识 → 空表 + 一条日志，**不抛**。

    空表在前端就是「用户什么都没改过」，也就是代码里那 6 组 18 卡 —— 读不到配置时首页
    照常能开，比抛出去让整页塌掉重要得多。
    """
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return _empty()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("分类配置文件读不出来（%s），当作没改过：%s", path, exc)
        return _empty()
    if not isinstance(raw, Mapping) or raw.get("version") != VERSION:
        logger.warning("分类配置的版本认不出来（%s），当作没改过", path)
        return _empty()
    try:
        return validate({"catalog": raw})
    except CatalogError as exc:
        logger.warning("分类配置有读不出来的地方（%s）：%s", path, exc)
        return _salvage(raw)


def save_catalog(path: Path, catalog: Mapping[str, Any]) -> None:
    """原子写分类表（先写 `.tmp` 再 `os.replace`）。存 `validate()` 收拾过的形态，不再二次精简：
    精简是前端的职责（只有前端知道代码里的默认值长什么样）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": VERSION, "order": list(catalog["order"]),
                          "categories": catalog["categories"], "tools": catalog["tools"]},
                         ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
    os.replace(tmp, path)


class CatalogManager:
    """分类表的读写。方法都在 `lock` 里跑（与兄弟工具一致）。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.file = self.root / "catalog.json"
        self.lock = threading.RLock()

    def get(self) -> dict[str, Any]:
        return {"catalog": load_catalog(self.file)}

    def save(self, payload: Any) -> dict[str, Any]:
        catalog = validate(payload)
        save_catalog(self.file, catalog)
        return {"catalog": catalog}
