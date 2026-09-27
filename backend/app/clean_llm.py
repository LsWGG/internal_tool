"""多端点大模型池：加权最小在途的负载均衡、熔断半开、持久缓存、成本护栏。

# 负载均衡为什么是「加权最小在途」而不是轮询或随机

慢端点在途数会自然堆积，于是它被少选 —— **这就是「按配置与网络状况负载均衡」的落地
方式**，不需要另写一套健康探测。得分 `(在途 + 1) / 最大并发 / 权重`：

- `+1` 让空闲端点的得分不为零，避免 0 分端点被无限选；
- 除以最大并发：并发 8 的端点应当比并发 2 的多接 4 倍；
- 除以权重：给「我更信这个端点」留一个手动旋钮；
- 熔断的、停用的、没令牌的端点直接不在候选里，**不发请求**（而不是发了再失败）。

# 四类失败的区别对待

| 情况 | 做法 | 理由 |
|---|---|---|
| 4xx（429 除外） | **不**转移，直接抛 | 请求本身或配置有问题，换端点只会重复失败，把它藏起来最糟 |
| 429 | 尊重 `Retry-After`，然后转移 | 这是端点级的容量问题，换个端点是对的 |
| 5xx / 超时 / 连不上 | 退避后转移（最多 3 个不同端点） | 端点级故障 |
| 单行生成的输出不合法 | 记 `llm_error`，交给回退策略 | 数据级问题，重试同一个端点有意义 |

# 密钥

`api_key` 只活在端点池文件（0600、在已 gitignore 的 `backend/clean_data/` 下）与任务级
`secret.json` 里，**绝不进 `tasks.json`**（那个文件每次进度更新都重写、还被列表接口返回）。
前端用 `********` 哨兵表示「保持原密钥」，由 API 层翻译，模型层永远拿不到哨兵。

注：这里刻意**没有**用 pydantic 的 `Field(exclude=True)` 来表达「密钥不进序列化」—— 那个
开关会把它从端点池文件里一并抹掉，而端点池文件正是它唯一该待的地方。所以改成两个显式的
出口：`public()`（给 API/报告，密钥换哨兵）与 `stored()`（给端点池文件）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import orjson
from pydantic import BaseModel, Field, field_validator

from .clean_generate import (
    BaseGenerator,
    CleanGenerateError,
    GenerationBatch,
    render_template,
    validate_template,
)
from .clean_models import CleanTaskConfig, FieldSpec, GenerateRule, LlmTaskOptions
from .clean_state import ensure_private_dir, write_private

logger = logging.getLogger(__name__)

# 前端用它表示「密钥不变」。API 层负责翻译成「保留原值」，模型层不该看到它。
SENTINEL = "********"
POOL_FILENAME = "llm_endpoints.json"

MAX_FAILOVER_ENDPOINTS = 3
CACHE_LIMIT = 20000
BACKOFF_CAP = 30.0

# 提交间隔的默认值。有大模型时密得多：崩溃后要重算的行数上限就是这个窗口，而每行重算都
# 可能是第二次花钱的大模型请求（持久缓存会吸收大部分，但不是全部）。
#
# 放在这里而不是引擎里，是因为**它同时是分块大小**：引擎按它切块（于是它决定了「一轮能发
# 多少请求」），预估要用同一个数字算轮数。两处各写一个 2000 迟早会分岔，而那正是「预估
# 与实际不符」的来源。
COMMIT_ROWS_DEFAULT = 50_000
COMMIT_ROWS_LLM = 2_000


class LlmError(Exception):
    """所有大模型相关错误的基础类型。"""


class LlmHttpError(LlmError):
    def __init__(self, status: int, text: str, endpoint: str = ""):
        self.status = int(status)
        self.text = (text or "")[:500]
        self.endpoint = endpoint
        super().__init__(f"HTTP {self.status}（{endpoint}）：{self.text}")


class LlmNetworkError(LlmError):
    """连不上、超时、读一半断了 —— 都归这里。"""


class LlmBudgetExhausted(LlmError):
    """调用次数硬上限用完了。任务应当**确定性地完成**，而不是无限重试。"""


class LlmAbortError(LlmError):
    """错误率超过阈值。一个「成功」但全是回退值的任务比诚实失败更糟。"""


class LlmUnavailable(LlmError):
    """一个可用端点都没有（全部在冷却或已停用）。"""


# =========================================================================== 端点


class LlmEndpoint(BaseModel):
    id: str
    name: str = ""
    url: str
    model: str = ""
    api_key: str = ""
    max_concurrency: int = Field(default=4, ge=1, le=256)
    weight: int = Field(default=1, ge=1, le=100)
    rpm: int = Field(default=0, ge=0)          # 0 = 不限速
    timeout_s: float = Field(default=60.0, gt=0, le=3600)
    connect_timeout_s: float = Field(default=5.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    temperature: float = Field(default=0.3, ge=0, le=2)
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    proxy: str = ""
    enabled: bool = True
    # 持久化的健康指标：端点池越用越准，预估也就越准
    ewma_ms: float = 0.0
    # `ewma_ms` 是**一次请求**的耗时，而「一次请求处理几行」是另一回事：10 行一批量出来的
    # 4856 ms 和 1 行一批量出来的 4856 ms 不是同一个东西。预估要按用户新配的批量折算，
    # 就必须知道这个延迟是在多大的批量上量出来的。老端点池文件没有这个字段 → 0.0
    # （未知），预估会按「不折算」处理，而不是瞎折。
    ewma_rows: float = 0.0
    ewma_samples: int = 0

    @field_validator("id", "url", "model")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """空 id / 空地址在加载期就拒绝。

        否则它们会在运行期变成「每行都失败一次、还看不出为什么」的端点，而端点池文件的
        读取是逐条容错的 —— 每条非法配置被跳过时都有日志，比跑起来才炸好得多。
        """
        text = (value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text

    @property
    def chat_url(self) -> str:
        """补成 OpenAI 兼容的 chat/completions 地址。

        用户填 `https://x/v1` 或 `https://x/v1/` 或完整的 `.../chat/completions` 都行 ——
        这三种都见过，让用户去猜哪一种是对的没有意义。
        """
        url = (self.url or "").strip().rstrip("/")
        if not url:
            raise LlmError("端点地址是空的")
        if url.endswith("/chat/completions"):
            return url
        return url + "/chat/completions"

    def public(self) -> dict[str, Any]:
        """给 API 与报告的形状：密钥换成哨兵，附带「有没有配密钥」。"""
        data = self.model_dump()
        data["api_key"] = SENTINEL if self.api_key else ""
        data["key_set"] = bool(self.api_key)
        return data

    def stored(self) -> dict[str, Any]:
        """给端点池文件的形状（0600）。密钥只在这里和任务级 secret.json 里出现。"""
        return self.model_dump()


def load_pool(path: Path) -> list[LlmEndpoint]:
    """读端点池。损坏的文件当空池处理（并留下日志），不抛 —— 打不开工具页更糟。"""
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("端点池文件读不出来（%s），当作空池：%s", path, exc)
        return []
    items = raw.get("endpoints") if isinstance(raw, dict) else raw
    endpoints: list[LlmEndpoint] = []
    for item in items or []:
        try:
            endpoints.append(LlmEndpoint.model_validate(item))
        except Exception as exc:                       # 单条坏了不该毁掉整个池
            logger.warning("端点池里有一条读不出来，已跳过：%s", exc)
    return endpoints


def save_pool(path: Path, endpoints: Sequence[LlmEndpoint]) -> None:
    """原子写端点池，权限 0600。"""
    path = Path(path)
    ensure_private_dir(path.parent)
    payload = orjson.dumps(
        {"version": 1, "endpoints": [item.stored() for item in endpoints]},
        option=orjson.OPT_INDENT_2,
    )
    write_private(path, payload)


def merge_endpoint(existing: LlmEndpoint | None, patch: Mapping[str, Any]) -> LlmEndpoint:
    """把一次 API 更新合并到端点上，处理密钥哨兵。

    `api_key` 是哨兵 → 保留原值（前端拿到的就是哨兵，回传时不该把密钥抹掉）；
    是空串 → 明确清除；其它 → 覆盖。

    新建的端点在这里补 id：`id` 是模型的必填字段，而「先测一下这个还没保存的地址通不通」
    正是它的主要用法 —— 要求调用方先造一个 id 才能测试，等于把测试按钮限制了。
    """
    data = dict(patch or {})
    incoming = data.pop("api_key", None)
    if existing is None:
        if not str(data.get("id") or "").strip():
            data["id"] = uuid.uuid4().hex[:12]
        endpoint = LlmEndpoint.model_validate(data)
        if incoming and incoming != SENTINEL:
            endpoint.api_key = str(incoming)
        return endpoint
    merged = existing.model_dump()
    merged.update(data)
    endpoint = LlmEndpoint.model_validate(merged)
    if incoming is None or incoming == SENTINEL:
        endpoint.api_key = existing.api_key
    elif incoming == "":
        endpoint.api_key = ""
    else:
        endpoint.api_key = str(incoming)
    return endpoint


# =========================================================================== 每端点状态


class EndpointState:
    """运行期状态。所有这些都**不**持久化（除了 EWMA 延迟），重启后重新统计。"""

    __slots__ = ("in_flight", "ok", "failed", "consecutive_failures", "ewma_ms",
                 "ewma_rows", "ewma_samples", "circuit_until", "probing", "tokens",
                 "refilled_at", "last_error")

    def __init__(self, endpoint: LlmEndpoint, now: float):
        # `__slots__` 的字段**必须**在这里逐个赋值：没赋值的槽连属性都不存在，
        # 读它会 AttributeError，而不是像普通类那样回落到类属性默认值。
        self.in_flight = 0
        self.ok = 0
        self.failed = 0
        self.consecutive_failures = 0
        self.ewma_ms = float(endpoint.ewma_ms or 0.0)
        self.ewma_rows = float(endpoint.ewma_rows or 0.0)
        self.ewma_samples = int(endpoint.ewma_samples or 0)
        self.circuit_until = 0.0
        self.probing = False
        self.tokens = float(endpoint.rpm) if endpoint.rpm else 0.0
        self.refilled_at = now
        self.last_error = ""

    # -- 令牌桶 ------------------------------------------------------------

    def refill(self, endpoint: LlmEndpoint, now: float) -> None:
        if not endpoint.rpm:
            return
        elapsed = now - self.refilled_at
        if elapsed <= 0:
            return
        self.tokens = min(float(endpoint.rpm), self.tokens + elapsed * endpoint.rpm / 60.0)
        self.refilled_at = now

    def wait_for_token(self, endpoint: LlmEndpoint, now: float) -> float:
        """返回还需要等多少秒才有令牌（0 表示现在就有）。"""
        if not endpoint.rpm:
            return 0.0
        self.refill(endpoint, now)
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) * 60.0 / endpoint.rpm


def circuit_cooldown(consecutive_failures: int) -> float:
    """连续失败 ≥3 次才熔断，冷却时间指数增长、上限 300 秒。

    设 3 次的门槛是刻意的：偶发的网络抖动不该让一个健康端点下线，而真的挂了的端点
    也不该被反复试探。
    """
    if consecutive_failures < 3:
        return 0.0
    return min(300.0, 15.0 * (2 ** (consecutive_failures - 3)))


# =========================================================================== 池


class LlmPool:
    """端点池 + 选择器 + 指标。线程安全：引擎的多个工作线程会同时来要名额。"""

    def __init__(
        self,
        endpoints: Sequence[LlmEndpoint],
        *,
        on_metrics: Callable[[dict[str, Any]], None] | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.endpoints = list(endpoints)
        self._on_metrics = on_metrics
        self._now = time_source
        self._sleep = sleep
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._states: dict[str, EndpointState] = {
            item.id: EndpointState(item, self._now()) for item in self.endpoints
        }
        self._rr = 0                     # 平局时的轮转起点，避免永远选同一个

    # -- 查询 --------------------------------------------------------------

    @property
    def usable(self) -> list[LlmEndpoint]:
        return [item for item in self.endpoints if item.enabled]

    def total_concurrency(self) -> int:
        """池能同时容纳的在途请求数 = Σ `max_concurrency`。

        **权重不进这个和。** `max_concurrency` 是用户写下的「这个端点最多同时几个请求」，
        权重是「我更愿意把它用在哪儿」，两者是不同的东西：权重只改变分配**比例**
        （`a` 权重 4、`b` 权重 1 时，`a` 会先拿到 4 倍的请求量），不会让 `a` 同时多跑 4 个
        请求 —— 那需要用户自己去改 `max_concurrency`。这个和喂给预估，所以它必须是
        真实的同时在途上限。
        """
        return sum(item.max_concurrency for item in self.usable)

    def available_concurrency(self) -> int:
        """**现在**真能派出去的在途请求数 = Σ 未熔断的启用端点 `max_concurrency`。

        与 `total_concurrency()` 的分工：那个喂预估（「这池子有多大」），这个喂运行期
        决定「同时开几个工作线程」。两者只在熔断时分岔 —— 冷却中的端点一个名额都给不出来，
        按总数派宽度的话 worker 会一起堵在 `slot()` 里等满 `slot_wait`，再一条接一条抛
        `LlmUnavailable`，而**每个失败窗口都记一次失败**，错误率一步就能跨过中止阈值。
        所以派发方每轮重新问一次，冷却中的端点下一轮自动让出宽度。
        """
        now = self._now()
        with self._lock:
            return sum(item.max_concurrency for item in self.usable
                       if self._states[item.id].circuit_until <= now)

    def untried(self, exclude: Sequence[str]) -> list[LlmEndpoint]:
        """还没试过的启用端点。故障转移靠它判断「换个端点」是否还有意义。"""
        excluded = set(exclude)
        return [item for item in self.usable if item.id not in excluded]

    def average_latency_ms(self) -> float | None:
        """按权重汇总的 EWMA 延迟。预估用它，没有历史就用 None（调用方给保守缺省）。"""
        total = weight = 0.0
        for item in self.usable:
            state = self._states[item.id]
            if state.ewma_ms > 0:
                total += state.ewma_ms * item.weight
                weight += item.weight
        return (total / weight) if weight else None

    def average_batch(self) -> float:
        """按权重汇总「历史延迟是在多大的批量上量出来的」，未知则 0.0。

        预估拿它把历史延迟折算到用户**这次**配的批量上：没有它，把批量从 10 改成 20 之后
        估算仍然按「10 行一次的耗时」算 20 行 —— 与「把批量折扣算两遍」是同一类错误，
        只是方向相反。
        """
        total = weight = 0.0
        for item in self.usable:
            state = self._states[item.id]
            if state.ewma_ms > 0 and state.ewma_rows > 0:
                total += state.ewma_rows * item.weight
                weight += item.weight
        return (total / weight) if weight else 0.0

    def state(self, endpoint_id: str) -> EndpointState | None:
        return self._states.get(endpoint_id)

    def next_available_in(self) -> float:
        """所有端点都不可用时，最快多久会有一个可用（给等待设上限，别无限等）。"""
        now = self._now()
        waits: list[float] = []
        with self._lock:
            for item in self.usable:
                state = self._states[item.id]
                if state.circuit_until > now:
                    waits.append(state.circuit_until - now)
                else:
                    token_wait = state.wait_for_token(item, now)
                    if token_wait:
                        waits.append(token_wait)
        return min(waits) if waits else 0.0

    # -- 选择 --------------------------------------------------------------

    def _score(self, endpoint: LlmEndpoint, state: EndpointState, now: float) -> float | None:
        """返回得分（越小越优先），`None` 表示这一轮不可选。"""
        if not endpoint.enabled:
            return None
        if state.circuit_until > now:
            return None
        if state.circuit_until and state.probing:
            return None                       # 半开状态只放一个探测请求进去
        if state.in_flight >= endpoint.max_concurrency:
            return None
        if endpoint.rpm and state.wait_for_token(endpoint, now) > 0:
            return None
        return (state.in_flight + 1) / endpoint.max_concurrency / endpoint.weight

    def choose(self) -> LlmEndpoint | None:
        """挑一个端点（不占名额）。测试与预估用；真正要发请求请用 `slot()`。"""
        with self._lock:
            return self._pick(set())

    def _pick(self, excluded: set[str]) -> LlmEndpoint | None:
        """**必须在锁内调用。** 选中即占一个在途名额，所以只有一处能调它。"""
        now = self._now()
        candidates = [item for item in self.usable if item.id not in excluded]
        if not candidates:
            return None
        count = len(candidates)
        chosen: LlmEndpoint | None = None
        best_score = 0.0
        for offset in range(count):
            endpoint = candidates[(self._rr + offset) % count]
            state = self._states[endpoint.id]
            score = self._score(endpoint, state, now)
            if score is None:
                continue
            # 严格小于：得分相同时保留**扫描序列里最先出现**的那个，而扫描起点每选中一次
            # 就前进一格 —— 这就等价于等配端点之间的轮转。若改成比较列表下标，平局会永远
            # 判给列表第一个端点，低负载时全部流量压在一个端点上（正是要避免的那种不均）。
            if chosen is None or score < best_score:
                chosen, best_score = endpoint, score
        if chosen is None:
            return None
        self._rr = (self._rr + 1) % count
        state = self._states[chosen.id]
        if chosen.rpm:
            state.refill(chosen, now)
            state.tokens = max(0.0, state.tokens - 1.0)
        state.in_flight += 1
        # 熔断冷却已过、还没人试过的端点：这一次请求就是探测。失败会重新熔断，
        # 成功才真正恢复 —— 半开状态只放一个请求进去，避免刚恢复就被打满。
        state.probing = state.circuit_until > 0
        return chosen

    @contextmanager
    def slot(self, *, exclude: Sequence[str] = (), wait: float = 60.0) -> Iterator[LlmEndpoint]:
        """占一个在途名额直到 with 块结束。等不到可用端点就抛 `LlmUnavailable`。

        `exclude` 用于故障转移：已经试过的端点不再选。
        """
        excluded = set(exclude)
        deadline = self._now() + wait
        while True:
            with self._cv:
                chosen = self._pick(excluded)
                if chosen is not None:
                    break
                remaining = deadline - self._now()
                if remaining <= 0:
                    raise LlmUnavailable(
                        "没有可用的大模型端点（都在冷却、达到并发上限或已停用）"
                    )
                # 等到「有人归还名额」或「某个端点的冷却/限速到期」为止，取先到的那个
                wake = min(remaining, max(0.02, self.next_available_in()))
                self._cv.wait(timeout=wake)
        try:
            yield chosen
        finally:
            with self._cv:
                self._release_locked(chosen)
                self._cv.notify_all()

    def _release_locked(self, endpoint: LlmEndpoint) -> None:
        state = self._states.get(endpoint.id)
        if state is None:
            return
        state.in_flight = max(0, state.in_flight - 1)
        state.probing = False

    # -- 回报 --------------------------------------------------------------

    def report_ok(self, endpoint: LlmEndpoint, seconds: float, *, rows: int = 1) -> None:
        """记一次成功。`rows` 是这次请求处理了几行 —— 延迟要与批量一起记，预估才算得准。"""
        with self._cv:
            state = self._states[endpoint.id]
            state.ok += 1
            state.consecutive_failures = 0
            state.circuit_until = 0.0
            state.last_error = ""
            self._update_ewma(endpoint, state, seconds * 1000.0, rows)
            self._emit_metrics()
            self._cv.notify_all()

    def report_failure(self, endpoint: LlmEndpoint, error: BaseException, *,
                       retry_after: float = 0.0) -> None:
        with self._cv:
            state = self._states[endpoint.id]
            state.failed += 1
            state.consecutive_failures += 1
            state.last_error = str(error)[:200]
            cooldown = circuit_cooldown(state.consecutive_failures)
            if cooldown:
                state.circuit_until = self._now() + cooldown
            if retry_after:
                state.circuit_until = max(state.circuit_until, self._now() + retry_after)
            self._emit_metrics()
            self._cv.notify_all()

    def _update_ewma(self, endpoint: LlmEndpoint, state: EndpointState,
                     ms: float, rows: int) -> None:
        # 端点池里没有历史时直接取首值，否则慢启动会让前几十次预估离谱
        if state.ewma_ms <= 0:
            state.ewma_ms = ms
        else:
            state.ewma_ms = 0.7 * state.ewma_ms + 0.3 * ms
        # 批量（行数）跟延迟同一套平滑：用户把「批量合并」从 10 改成 20 之后，历史延迟要
        # 几轮就收敛到新批量上，而不是永远停在上一个批量上。0 行（不该出现）不参与。
        if rows > 0:
            if state.ewma_rows <= 0:
                state.ewma_rows = float(rows)
            else:
                state.ewma_rows = 0.7 * state.ewma_rows + 0.3 * rows
        state.ewma_samples += 1
        endpoint.ewma_ms = state.ewma_ms
        endpoint.ewma_rows = state.ewma_rows
        endpoint.ewma_samples = state.ewma_samples

    # -- 指标 --------------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._now()
        with self._lock:
            items = []
            for endpoint in self.endpoints:
                state = self._states[endpoint.id]
                items.append({
                    "id": endpoint.id,
                    "name": endpoint.name or endpoint.model or endpoint.id,
                    "model": endpoint.model,
                    "enabled": endpoint.enabled,
                    "in_flight": state.in_flight,
                    "max_concurrency": endpoint.max_concurrency,
                    "weight": endpoint.weight,
                    "ok": state.ok,
                    "failed": state.failed,
                    "ewma_ms": round(state.ewma_ms, 1),
                    "ewma_rows": round(state.ewma_rows, 1),
                    "ewma_samples": state.ewma_samples,
                    "circuit_open": state.circuit_until > now,
                    "circuit_seconds": round(max(0.0, state.circuit_until - now), 1),
                    "last_error": state.last_error,
                })
            return items

    def _emit_metrics(self) -> None:
        if self._on_metrics is None:
            return
        try:
            self._on_metrics({"endpoints": self.snapshot(),
                              "average_ms": self.average_latency_ms()})
        except Exception:                       # 指标推送失败不能影响生成
            logger.debug("端点指标推送失败", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._on_metrics = None


# =========================================================================== 预算护栏


@dataclass
class CallBudget:
    """调用次数硬上限 + 错误率中止。

    `min_calls_to_abort` 是必需的：没有它，第 1 次调用失败就是 100% 错误率，任务会被一次
    网络抖动判死。20 次以下不判中止，让偶发抖动过去。
    """

    max_calls: int = 50000
    abort_error_rate: float = 0.5
    min_calls_to_abort: int = 20
    calls: int = 0
    ok: int = 0
    failed: int = 0
    exhausted: bool = False
    aborted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(self) -> None:
        with self._lock:
            if self.exhausted:
                raise LlmBudgetExhausted(
                    f"大模型调用次数已达上限 {self.max_calls}，不再发起新请求"
                )
            if self.aborted:
                raise LlmAbortError("大模型错误率过高，任务已中止")

    def note(self, *, ok: bool) -> None:
        with self._lock:
            self.calls += 1
            if ok:
                self.ok += 1
            else:
                self.failed += 1
            if self.max_calls and self.calls >= self.max_calls:
                self.exhausted = True
            if self.calls >= self.min_calls_to_abort and self.error_rate() > self.abort_error_rate:
                self.aborted = True

    def error_rate(self) -> float:
        return (self.failed / self.calls) if self.calls else 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls) if self.max_calls else -1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls, "ok": self.ok, "failed": self.failed,
                "error_rate": round(self.error_rate(), 4),
                "max_calls": self.max_calls, "remaining": self.remaining,
                "exhausted": self.exhausted, "aborted": self.aborted,
            }


# =========================================================================== 缓存


class LlmCache:
    """LRU + 追加写 `llm_cache.jsonl`。

    持久那份是**续跑必需**的：没有它，崩溃回退窗口内已经付过钱的大模型调用会在续跑时
    重来一遍。缓存条目只增不减（jsonl 不可变），重启时重放、按 key 取最后一次。
    """

    def __init__(self, path: Path | None = None, *, limit: int = CACHE_LIMIT):
        self.path = Path(path) if path is not None else None
        self.limit = max(1, int(limit))
        self._items: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(model: str, temperature: float, system: str, prompt: str,
            max_tokens: int = -1) -> str:
        """键覆盖**所有会改变输出的东西**。

        `max_tokens` 也必须进键：同一个 prompt 用小额度试出来的截断结果如果被缓存，之后
        用足额度的请求会拿到那段截断文本 —— 而且看起来完全正常。
        """
        blob = "\x00".join([model or "", f"{temperature:g}", f"{max_tokens}",
                            system or "", prompt or ""])
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                self.hits += 1
                return self._items[key]
            self.misses += 1
            return None

    def put(self, key: str, value: str) -> None:
        line = orjson.dumps({"key": key, "value": value}) + b"\n"
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as handle:      # 追加写，崩溃只丢尾部
                handle.write(line)

    def restore(self) -> int:
        """重放 jsonl。**后写的覆盖先写的**，所以读取顺序就是事实顺序。"""
        if self.path is None or not self.path.exists():
            return 0
        loaded = 0
        with open(self.path, "rb") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = orjson.loads(line)
                    key, value = item["key"], item["value"]
                except (orjson.JSONDecodeError, KeyError, TypeError):
                    continue                            # 崩溃时最后一行可能写了一半
                self._items[key] = value
                loaded += 1
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return loaded

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"size": len(self._items), "limit": self.limit,
                    "hits": self.hits, "misses": self.misses}


# =========================================================================== 客户端


@dataclass
class LlmReply:
    text: str
    endpoint_id: str
    seconds: float
    cached: bool = False


class LlmClient:
    """按池发请求：缓存 → 预算 → 选端点 → 重试 → 故障转移。

    `transport` 可注入，测试用它完全替代网络。签名见 `default_transport`。
    """

    def __init__(
        self,
        pool: LlmPool,
        *,
        cache: LlmCache | None = None,
        budget: CallBudget | None = None,
        transport: Callable[..., Any] | None = None,
        max_failover: int = MAX_FAILOVER_ENDPOINTS,
        slot_wait: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        on_attempt: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.pool = pool
        self.cache = cache
        self.budget = budget or CallBudget()
        self._transport = transport or default_transport
        self.max_failover = max(1, int(max_failover))
        self.slot_wait = float(slot_wait)
        self._sleep = sleep
        self._random = random_source
        self._on_attempt = on_attempt
        self._sessions: dict[str, Any] = {}
        self._session_lock = threading.Lock()

    def concurrency(self) -> int:
        """可以同时开几个请求 = 池子里**现在可用**的名额数（生成器拿它当工作线程数）。

        是个快照而不是保证：真正的名额始终由 `slot()` 把关，所以这里偏大只会让 worker
        多等一会儿，不会越权。取不到（鸭子类型的假 client、被换掉的池）就退回 1 ——
        串行是永远正确的退化。
        """
        try:
            return max(0, int(self.pool.available_concurrency()))
        except Exception:
            return 1

    # -- 主入口 ------------------------------------------------------------

    def chat(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: Mapping[str, Any] | None = None,
        rows: int = 1,
    ) -> LlmReply:
        """发一次请求（含缓存与故障转移）。失败抛出 `LlmError` 的子类。

        `rows` 只用于记账：这次请求处理了几行。它跟耗时一起进 EWMA，预估才能把历史延迟
        折算到用户这次配的批量上（见 `LlmEndpoint.ewma_rows`）。默认 1 —— 逐行生成与
        「测一下这个端点通不通」都是这个形状。
        """
        # 顺序很重要：**先查缓存再查预算**。缓存命中不发请求、不花钱，所以调用次数用尽
        # 不该让它失败 —— 否则续跑时把已经付过钱的答案也一起拒了，等于白花那笔钱。
        cache_key = None
        if self.cache is not None:
            cache_key = LlmCache.key(model or "*", temperature if temperature is not None else -1,
                                     system, prompt,
                                     -1 if max_tokens is None else max_tokens)
            hit = self.cache.get(cache_key)
            if hit is not None:
                return LlmReply(text=hit, endpoint_id="cache", seconds=0.0, cached=True)
        self.budget.check()

        tried: list[str] = []
        last_error: LlmError | None = None
        for _ in range(self.max_failover):
            if not self.pool.untried(tried):
                # 所有启用的端点都试过了。「换个端点再来一遍」到此为止 —— 再进 `slot()`
                # 只会等满 slot_wait 秒，然后拿到同一个结论。
                break
            try:
                with self.pool.slot(exclude=tried, wait=self.slot_wait) as endpoint:
                    tried.append(endpoint.id)
                    reply = self._call_endpoint(
                        endpoint, prompt, system=system, model=model,
                        temperature=temperature, max_tokens=max_tokens, extra_body=extra_body,
                        rows=rows,
                    )
                self.budget.note(ok=True)
                if cache_key is not None and self.cache is not None:
                    self.cache.put(cache_key, reply.text)
                return reply
            except LlmHttpError as exc:
                last_error = exc
                self.budget.note(ok=False)
                if not _should_failover(exc.status):
                    raise                        # 4xx（429 除外）换端点只会重复失败
            except LlmUnavailable as exc:
                # 池说的是「一个可用端点都没有」—— 这是池级结论，不是某个端点的问题。
                # 换端点换的还是同一批，而且 `slot()` 已经等满 slot_wait 秒了，再等三遍
                # 只会把一次故障拖成三分钟。直接上抛，交给上层护栏。
                self.budget.note(ok=False)
                raise
            except LlmNetworkError as exc:
                last_error = exc
                self.budget.note(ok=False)
            self.budget.check()
        if last_error is not None:
            raise last_error
        # 一个端点都没试成（全停用 / 全被排除）。这也算一次失败：端点池整体挂掉时，
        # 任务应当在护栏上中止，而不是安静地给每一行都写空值。
        self.budget.note(ok=False)
        raise LlmUnavailable("没有可用的大模型端点（都在冷却、达到并发上限或已停用）")

    def _call_endpoint(self, endpoint: LlmEndpoint, prompt: str, *, system: str,
                       model: str, temperature: float | None, max_tokens: int | None,
                       extra_body: Mapping[str, Any] | None, rows: int = 1) -> LlmReply:
        payload: dict[str, Any] = {
            "model": model or endpoint.model,
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + [{"role": "user", "content": prompt}],
            "temperature": endpoint.temperature if temperature is None else temperature,
        }
        if endpoint.top_p is not None:
            payload["top_p"] = endpoint.top_p
        limit = endpoint.max_tokens if max_tokens is None else max_tokens
        if limit:
            payload["max_tokens"] = limit
        payload.update(dict(endpoint.extra_body or {}))
        payload.update(dict(extra_body or {}))

        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        headers.update(endpoint.extra_headers or {})

        attempt = 0
        while True:
            started = time.monotonic()
            try:
                status, text, response_headers = self._transport(
                    endpoint.chat_url, headers, payload,
                    (endpoint.connect_timeout_s, endpoint.timeout_s), endpoint.proxy,
                    self._session(endpoint),
                )
            except Exception as exc:                 # 传输层异常一律当网络故障
                seconds = time.monotonic() - started
                error = LlmNetworkError(f"请求大模型失败（{endpoint.id}）：{exc}")
                self.pool.report_failure(endpoint, error)
                self._attempt(endpoint, "network", seconds, str(exc))
                if attempt >= endpoint.max_retries:
                    raise error from exc
                self._backoff(attempt)
                attempt += 1
                continue

            seconds = time.monotonic() - started
            if status >= 400:
                retry_after = _retry_after(response_headers)
                error = LlmHttpError(status, text, endpoint.id)
                self.pool.report_failure(endpoint, error, retry_after=retry_after)
                self._attempt(endpoint, "http", seconds, f"{status}")
                if status == 429 and retry_after:
                    self._sleep(min(retry_after, BACKOFF_CAP))     # 尊重服务端的要求
                if not _should_retry(status) or attempt >= endpoint.max_retries:
                    raise error
                self._backoff(attempt)
                attempt += 1
                continue

            content = _extract_content(text)
            if content is None:
                error = LlmNetworkError(f"响应里没有内容（{endpoint.id}）：{text[:200]}")
                self.pool.report_failure(endpoint, error)
                self._attempt(endpoint, "shape", seconds, "响应缺少 choices")
                if attempt >= endpoint.max_retries:
                    raise error
                self._backoff(attempt)
                attempt += 1
                continue

            self.pool.report_ok(endpoint, seconds, rows=rows)
            self._attempt(endpoint, "ok", seconds, "")
            return LlmReply(text=content, endpoint_id=endpoint.id, seconds=seconds)

    def _session(self, endpoint: LlmEndpoint) -> Any:
        """每端点一个 `requests.Session`，连接池大小与该端点的并发上限一致。

        复用连接在高频小请求上是几倍的差距；而连接池小于并发上限会让请求排队等连接，
        白白把并发上限变成摆设。
        """
        with self._session_lock:
            session = self._sessions.get(endpoint.id)
            if session is None:
                import requests
                from requests.adapters import HTTPAdapter

                session = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=endpoint.max_concurrency,
                    pool_maxsize=endpoint.max_concurrency,
                    max_retries=0,               # 重试由这里控制，不要两处各retry一遍
                )
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                self._sessions[endpoint.id] = session
            return session

    def _backoff(self, attempt: int) -> None:
        # 沿用 downloader.py 的退避形状，加上抖动：多个工作线程同时失败时不要一起回来
        self._sleep(min(BACKOFF_CAP, 1.2 * (2 ** attempt)) + self._random() * 0.8)

    def _attempt(self, endpoint: LlmEndpoint, outcome: str, seconds: float, detail: str) -> None:
        if self._on_attempt is None:
            return
        try:
            self._on_attempt({"endpoint": endpoint.id, "outcome": outcome,
                              "seconds": round(seconds, 3), "detail": detail})
        except Exception:
            logger.debug("尝试回调失败", exc_info=True)

    def close(self) -> None:
        with self._session_lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    pass
            self._sessions.clear()


def _should_retry(status: int) -> bool:
    """同一个端点上重试是否值得。429 值得（等一下就好），5xx 值得，4xx 不值得。"""
    return status == 429 or status >= 500


def _should_failover(status: int) -> bool:
    """换端点是否值得。4xx（429 除外）不值得 —— 那是请求或配置的问题。"""
    return status == 429 or status >= 500 or status == 408


def _retry_after(headers: Mapping[str, str] | None) -> float:
    if not headers:
        return 0.0
    raw = None
    for name, value in headers.items():
        if str(name).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 1.0                    # HTTP-date 形式：保守地等 1 秒


def default_transport(url: str, headers: Mapping[str, str], payload: Mapping[str, Any],
                      timeout: tuple[float, float], proxy: str, session: Any) -> tuple[int, str, Mapping[str, str]]:
    """默认传输层：`requests`。返回 `(状态码, 响应体, 响应头)`。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    response = session.post(url, headers=dict(headers), json=dict(payload),
                            timeout=timeout, proxies=proxies)
    return response.status_code, response.text, response.headers


def _extract_content(body: str) -> str | None:
    """从 OpenAI 兼容响应里取正文。

    兼容三种真实见过的形状：标准的 `choices[0].message.content`、推理模型把正文放在
    `choices[0].text`、以及 content 是分段数组（新版多模态接口）。
    """
    try:
        data = orjson.loads(body)
    except orjson.JSONDecodeError:
        return None
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return None
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):                # [{"type":"text","text":...}]
            parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "".join(parts)
    text = first.get("text")
    if isinstance(text, str):
        return text
    return None


# =========================================================================== 生成器


_SYSTEM_DEFAULT = (
    "你是一个数据清洗助手。只输出被要求的内容本身，不要解释、不要加引号、不要加前缀。"
)


class LlmGenerator(BaseGenerator):
    """按字段声明生成。`batch_size > 1` 时一次请求处理多行，返回 JSON 数组。

    一次请求生成多行能省 5-20 倍的 token 与延迟（system 提示只发一遍），代价是
    「模型没按格式返回」时的处理成本 —— 所以批量解析失败会退回到逐行，而不是整批放弃。

    **一批之内是并发的**（`generate`）：端点池写着「最多同时几个请求」，就该同时发几个 ——
    否则 `max_concurrency` 只是个摆设，一串请求前一个的 RTT 全是白等。并发范围刻意压在
    这个类里：只有它知道 `batch_size`，而引擎（分块、检查点、逐字段记账）因此仍然是单线程的。
    """

    # 批量响应不是 JSON 数组时，允许连续几次窗口退回逐行；超过就停手（见 `_parse_streak`）
    PARSE_FALLBACK_LIMIT = 2

    def __init__(
        self,
        spec: FieldSpec,
        rule: GenerateRule,
        seed: int,
        *,
        client: LlmClient,
        task: LlmTaskOptions | None = None,
        on_note: Callable[[str, str], None] | None = None,
    ):
        super().__init__(spec.dest, rule, seed=seed)
        self.client = client
        self.task = task or LlmTaskOptions()
        self.spec = spec
        self.output_fields = tuple(rule.output_fields) or (spec.dest,)
        self.batch_size = max(1, int(rule.batch_size or 1))
        self.system = rule.system or _SYSTEM_DEFAULT
        self._on_note = on_note
        # 连续几次批量解析失败（见 PARSE_FALLBACK_LIMIT）。工作线程会同时加减它，所以要锁；
        # 这一处是并发里唯一必须共享的可变状态 —— 别在 worker 里再碰别的东西。
        self._parse_streak = 0
        self._parse_lock = threading.Lock()
        if not (rule.prompt or "").strip():
            raise CleanGenerateError(f"字段「{spec.dest}」的大模型生成没有写 prompt")

    # -- 校验 --------------------------------------------------------------

    def validate(self, available: Sequence[str]) -> None:
        for text, label in ((self.rule.prompt, "prompt"), (self.rule.system, "system")):
            if not text:
                continue
            problems = validate_template(text, available)
            if problems:
                raise CleanGenerateError(
                    f"字段「{self.spec.dest}」的 {label}：" + "；".join(problems)
                )
        if self.batch_size > 1 and len(self.output_fields) != 1:
            # 多字段 + 批量会让「数组里第 i 项的键」这件事变得难以约束，收益也不明显
            raise CleanGenerateError(
                f"字段「{self.spec.dest}」同时用了批量生成与多输出字段；"
                "请把 batch_size 设为 1，或只保留一个输出字段"
            )

    def describe(self) -> str:
        return f"大模型（批量 {self.batch_size}，输出 {'、'.join(self.output_fields)}）"

    # -- 生成 --------------------------------------------------------------

    def generate(self, batch: GenerationBatch) -> list[dict[str, Any]]:
        """切工作项 → 按池子的可用并发派发 → 按**下标**回填。

        两条路（批量窗口与单行）都走同一个派发器。只并发「窗口」那条是不行的：多输出字段
        被 `validate` 强制成 `batch_size=1`，那正是调用次数最多、最慢的配置，漏了它等于没改。

        回填在主线程按 `(start, stop)` 做，与完成顺序无关 —— 谁先回来不影响结果放在哪一行。
        """
        results: list[dict[str, Any]] = [self._blank() for _ in batch.indices]
        plans = self._plans(batch)
        width = self._width(len(plans))
        if width <= 1:
            # 宽度 1（单并发端点、池子全在冷却、只有一个工作项）走纯串行：与并发之前逐字节相同
            for start, stop, work in plans:
                results[start:stop] = work()
            return results
        # 线程池按调用建/销毁：一次 `generate()` 就是引擎的一个分块，摊在秒级请求上可以忽略。
        # 约束：同一个生成器实例不允许被两个线程同时调 `generate()`（引擎是单线程调用的）。
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="clean-llm") as pool:
            futures = [(start, stop, pool.submit(work)) for start, stop, work in plans]
            # 按提交顺序收，不按完成顺序：位置本来就由下标决定，而固定顺序让异常以确定的方式
            # 抛出（可调试、可测试）。代价只是多等几个已经跑完的 future。
            for start, stop, future in futures:
                results[start:stop] = future.result()
        return results

    def _plans(self, batch: GenerationBatch) -> list[tuple[int, int, Callable[[], list[dict[str, Any]]]]]:
        """切成 `(起, 止, thunk)`：thunk 只负责算出这一段的取值，不碰任何实例状态。"""
        size = self.batch_size if self.batch_size > 1 else 1
        total = len(batch.indices)
        plans = []
        for start in range(0, total, size):
            stop = min(start + size, total)
            plans.append((start, stop, self._work(batch, start, stop)))
        return plans

    def _work(self, batch: GenerationBatch, start: int, stop: int) -> Callable[[], list[dict[str, Any]]]:
        window = slice(start, stop)
        if self.batch_size > 1:
            def run() -> list[dict[str, Any]]:
                return self._generate_chunk(GenerationBatch(
                    indices=batch.indices[window], rows=batch.rows[window],
                    attempt=batch.attempt,
                    feedback=batch.feedback[window] if batch.feedback else None,
                ))
            return run

        def run() -> list[dict[str, Any]]:
            return [
                self._generate_one(batch.indices[position], batch.rows[position], batch.attempt,
                                   batch.feedback[position] if batch.feedback else None)
                for position in range(start, stop)
            ]
        return run

    def _width(self, plans: int) -> int:
        """这次派发开几个工作线程：池子**现在**可用的名额数，且不超过工作项数。"""
        if plans <= 1:
            return 1
        try:
            limit = int(self.client.concurrency())
        except Exception:                 # 鸭子类型的假 client：退回串行
            return 1
        return max(1, min(limit, plans))

    def _blank(self) -> dict[str, Any]:
        return {name: None for name in self.output_fields}

    def _generate_one(self, index: int, row: Mapping[str, Any], attempt: int,
                      feedback: str | None) -> dict[str, Any]:
        prompt = self._render(row, feedback)
        try:
            reply = self.client.chat(
                prompt, system=self.system, temperature=self._temperature(),
                max_tokens=self._max_tokens(), rows=1,
            )
        except LlmError as exc:
            self._note(f"字段「{self.field_name}」第 {index} 行生成失败：{exc}")
            return self._blank()
        return self._parse_single(reply.text)

    def _generate_chunk(self, chunk: GenerationBatch) -> list[dict[str, Any]]:
        rows = chunk.rows
        prompt = self._render_many(rows, chunk.feedback)
        try:
            reply = self.client.chat(
                prompt, system=self.system, temperature=self._temperature(),
                max_tokens=self._max_tokens(), rows=len(rows),
            )
        except LlmError as exc:
            self._note(f"字段「{self.field_name}」一批 {len(rows)} 行生成失败：{exc}")
            return [self._blank() for _ in rows]
        parsed = self._parse_many(reply.text, len(rows))
        if parsed is not None:
            with self._parse_lock:
                self._parse_streak = 0
            return parsed
        # 批量格式没守住：退回逐行。批量省下的钱在这里又花回去一点，但比整批作废好。
        # 但只救前几次：并发派发下几个窗口会同时坏掉，一轮就能烧掉 batch_size × 窗口数 次调用，
        # 而告警是去重的 —— 用户只看到一条，账单上看不见。连续坏到额度就不再逐行。
        streak = self._parse_failed_again()
        if streak > self.PARSE_FALLBACK_LIMIT:
            self._note(
                f"字段「{self.field_name}」的批量响应连续 {streak} 次不是合法 JSON 数组，"
                "已停止逐行重试（这一批留空；请检查 prompt 是否明确要求返回 JSON 数组）"
            )
            return [self._blank() for _ in rows]
        self._note(
            f"字段「{self.field_name}」的批量响应不是合法 JSON 数组，已退回逐行重试"
        )
        return [
            self._generate_one(index, row, chunk.attempt, None)
            for index, row in zip(chunk.indices, rows)
        ]

    def _parse_failed_again(self) -> int:
        """记一次「批量解析失败」，返回**含这次在内**的连续失败次数。

        在工作线程里调用，所以计数与判定必须在同一把锁内 —— 否则两个同时失败的窗口会
        各自认为自己是第一次（并发下这正是常态）。返回值一起带出来，是为了让调用方
        写日志时不必再到锁外读一次 `_parse_streak`。
        """
        with self._parse_lock:
            self._parse_streak += 1
            return self._parse_streak

    # -- 渲染 --------------------------------------------------------------

    def _render(self, row: Mapping[str, Any], feedback: str | None) -> str:
        text = render_template(self.rule.prompt, row)
        if not feedback:
            return text
        # 把上一次为什么不合格说清楚，比让模型盲重采样有效得多（沿用 ai_native 的形状）
        return f"{text}\n\n上一次的输出不合格：{feedback}\n请据此修正后重新输出。"

    def _render_many(self, rows: Sequence[Mapping[str, Any]], feedback: Sequence[str] | None) -> str:
        lines = []
        for position, row in enumerate(rows, 1):
            body = render_template(self.rule.prompt, row)
            if feedback and position <= len(feedback) and feedback[position - 1]:
                body += f"（上一次输出不合格：{feedback[position - 1]}）"
            lines.append(f"{position}. {body}")
        fields = "、".join(self.output_fields)
        return (
            f"下面是 {len(rows)} 条待处理的数据，逐条处理。\n"
            + "\n".join(lines)
            + f"\n\n只输出一个 JSON 数组，共 {len(rows)} 个元素，顺序与上面一致；"
            f"每个元素是一个对象，键为 {fields}。不要输出任何其它文字。"
        )

    def _temperature(self) -> float | None:
        if self.rule.temperature is not None:
            return self.rule.temperature
        return self.task.temperature

    def _max_tokens(self) -> int | None:
        if self.rule.max_tokens is not None:
            return self.rule.max_tokens
        return self.task.max_tokens

    # -- 解析 --------------------------------------------------------------

    def _parse_single(self, text: str) -> dict[str, Any]:
        """单行输出：先按 JSON 对象试，再退回到「整段文本就是第一个字段的值」。"""
        cleaned = (text or "").strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                data = orjson.loads(cleaned)
                if isinstance(data, dict):
                    return {name: data.get(name) for name in self.output_fields}
            except orjson.JSONDecodeError:
                pass
        if len(self.output_fields) == 1:
            return {self.output_fields[0]: _strip_wrapping(cleaned)}
        self._note(f"字段「{self.field_name}」的输出不是合法 JSON 对象：{cleaned[:120]}")
        return {name: None for name in self.output_fields}

    def _parse_many(self, text: str, count: int) -> list[dict[str, Any]] | None:
        items = _json_array(text)
        if items is None or len(items) != count:
            return None
        parsed: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                parsed.append({name: item.get(name) for name in self.output_fields})
            elif isinstance(item, str) and len(self.output_fields) == 1:
                parsed.append({self.output_fields[0]: _strip_wrapping(item)})
            else:
                return None
        return parsed

    def _note(self, message: str) -> None:
        if self._on_note is not None:
            try:
                self._on_note("warn", message)
            except Exception:
                logger.debug("生成器日志回调失败", exc_info=True)


# 整段被包住时该脱掉的成对符号。注意「成对」而不只是「首尾相同」：中文引号的首尾是两个
# 不同字符，「同一字符」的写法对它们是无效的（这正是这段代码第一版漏掉的情况）。
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}


def _strip_wrapping(text: str) -> str:
    """去掉模型爱加的包裹：整行引号、代码围栏。

    只在**整段**被包住时脱：值内部带引号是正常内容，不能动。
    """
    value = (text or "").strip()
    if value.startswith("```"):
        lines = [line for line in value.splitlines() if not line.strip().startswith("```")]
        value = "\n".join(lines).strip()
    if len(value) >= 2 and _QUOTE_PAIRS.get(value[0]) == value[-1]:
        value = value[1:-1].strip()
    return value


def _json_array(text: str) -> list[Any] | None:
    """从可能夹带说明文字的输出里抠出 JSON 数组。

    先整体解析（最理想），失败再取第一个 `[` 到最后一个 `]` —— 模型经常在数组前后
    各写一句「好的，以下是结果」。
    """
    cleaned = _strip_wrapping(text)
    for candidate in (cleaned, _outermost_array(cleaned)):
        if not candidate:
            continue
        try:
            data = orjson.loads(candidate)
        except orjson.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None


def _outermost_array(text: str) -> str:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return ""
    return text[start:end + 1]


# =========================================================================== 预估


@dataclass
class LlmEstimate:
    """提交前的预估。**给区间，不给单点** —— 单点在 GB 级必然是谎话。

    `seconds_low/high` 是 `None` 而不是 `0.0`，专门表示「这一项给不出时间」：没有大模型
    字段时，耗时完全由磁盘、CPU 与用户函数决定，这里一个数字都算不出来。写 0.0 会被界面
    原样显示成「约 0 秒」，而用户接下来要等的是十分钟 —— **0 是谎话，null 才是诚实的**。
    """

    rows: int
    generated_fields: int
    batch_size: int
    calls: int
    rounds: int
    calls_capped: bool
    seconds_low: float | None
    seconds_high: float | None
    concurrency: float
    avg_latency_ms: float
    notes: list[str] = field(default_factory=list)

    @property
    def hours_low(self) -> float | None:
        return None if self.seconds_low is None else self.seconds_low / 3600.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "generated_fields": self.generated_fields,
            "batch_size": self.batch_size,
            "calls": self.calls,
            "rounds": self.rounds,
            "calls_capped": self.calls_capped,
            "seconds_low": None if self.seconds_low is None else round(self.seconds_low, 1),
            "seconds_high": None if self.seconds_high is None else round(self.seconds_high, 1),
            "concurrency": self.concurrency,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "notes": list(self.notes),
        }


DEFAULT_LATENCY_MS = 2000.0
# 「批效率」：批量请求的延迟不会随批大小线性增长，但也不是常数。经验上 0.6 次方比较接近
# 真实（一次 5 行的请求大约是单行的 2.5 倍耗时），这也是批量能省时间的原因。
BATCH_EFFICIENCY = 0.6


def estimate_llm(
    config: CleanTaskConfig,
    pool: LlmPool | None = None,
    *,
    rows: int,
    concurrency: float | None = None,
    avg_latency_ms: float | None = None,
) -> LlmEstimate:
    """预估大模型部分的墙钟与调用次数。

    墙钟 = Σ_字段 (轮数 × 每次请求耗时)。三件事必须一起看才对得上实测：

    - **字段之间是串行的**（引擎按字段顺序逐个生成整列），所以逐字段算完再相加，不能拿
      总调用数除以并发 —— 那会把「第 2 个字段只能等第 1 个字段跑完」这件事抹掉。
    - **轮数不是 `调用数 / 并发`**，还要受分块约束：引擎按 `commit_rows` 切块、每块单独
      派发，所以 5 万行 × 批量 2000 = 25 个块，每块只有 1 个请求，再高的并发也只能一轮一个。
    - **单次耗时用历史延迟本身**，只在用户改了批量、且是**改大**的时候按批效率折算。
      旧版把「10 行一批量出来的 4856 ms」又按批量折了一次当成单行耗时，实际 50 行跑了
      23.6 秒，而面板上写的是「2 秒 – 5 秒」。

    调用次数会被 `sample_rows` 与 `llm_max_calls` 截断 —— 截断必须显示出来，否则用户会以为
    「任务跑完了 = 每行都生成了」。上限按**字段顺序**分配：引擎里就是第一个字段先花完，
    后面的字段连一次请求都发不出去。

    `pool` 与 `concurrency` 二选一：前者从端点池读真实并发与历史延迟，后者给「还没建池
    但要先算个数」的场景（例如 `/estimate` 只拿到了端点 id 列表）。
    """
    options = config.llm
    generators = [spec for spec in config.fields
                  if spec.generate is not None and spec.generate.kind == "llm"]
    if not generators:
        # 没有大模型字段 = 没有可估算的那一部分。时间照样会花掉（磁盘、CPU、用户函数），
        # 但那不属于这里能算的东西，所以给 None 而不是 0 —— 见 LlmEstimate 的 docstring。
        return LlmEstimate(
            rows=rows, generated_fields=0, batch_size=1, calls=0, rounds=0,
            calls_capped=False, seconds_low=None, seconds_high=None, concurrency=0.0,
            avg_latency_ms=0.0,
            notes=["没有大模型字段，这里给不出处理时间：耗时取决于磁盘、CPU 与自定义函数，"
                   "提交后按实际处理速率为准"],
        )

    notes: list[str] = []
    rows_effective = min(rows, options.sample_rows) if options.sample_rows else rows
    if options.sample_rows and rows_effective < rows:
        notes.append(
            f"只对前 {options.sample_rows} 行生成，其余行留空（调用次数按此降低）"
        )
    # 未截断的调用数，只用来判断「上限有没有真的砍掉东西」（截断后是恒等的）
    uncapped = sum(_planned_count(spec, rows_effective) for spec in generators)

    budget: int | None = int(options.max_calls or 0) or None
    # 批量只对「单输出字段」生效（多输出 + 批量在生成器里就被拒了），所以按字段分别算。
    # 上限按字段顺序分：引擎里就是第一个字段先花完，后面的字段连一次请求都发不出去。
    plan: list[tuple[int, int]] = []                 # (本字段的批量, 本字段的调用数)
    for spec in generators:
        count = _planned_count(spec, rows_effective)
        if budget is not None:
            count = min(count, budget)
            budget -= count
        plan.append((_planned_batch(spec), count))
    calls = sum(count for _, count in plan)
    calls_capped = calls < uncapped
    if calls_capped:
        notes.append(
            f"调用次数 {uncapped} 超过硬上限 {options.max_calls}，超出部分不会发起 ——"
            "这部分字段会留空，报告里会标注"
        )

    total_concurrency = (float(pool.total_concurrency()) if pool is not None
                         else float(concurrency or 0.0))
    latency = avg_latency_ms or (pool.average_latency_ms() if pool is not None else None) \
        or DEFAULT_LATENCY_MS
    if avg_latency_ms is None and (pool is None or pool.average_latency_ms() is None):
        notes.append(f"没有历史延迟，按每次 {DEFAULT_LATENCY_MS / 1000:.1f} 秒保守估算")
    observed = pool.average_batch() if pool is not None else 0.0
    chunk_rows = int(config.output.commit_rows or 0) or COMMIT_ROWS_LLM
    width = max(1.0, total_concurrency)

    low = high = 0.0
    rounds = 0
    for planned, count in plan:
        if count <= 0:
            continue                                 # 上限已经被前面的字段吃光
        field_rounds = _rounds(count, width, rows_effective, chunk_rows)
        per_call = _per_call_ms(latency, planned, observed)
        rounds += field_rounds
        # 乐观/悲观两套参数：端点健康 vs 有重试与抖动（0.7 与 1.8 两个旋钮沿用原值）
        low += field_rounds * _per_call_ms(latency * 0.7, planned, observed) / 1000.0
        high += field_rounds * (_per_call_ms(latency * 1.8, planned, observed) + 800.0) / 1000.0
    if total_concurrency <= 0:
        # 0 是谎话（界面会显示「约 0 秒」），null 才是诚实的 —— 见 LlmEstimate 的 docstring
        notes.append("没有可用端点，无法估算处理时间 —— 请先在模型池里启用至少一个端点")
        low = high = None

    notes.append("这里只估大模型请求的时间：读文件、写文件、自定义函数与约束检查都在此之外")
    batch_size = max((planned for planned, _ in plan), default=1)
    return LlmEstimate(rows=rows, generated_fields=len(generators), batch_size=batch_size,
                       calls=calls, rounds=rounds, calls_capped=calls_capped,
                       seconds_low=low, seconds_high=high, concurrency=total_concurrency,
                       avg_latency_ms=latency, notes=notes)


def _planned_batch(spec: FieldSpec) -> int:
    """这个字段一次请求处理几行。多输出字段被生成器强制成 1（见 `LlmGenerator.validate`）。"""
    rule = spec.generate
    if len(rule.output_fields or [spec.dest]) > 1:
        return 1
    return max(1, int(rule.batch_size or 1))


def _planned_count(spec: FieldSpec, rows: int) -> int:
    """这个字段要发几次请求（未截断）。"""
    return max(1, -(-rows // _planned_batch(spec)))


def _rounds(calls: int, width: float, rows: int, chunk_rows: int) -> int:
    """几轮才发得完：并发决定一轮几个请求，分块决定一轮几个请求**能**发。

    两个下界取大：`calls / width`（并发上限）与 `rows / chunk_rows`（引擎是逐块派发的，
    一块之内才谈得上并发）。
    """
    chunks = max(1, -(-rows // max(1, chunk_rows)))
    return max(-(-calls // int(max(1.0, width))), chunks)


def _per_call_ms(latency_ms: float, planned: int, observed: float) -> float:
    """把「一次 `observed` 行的请求耗时」折算成「一次 `planned` 行」的耗时。

    相等时就是原值 —— 这正是旧公式错的地方：它把 `ewma_ms`（本来就是「一次请求」的耗时）
    当成单行耗时又乘了一次批量折扣。

    批量**调大**时按 `BATCH_EFFICIENCY` 折（批量省时间的收益就在这里）；调小时不给折扣：
    拆成更小的请求只会更慢，而慢多少没有依据 —— 按「每次还是要这么久」算是诚实的上界，
    也正是「不批量」的真实代价（用户实测的 5 次 × 4856 ms = 23.6 秒就是这么来的）。
    """
    if observed <= 0 or planned <= observed:
        return latency_ms
    return latency_ms * ((planned / observed) ** (BATCH_EFFICIENCY - 1.0))


def build_llm_factory(
    client: LlmClient,
    task: LlmTaskOptions | None = None,
    *,
    on_note: Callable[[str, str], None] | None = None,
) -> Callable[[FieldSpec, GenerateRule, int], LlmGenerator]:
    """给 `clean_generate.build_generators` 用的工厂。"""

    def factory(spec: FieldSpec, rule: GenerateRule, seed: int) -> LlmGenerator:
        return LlmGenerator(spec, rule, seed, client=client, task=task, on_note=on_note)

    return factory
