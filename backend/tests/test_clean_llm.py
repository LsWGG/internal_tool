"""大模型池：负载均衡、熔断半开、故障转移策略、缓存、护栏、预估。

这个文件里的每一条都在验证一件「配错了会静默出错」的事：

- **4xx 不转移**：如果 400（比如模型名写错）也去换端点重试，用户看到的是「慢」，而不是
  「你的 model 填错了」—— 错误被三次重试和两个端点稀释掉了。
- **熔断真的会熔断**：一个挂了的端点如果每次还被选中、每次失败、每次退避，任务就会以
  「全都在重试」的速度爬完几百万行。
- **预算耗尽后确定性完成**：硬上限的意义是「停止发请求并如实收尾」，不是「卡住」。
- **缓存命中不重复计费**：续跑会重算崩溃窗口内的行，没有持久缓存就是白花钱。
- **密钥不出现在任何序列化出口**：`public()` 是给 API 和报告的，`stored()` 是给 0600 端点池
  文件的，两者搞混一次就是把密钥写进 `tasks.json`（每次进度更新都重写、还被列表接口返回）。

网络用注入的 `transport` 完全替代，所以本文件不发任何真实请求。
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clean_generate import GenerationBatch
from app.clean_llm import (
    BATCH_EFFICIENCY,
    SENTINEL,
    CallBudget,
    LlmAbortError,
    LlmBudgetExhausted,
    LlmCache,
    LlmClient,
    LlmEndpoint,
    LlmError,
    LlmGenerator,
    LlmHttpError,
    LlmNetworkError,
    LlmPool,
    LlmUnavailable,
    _extract_content,
    _json_array,
    _retry_after,
    build_llm_factory,
    circuit_cooldown,
    estimate_llm,
    load_pool,
    merge_endpoint,
    save_pool,
)
from app.clean_models import (
    CleanTaskConfig,
    FieldSpec,
    GenerateRule,
    LlmTaskOptions,
    SourceSpec,
)

from pydantic import ValidationError

SECRET = "sk-do-not-leak-12345"


# =========================================================================== 辅助


class Clock:
    """可拨的假时钟。熔断冷却、令牌桶都靠它，测试才不用真的睡 15 秒。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def endpoint(endpoint_id: str, *, concurrency: int = 4, weight: int = 1, **kwargs) -> LlmEndpoint:
    kwargs.setdefault("max_concurrency", concurrency)
    kwargs.setdefault("weight", weight)
    return LlmEndpoint(id=endpoint_id, url=f"https://{endpoint_id}.test/v1", model="m", **kwargs)


def reply(text: str, status: int = 200, headers: dict | None = None):
    """造一个 OpenAI 兼容响应。"""
    body = json.dumps({"choices": [{"message": {"content": text}}]})
    return status, body, headers or {}


class ScriptedTransport:
    """按脚本回答的传输层，并记录每一次调用。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, url, headers, payload, timeout, proxy, session):
        with self._lock:
            self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload),
                               "timeout": timeout, "proxy": proxy})
            position = len(self.calls) - 1
        step = self.script[position] if position < len(self.script) \
            else self.script[-1] if self.script else reply("ok")
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(url, headers, payload)
        return step


class Sleeping:
    """记录退避时长，不真的睡。"""

    def __init__(self):
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.slept)


def rows_in(prompt: str) -> list[int]:
    """prompt 里出现的行号，按出现顺序。行内容由 `batch()` 造（`第{i}条`），单行 prompt 就是它
    本身，批量 prompt 是「1. 第0条 / 2. 第1条」这样的编号列表。"""
    return [int(match) for match in re.findall(r"第(\d+)条", prompt)]


class ParallelTransport:
    """按 prompt **内容**回答的传输层，并记录**最大在途数**。

    并发下不能再用 `ScriptedTransport`：它拿 `len(self.calls)` 当回放位置，而在途顺序不等于提交
    顺序，回放会错位 —— 于是「哪一行拿到哪个答案」变成看运气。这里改成看请求里写了哪一行来决定
    答案，于是在途多少、谁先回来都不影响内容。

    `expected`：把这么多次调用一起卡在闸门上，凑齐才放行。不加闸门的话，「最大在途数」就只能靠
    线程调度的运气，断定时绿时红。凑不齐（串行实现正是如此）就超时放行 —— 让断言去报错，而不是
    把测试挂住（只有第一次等待会真的等满，闸门一旦破了后面的等待立刻返回）。

    `finished` 记完成顺序（取该次请求的第一个行号），用来验证「回填按位置、不按完成顺序」。
    """

    def __init__(self, answer, *, expected: int = 0, slow=None, gate_timeout: float = 5.0):
        self.answer = answer                      # answer(rows: list[int]) -> str
        self.slow = slow or (lambda rows: 0.0)    # 让某几次调用慢下来，制造乱序完成
        self.gate_timeout = gate_timeout
        self.gate = threading.Barrier(expected) if expected > 1 else None
        self.calls: list[dict] = []
        self.finished: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, url, headers, payload, timeout, proxy, session):
        prompt = payload["messages"][-1]["content"]
        rows = rows_in(prompt)
        with self._lock:
            self.calls.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                try:
                    self.gate.wait(self.gate_timeout)
                except threading.BrokenBarrierError:
                    pass                          # 没凑齐：放行，让最大在途数的断言去失败
            delay = self.slow(rows)
            if delay:
                time.sleep(delay)
            with self._lock:
                self.finished.append(rows[0] if rows else -1)
            return reply(self.answer(rows))
        finally:
            with self._lock:
                self.in_flight -= 1


# =========================================================================== 选择与均衡


class SelectionTests(unittest.TestCase):
    """加权最小在途：候选、得分、占名额。"""

    def test_idle_endpoints_split_by_capacity(self):
        """把两个端点的名额都用满，各自应当刚好占到自己 `max_concurrency`。

        这就是「按最大并发分配」的断言：如果选择器写成了简单轮询，2 号会拿到 4 个、
        1 号拿到 4 个（或者按重量级平均分），并发上限就成了摆设。
        """
        pool = LlmPool([endpoint("a", concurrency=2), endpoint("b", concurrency=6)])
        taken = []
        for _ in range(8):
            ep = pool.choose()
            self.assertIsNotNone(ep, "还有名额却没得选")
            taken.append(ep.id)
        self.assertEqual(taken.count("a"), 2)
        self.assertEqual(taken.count("b"), 6)
        self.assertIsNone(pool.choose(), "名额用完了还选得出端点")

    def test_weight_biases_the_selection_ratio(self):
        """权重是「我更信这个端点」的旋钮：权重 4 的端点应当拿到约 4 倍的请求。

        注意它是**比例**，不是额外名额 —— `max_concurrency` 才是同时在途上限（见
        `test_weight_does_not_raise_the_concurrency_ceiling`）。前四次的分配是确定的：
        三次给 a、一次给 b。
        """
        pool = LlmPool([endpoint("a", concurrency=8, weight=4), endpoint("b", concurrency=8)])
        taken = [pool.choose().id for _ in range(4)]
        self.assertEqual(taken.count("a"), 3, f"权重没有起作用：{taken}")
        self.assertEqual(taken.count("b"), 1, f"权重没有起作用：{taken}")
        self.assertEqual(pool.total_concurrency(), 16)

    def test_weight_does_not_raise_the_concurrency_ceiling(self):
        """权重 100 也不该让一个 `max_concurrency=1` 的端点同时跑两个请求。"""
        pool = LlmPool([endpoint("a", concurrency=1, weight=100)])
        self.assertEqual(pool.total_concurrency(), 1)
        self.assertIsNotNone(pool.choose())
        self.assertIsNone(pool.choose(), "权重绕过了并发上限")

    def test_the_less_loaded_endpoint_is_preferred(self):
        """同样的配置下，在途少的那个先被选中 —— 慢端点自然会堆积在途、自动被少选。"""
        pool = LlmPool([endpoint("a", concurrency=4), endpoint("b", concurrency=4)])
        first = pool.choose()
        second = pool.choose()
        self.assertNotEqual(first.id, second.id, "两次都选了同一个端点，没有按在途数平衡")
        third = pool.choose()
        self.assertIn(third.id, {"a", "b"})

    def test_a_disabled_endpoint_is_never_chosen(self):
        pool = LlmPool([endpoint("a", concurrency=4, enabled=False), endpoint("b", concurrency=1)])
        for _ in range(1):
            self.assertEqual(pool.choose().id, "b")
        self.assertIsNone(pool.choose())
        self.assertEqual(pool.total_concurrency(), 1)

    def test_an_endpoint_with_a_full_circuit_is_skipped(self):
        """熔断的端点不在候选里 —— 是「不发请求」，不是「发了再失败」。"""
        clock = Clock()
        pool = LlmPool([endpoint("a"), endpoint("b")], time_source=clock)
        for _ in range(3):
            pool.report_failure(pool.endpoints[0], LlmNetworkError("boom"))
        chosen = [pool.choose().id for _ in range(4)]
        self.assertNotIn("a", chosen)
        self.assertEqual(pool.choose(), None, "b 的名额用完了，却还有别的候选")

    def test_a_rate_limited_endpoint_waits_for_its_bucket(self):
        """`rpm=60` 的桶容量就是 60（一分钟的量），所以是「60 次之后开始限速」——
        这一条同时说明了桶容量，免得后来者以为桶里只有 1 个令牌。"""
        clock = Clock()
        pool = LlmPool([endpoint("a", concurrency=200, rpm=60)], time_source=clock)
        taken = sum(1 for _ in range(60) if pool.choose() is not None)
        self.assertEqual(taken, 60)
        self.assertIsNone(pool.choose(), "令牌桶没拦住第 61 次请求")
        clock.advance(1.0)
        self.assertIsNotNone(pool.choose(), "等够一秒了还是没有令牌")
        self.assertIsNone(pool.choose(), "一秒只该攒出一个令牌")

    def test_slot_releases_its_place(self):
        pool = LlmPool([endpoint("a", concurrency=1)])
        with pool.slot() as ep:
            self.assertEqual(ep.id, "a")
            self.assertEqual(pool.state("a").in_flight, 1)
        self.assertEqual(pool.state("a").in_flight, 0)

    def test_slot_waits_for_a_place_and_then_succeeds(self):
        """并发满了要**等**，而不是立刻失败 —— 大模型请求本来就要排队。"""
        pool = LlmPool([endpoint("a", concurrency=1)])
        acquired = threading.Event()

        def other():
            with pool.slot(wait=5.0) as ep:
                self.assertEqual(ep.id, "a")
                acquired.set()

        with pool.slot():
            waiter = threading.Thread(target=other)
            waiter.start()
            # 名额在主线程手里，且释放严格晚于这里 —— 等待线程不可能抢先拿到
            self.assertFalse(acquired.wait(0.15), "并发上限是 1，第二个名额不可能立刻拿到")
        self.assertTrue(acquired.wait(2.0), "名额归还后等待者没有被唤醒")
        waiter.join(timeout=5.0)
        self.assertEqual(pool.state("a").in_flight, 0)

    def test_no_available_endpoint_raises_instead_of_hanging(self):
        pool = LlmPool([endpoint("a", enabled=False)])
        with self.assertRaises(LlmUnavailable):
            with pool.slot(wait=0.05):
                pass

    def test_the_in_flight_count_returns_to_zero_and_the_place_is_reusable(self):
        """名额泄漏的后果是并发上限慢慢变成 0，任务在「没有可用端点」上卡死。"""
        pool = LlmPool([endpoint("a", concurrency=1)])
        for _ in range(5):
            with pool.slot():
                pass
        self.assertEqual(pool.state("a").in_flight, 0)
        self.assertIsNotNone(pool.choose())


class CircuitTests(unittest.TestCase):
    """连续失败 → 熔断 → 冷却 → 半开探测 → 恢复或重新熔断。"""

    def test_cooldown_grows_and_is_capped(self):
        self.assertEqual(circuit_cooldown(1), 0.0)
        self.assertEqual(circuit_cooldown(2), 0.0)
        self.assertEqual(circuit_cooldown(3), 15.0)
        self.assertEqual(circuit_cooldown(4), 30.0)
        self.assertEqual(circuit_cooldown(5), 60.0)
        self.assertEqual(circuit_cooldown(20), 300.0, "冷却时间必须有上限")

    def test_two_failures_are_not_enough_to_trip_it(self):
        """偶发抖动不该让一个健康端点下线。"""
        clock = Clock()
        pool = LlmPool([endpoint("a")], time_source=clock)
        for _ in range(2):
            pool.report_failure(pool.endpoints[0], LlmNetworkError("jitter"))
        self.assertIsNotNone(pool.choose())
        self.assertFalse(pool.snapshot()[0]["circuit_open"])

    def test_three_failures_open_the_circuit_and_then_it_half_opens(self):
        clock = Clock()
        pool = LlmPool([endpoint("a")], time_source=clock)
        for _ in range(3):
            pool.report_failure(pool.endpoints[0], LlmNetworkError("down"))
        self.assertTrue(pool.snapshot()[0]["circuit_open"])
        self.assertIsNone(pool.choose(), "冷却期内还在被选中")

        clock.advance(15.0)                    # 冷却结束
        probe = pool.choose()
        self.assertIsNotNone(probe, "冷却结束后不放探测请求，端点就永远回不来")
        self.assertTrue(pool.state("a").probing)

    def test_half_open_admits_only_one_probe(self):
        """半开时只放一个请求进去 —— 否则刚恢复的端点会被瞬间打满。"""
        clock = Clock()
        pool = LlmPool([endpoint("a", concurrency=8)], time_source=clock)
        for _ in range(3):
            pool.report_failure(pool.endpoints[0], LlmNetworkError("down"))
        clock.advance(15.0)
        self.assertIsNotNone(pool.choose())
        self.assertIsNone(pool.choose(), "半开状态放进了第二个探测请求")

    def test_a_successful_probe_closes_the_circuit(self):
        clock = Clock()
        pool = LlmPool([endpoint("a", concurrency=4)], time_source=clock)
        for _ in range(3):
            pool.report_failure(pool.endpoints[0], LlmNetworkError("down"))
        clock.advance(15.0)
        probe = pool.choose()
        pool.report_ok(probe, 0.4)
        self.assertFalse(pool.snapshot()[0]["circuit_open"])
        self.assertEqual(pool.state("a").in_flight, 1)
        self.assertIsNotNone(pool.choose())

    def test_a_failed_probe_re_opens_the_circuit(self):
        clock = Clock()
        pool = LlmPool([endpoint("a", concurrency=4)], time_source=clock)
        for _ in range(3):
            pool.report_failure(pool.endpoints[0], LlmNetworkError("down"))
        clock.advance(15.0)
        # 探测请求本身也要占名额：半开状态只放它一个进去，所以这里必须用 slot 而不是 choose
        with pool.slot() as probe:
            self.assertEqual(probe.id, "a")
            pool.report_failure(probe, LlmNetworkError("still down"))
        self.assertTrue(pool.snapshot()[0]["circuit_open"])
        self.assertIsNone(pool.choose())

    def test_retry_after_extends_the_cooldown(self):
        clock = Clock()
        pool = LlmPool([endpoint("a")], time_source=clock)
        pool.report_failure(pool.endpoints[0], LlmHttpError(429, "slow down"), retry_after=45.0)
        self.assertAlmostEqual(pool.snapshot()[0]["circuit_seconds"], 45.0, places=1)
        clock.advance(44.0)
        self.assertIsNone(pool.choose())
        clock.advance(2.0)
        self.assertIsNotNone(pool.choose())

    def test_ewma_learns_and_is_written_back_to_the_endpoint(self):
        """EWMA 存回 `LlmEndpoint` 是为了持久化 —— 端点池越用，预估越准。"""
        pool = LlmPool([endpoint("a")])
        ep = pool.endpoints[0]
        self.assertIsNone(pool.average_latency_ms())
        pool.report_ok(ep, 1.0)
        self.assertAlmostEqual(pool.average_latency_ms(), 1000.0)
        pool.report_ok(ep, 2.0)
        self.assertGreater(pool.average_latency_ms(), 1000.0)
        self.assertLess(pool.average_latency_ms(), 2000.0)
        self.assertAlmostEqual(ep.ewma_ms, pool.average_latency_ms())
        self.assertEqual(ep.ewma_samples, 2)

    def test_ewma_also_remembers_how_many_rows_a_request_covered(self):
        """延迟必须跟批量一起记：`ewma_ms` 是「一次请求」的耗时，10 行一批的 4856 ms 与
        1 行一批的 4856 ms 不是同一个东西，没有行数就没法把历史折算到新批量上。"""
        pool = LlmPool([endpoint("a")])
        ep = pool.endpoints[0]
        self.assertEqual(pool.average_batch(), 0.0, "没观测过就不该假装知道批量")
        pool.report_ok(ep, 4.8, rows=10)
        self.assertAlmostEqual(pool.average_batch(), 10.0)
        self.assertAlmostEqual(ep.ewma_rows, 10.0)
        pool.report_ok(ep, 5.2, rows=20)
        self.assertAlmostEqual(pool.average_batch(), 13.0, places=6)   # 0.7×10 + 0.3×20
        self.assertEqual(pool.snapshot()[0]["ewma_rows"], 13.0)

    def test_the_recorded_batch_survives_a_pool_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_endpoints.json"
            ep = endpoint("a")
            ep.ewma_ms, ep.ewma_rows, ep.ewma_samples = 4856.5, 10.0, 5
            save_pool(path, [ep])
            loaded = load_pool(path)
            self.assertEqual(loaded[0].ewma_ms, 4856.5)
            self.assertEqual(loaded[0].ewma_rows, 10.0)

    def test_a_pool_file_without_the_batch_field_still_loads(self):
        """老端点池文件（这次改动之前写的）没有 `ewma_rows` —— 缺字段当「未知」，不是失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_endpoints.json"
            path.write_text(json.dumps({"version": 1, "endpoints": [
                {"id": "a", "url": "https://a.test/v1", "model": "m", "ewma_ms": 900.0,
                 "ewma_samples": 3}]}), "utf-8")
            loaded = load_pool(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].ewma_rows, 0.0)
            self.assertEqual(loaded[0].ewma_ms, 900.0)

    def test_a_recovered_endpoint_clears_the_last_error(self):
        pool = LlmPool([endpoint("a")])
        ep = pool.endpoints[0]
        pool.report_failure(ep, LlmNetworkError("nope"))
        self.assertIn("nope", pool.snapshot()[0]["last_error"])
        pool.report_ok(ep, 0.5)
        self.assertEqual(pool.snapshot()[0]["last_error"], "")


# =========================================================================== 故障转移


class FailoverTests(unittest.TestCase):
    """4xx 直接抛、429/5xx/超时转移、重试次数与退避。"""

    def setUp(self):
        self.pool = LlmPool([
            endpoint("a", max_retries=1, max_concurrency=1),
            endpoint("b", max_retries=1, max_concurrency=1),
        ])
        self.sleep = Sleeping()

    def client(self, transport, **kwargs):
        kwargs.setdefault("sleep", self.sleep)
        kwargs.setdefault("random_source", lambda: 0.0)
        return LlmClient(self.pool, transport=transport, **kwargs)

    def test_a_400_is_not_failed_over(self):
        """模型名写错是**配置**问题，换端点只会重复失败，还把它藏起来。"""
        transport = ScriptedTransport([(400, "bad model", {})])
        with self.assertRaises(LlmHttpError) as caught:
            self.client(transport).chat("hi", model="nope")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(len(transport.calls), 1, "400 之后还去试了别的端点")

    def test_a_401_is_not_retried_even_on_the_same_endpoint(self):
        transport = ScriptedTransport([(401, "unauthorized", {})])
        with self.assertRaises(LlmHttpError):
            self.client(transport).chat("hi")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.sleep.slept, [], "认证失败没有退避的意义")

    def test_a_429_fails_over_and_honours_retry_after(self):
        """429 是**端点级容量问题**：等一下 + 换个端点都是对的，所以本端点重试一次后转移。"""
        transport = ScriptedTransport([
            (429, "slow down", {"Retry-After": "2"}),
            (429, "slow down", {"Retry-After": "2"}),
            reply("finally"),
        ])
        result = self.client(transport).chat("hi")
        self.assertEqual(result.text, "finally")
        self.assertEqual(len(transport.calls), 3)
        self.assertIn(2.0, self.sleep.slept, "没有等 Retry-After 指定的时间")
        self.assertEqual(transport.calls[0]["url"], transport.calls[1]["url"])
        self.assertNotEqual(transport.calls[1]["url"], transport.calls[2]["url"],
                            "429 用尽重试后还是没有换端点")

    def test_a_500_retries_then_fails_over(self):
        """同一个端点先重试（它可能只是抖了一下），重试用尽再换端点。"""
        transport = ScriptedTransport([
            (500, "server error", {}),
            (500, "server error", {}),
            reply("second endpoint"),
        ])
        result = self.client(transport).chat("hi")
        self.assertEqual(result.text, "second endpoint")
        self.assertEqual(len(transport.calls), 3)
        self.assertGreater(self.sleep.total, 0.0, "5xx 没有退避")
        self.assertEqual(transport.calls[0]["url"], transport.calls[1]["url"])
        self.assertNotEqual(transport.calls[1]["url"], transport.calls[2]["url"])

    def test_a_timeout_fails_over(self):
        transport = ScriptedTransport([
            LlmNetworkError("read timed out"), LlmNetworkError("read timed out"),
            reply("ok from b"),
        ])
        result = self.client(transport).chat("hi")
        self.assertEqual(result.text, "ok from b")

    def test_all_endpoints_failing_raises_the_last_error(self):
        transport = ScriptedTransport([(500, "down", {})])
        with self.assertRaises(LlmHttpError):
            self.client(transport).chat("hi", model="m")
        self.assertEqual(len(transport.calls), 4, "2 个端点 × (1 次初试 + 1 次重试)")

    def test_failover_never_picks_the_same_endpoint_twice(self):
        """端点试遍了就停 —— 再回头重试同一个只会把一次故障拖成三倍时间。"""
        transport = ScriptedTransport([(500, "down", {})])
        client = self.client(transport, max_failover=5)
        with self.assertRaises(LlmHttpError):
            client.chat("hi")
        tried = [call["url"] for call in transport.calls]
        self.assertEqual(len(tried), 4, f"端点被重复选择了：{tried}")
        self.assertEqual(len(set(tried)), 2, "两个端点都应该被试到")

    def test_an_all_down_pool_fails_fast_instead_of_retrying_three_times(self):
        """池级「全都不可用」不是端点级故障：换端点换的是同一批，重试三遍只是把
        一次故障拖成三分钟。`slot()` 已经等过 slot_wait 了。"""
        pool = LlmPool([endpoint("a", enabled=False)])
        transport = ScriptedTransport([reply("never")])
        client = LlmClient(pool, transport=transport, slot_wait=0.05)
        with self.assertRaises(LlmUnavailable):
            client.chat("hi")
        self.assertEqual(transport.calls, [], "没有可用端点却发了请求")

    def test_a_non_json_body_is_treated_as_an_endpoint_failure(self):
        transport = ScriptedTransport([(200, "<html>gateway</html>", {}),
                                       (200, "not json either", {}), reply("ok")])
        result = self.client(transport).chat("hi", model="m")
        self.assertEqual(result.text, "ok")

    def test_the_reply_carries_which_endpoint_served_it(self):
        transport = ScriptedTransport([reply("hello")])
        result = self.client(transport).chat("hi")
        self.assertIn(result.endpoint_id, {"a", "b"})
        self.assertFalse(result.cached)

    def test_connect_and_read_timeouts_are_passed_separately(self):
        """只给一个超时值会让「连不上」和「连上了但很慢」共用一个数字 ——
        前者该短（换端点），后者该长（等它算完）。"""
        transport = ScriptedTransport([reply("ok")])
        self.client(transport).chat("hi")
        self.assertEqual(transport.calls[0]["timeout"], (5.0, 60.0))

    def test_the_endpoint_proxy_is_used(self):
        pool = LlmPool([endpoint("a", proxy="http://127.0.0.1:7890")])
        transport = ScriptedTransport([reply("ok")])
        LlmClient(pool, transport=transport).chat("hi")
        self.assertEqual(transport.calls[0]["proxy"], "http://127.0.0.1:7890")

    def test_the_key_is_sent_as_a_bearer_header_and_never_in_the_body(self):
        pool = LlmPool([endpoint("a", api_key=SECRET)])
        transport = ScriptedTransport([reply("ok")])
        LlmClient(pool, transport=transport).chat("hi")
        call = transport.calls[0]
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, json.dumps(call["payload"], ensure_ascii=False))

    def test_extra_body_and_headers_reach_the_request(self):
        pool = LlmPool([endpoint("a", extra_body={"seed": 7},
                                 extra_headers={"X-Org": "team"})])
        transport = ScriptedTransport([reply("ok")])
        LlmClient(pool, transport=transport).chat("hi", extra_body={"top_k": 5})
        call = transport.calls[0]
        self.assertEqual(call["payload"]["seed"], 7)
        self.assertEqual(call["payload"]["top_k"], 5)
        self.assertEqual(call["headers"]["X-Org"], "team")

    def test_the_temperature_precedence_is_call_then_endpoint(self):
        pool = LlmPool([endpoint("a", temperature=0.9)])
        transport = ScriptedTransport([reply("ok")])
        client = LlmClient(pool, transport=transport)
        client.chat("hi", temperature=0.1)
        self.assertEqual(transport.calls[0]["payload"]["temperature"], 0.1)
        client.chat("hi2")
        self.assertEqual(transport.calls[1]["payload"]["temperature"], 0.9)

    def test_attempts_are_reported_to_the_callback(self):
        seen = []
        transport = ScriptedTransport([(500, "down", {}), reply("ok")])
        client = self.client(transport, on_attempt=seen.append)
        client.chat("hi")
        self.assertEqual(seen[0]["outcome"], "http")
        self.assertEqual(seen[0]["detail"], "500")
        self.assertEqual(seen[-1]["outcome"], "ok")

    def test_a_failing_metrics_callback_does_not_break_generation(self):
        def explode(payload):
            raise RuntimeError("SSE 断了")

        pool = LlmPool([endpoint("a")], on_metrics=explode)
        transport = ScriptedTransport([reply("ok")])
        self.assertEqual(LlmClient(pool, transport=transport).chat("hi").text, "ok")


# =========================================================================== 预算与护栏


class BudgetTests(unittest.TestCase):
    def test_exhaustion_is_deterministic_not_a_hang(self):
        """硬上限的意义是「停止发请求并如实收尾」。"""
        budget = CallBudget(max_calls=2)
        pool = LlmPool([endpoint("a")])
        transport = ScriptedTransport([reply("1"), reply("2")])
        client = LlmClient(pool, transport=transport, budget=budget)
        client.chat("a")
        client.chat("b")
        with self.assertRaises(LlmBudgetExhausted) as caught:
            client.chat("c")
        self.assertIn("上限", str(caught.exception))
        self.assertEqual(len(transport.calls), 2, "超限后还发了请求")
        self.assertTrue(budget.snapshot()["exhausted"])
        self.assertEqual(budget.remaining, 0)

    def test_a_zero_max_calls_means_no_limit(self):
        budget = CallBudget(max_calls=0)
        self.assertEqual(budget.remaining, -1)
        budget.note(ok=True)
        self.assertFalse(budget.exhausted)

    def test_an_early_failure_does_not_abort_the_task(self):
        """没有 `min_calls_to_abort` 这道门槛，第 1 次网络抖动就是 100% 错误率。"""
        budget = CallBudget(max_calls=1000, abort_error_rate=0.5, min_calls_to_abort=20)
        budget.note(ok=False)
        self.assertFalse(budget.aborted)
        self.assertEqual(budget.error_rate(), 1.0)
        budget.check()

    def test_a_high_error_rate_aborts(self):
        """一个「成功」但全是回退值的任务比诚实失败更糟。"""
        budget = CallBudget(max_calls=1000, abort_error_rate=0.5, min_calls_to_abort=20)
        for _ in range(12):
            budget.note(ok=False)
            budget.note(ok=True)
        budget.note(ok=False)                      # 13/25 = 0.52，越过阈值
        self.assertEqual(budget.calls, 25)
        self.assertGreater(budget.error_rate(), 0.5)
        self.assertTrue(budget.aborted)
        with self.assertRaises(LlmAbortError):
            budget.check()

    def test_a_rate_exactly_at_the_threshold_does_not_abort(self):
        """阈值是「超过就中止」，不是「达到就中止」—— 边界值上的行为要明确。"""
        budget = CallBudget(max_calls=1000, abort_error_rate=0.5, min_calls_to_abort=20)
        for _ in range(10):
            budget.note(ok=False)
            budget.note(ok=True)
        self.assertEqual(budget.error_rate(), 0.5)
        self.assertFalse(budget.aborted)

    def test_a_healthy_task_never_aborts(self):
        budget = CallBudget(max_calls=1000, abort_error_rate=0.5, min_calls_to_abort=20)
        for index in range(100):
            budget.note(ok=index % 10 != 0)          # 一成失败
        self.assertFalse(budget.aborted)
        self.assertFalse(budget.exhausted)

    def test_the_budget_counts_are_thread_safe(self):
        budget = CallBudget(max_calls=100000)

        def hammer():
            for _ in range(500):
                budget.note(ok=True)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(budget.calls, 2000)


# =========================================================================== 缓存


class CacheTests(unittest.TestCase):
    def test_the_key_covers_everything_that_changes_the_answer(self):
        base = LlmCache.key("m", 0.3, "sys", "prompt")
        self.assertEqual(base, LlmCache.key("m", 0.3, "sys", "prompt"))
        for other in (LlmCache.key("m2", 0.3, "sys", "prompt"),
                      LlmCache.key("m", 0.4, "sys", "prompt"),
                      LlmCache.key("m", 0.3, "sys2", "prompt"),
                      LlmCache.key("m", 0.3, "sys", "prompt2"),
                      # 额度不同 -> 结果可能是截断的，不能共用缓存条目
                      LlmCache.key("m", 0.3, "sys", "prompt", 100)):
            self.assertNotEqual(base, other)

    def test_a_hit_skips_the_request_entirely(self):
        cache = LlmCache()
        pool = LlmPool([endpoint("a")])
        transport = ScriptedTransport([reply("first")])
        client = LlmClient(pool, transport=transport, cache=cache)
        first = client.chat("同一个 prompt")
        second = client.chat("同一个 prompt")
        self.assertEqual(first.text, "first")
        self.assertEqual(second.text, "first")
        self.assertTrue(second.cached)
        self.assertEqual(second.endpoint_id, "cache")
        self.assertEqual(len(transport.calls), 1, "缓存命中还是发了请求")
        self.assertEqual(cache.hits, 1)
        self.assertEqual(cache.misses, 1)

    def test_a_cached_call_does_not_count_against_the_budget(self):
        """命中不发请求，就不该占调用次数 —— 否则预算是按「重跑一遍」算的。"""
        cache = LlmCache()
        pool = LlmPool([endpoint("a")])
        transport = ScriptedTransport([reply("x")])
        budget = CallBudget(max_calls=1)
        client = LlmClient(pool, transport=transport, cache=cache, budget=budget)
        client.chat("p")
        client.chat("p")
        self.assertEqual(budget.calls, 1)

    def test_a_failed_call_is_not_cached(self):
        cache = LlmCache()
        pool = LlmPool([endpoint("a")])
        transport = ScriptedTransport([(400, "bad", {}), reply("second try")])
        client = LlmClient(pool, transport=transport, cache=cache)
        with self.assertRaises(LlmHttpError):
            client.chat("p")
        self.assertEqual(len(cache), 0, "失败的结果被缓存了，续跑会拿到空值")

    def test_the_cache_is_replayed_from_disk_after_a_crash(self):
        """没有持久缓存，续跑会把崩溃窗口内已经付过钱的大模型调用重来一遍。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_cache.jsonl"
            first = LlmCache(path)
            first.put("k1", "v1")
            first.put("k2", "v2")
            first.put("k1", "v1-corrected")          # 后写的胜出
            restored = LlmCache(path)
            self.assertEqual(restored.restore(), 3)
            self.assertEqual(restored.get("k1"), "v1-corrected")
            self.assertEqual(restored.get("k2"), "v2")

    def test_a_half_written_last_line_is_ignored(self):
        """崩溃时最后一行可能只写了一半。丢它是安全的：那一次请求本来也没算完。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_cache.jsonl"
            cache = LlmCache(path)
            cache.put("k1", "v1")
            with open(path, "ab") as handle:
                handle.write(b'{"key": "k2", "valu')
            restored = LlmCache(path)
            self.assertEqual(restored.restore(), 1)
            self.assertEqual(restored.get("k1"), "v1")

    def test_the_lru_evicts_the_oldest_and_keeps_writing_to_disk(self):
        cache = LlmCache(limit=2)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.get("a")                                # a 变成最近使用
        cache.put("c", "3")
        self.assertEqual(len(cache), 2)
        self.assertIsNone(cache.get("b"), "LRU 淘汰错了对象")
        self.assertEqual(cache.get("a"), "1")
        self.assertEqual(cache.get("c"), "3")


# =========================================================================== 端点池文件


class PoolFileTests(unittest.TestCase):
    def test_round_trip_keeps_the_key_and_locks_the_file_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean_data" / "llm_endpoints.json"
            save_pool(path, [endpoint("a", api_key=SECRET, name="主力", max_concurrency=8)])
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"端点池文件权限是 {oct(mode)}，密钥可被他人读取")
            loaded = load_pool(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].api_key, SECRET)
            self.assertEqual(loaded[0].max_concurrency, 8)
            self.assertEqual(loaded[0].name, "主力")

    def test_a_corrupt_pool_file_is_an_empty_pool_not_a_crash(self):
        """打不开工具页比丢一份端点配置糟得多。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_endpoints.json"
            path.write_text("{ this is not json", "utf-8")
            self.assertEqual(load_pool(path), [])

    def test_a_missing_pool_file_is_an_empty_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_pool(Path(tmp) / "nope.json"), [])

    def test_one_broken_entry_does_not_lose_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_endpoints.json"
            path.write_text(json.dumps({"endpoints": [
                {"id": "a", "url": "https://a/v1", "model": "m"},
                {"id": "b", "url": "", "model": "m"},        # url 空 -> 非法
                {"id": "c", "url": "https://c/v1", "model": "m"},
            ]}), "utf-8")
            self.assertEqual([item.id for item in load_pool(path)], ["a", "c"])

    def test_the_sentinel_keeps_the_existing_key(self):
        """前端拿到的是哨兵，回传时不该把密钥抹掉 —— 这是最容易被写成「保存后密钥没了」。"""
        existing = endpoint("a", api_key=SECRET)
        merged = merge_endpoint(existing, {"name": "改了名字", "api_key": SENTINEL})
        self.assertEqual(merged.api_key, SECRET)
        self.assertEqual(merged.name, "改了名字")

    def test_an_absent_key_field_keeps_it_and_an_empty_one_clears_it(self):
        existing = endpoint("a", api_key=SECRET)
        self.assertEqual(merge_endpoint(existing, {"weight": 3}).api_key, SECRET)
        self.assertEqual(merge_endpoint(existing, {"api_key": ""}).api_key, "")

    def test_a_new_key_overwrites(self):
        existing = endpoint("a", api_key=SECRET)
        merged = merge_endpoint(existing, {"api_key": "sk-new"})
        self.assertEqual(merged.api_key, "sk-new")

    def test_a_new_endpoint_can_carry_a_key(self):
        fresh = merge_endpoint(None, {"id": "a", "url": "https://a/v1", "model": "m",
                                      "api_key": SECRET})
        self.assertEqual(fresh.api_key, SECRET)
        self.assertEqual(fresh.id, "a")

    def test_a_new_endpoint_ignores_the_sentinel(self):
        """新建时哨兵没有「原值」可保留，存下来只会是一个假密钥。"""
        fresh = merge_endpoint(None, {"id": "a", "url": "https://a/v1", "model": "m",
                                      "api_key": SENTINEL})
        self.assertEqual(fresh.api_key, "")


class RedactionTests(unittest.TestCase):
    def test_the_public_shape_never_contains_the_key(self):
        item = endpoint("a", api_key=SECRET)
        blob = json.dumps(item.public(), ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertEqual(item.public()["api_key"], SENTINEL)
        self.assertTrue(item.public()["key_set"])

    def test_an_endpoint_without_a_key_reports_so(self):
        item = endpoint("a")
        self.assertEqual(item.public()["api_key"], "")
        self.assertFalse(item.public()["key_set"])

    def test_the_stored_shape_keeps_the_key(self):
        """端点池文件是密钥**唯一**该待的地方，所以这条要反着断言。"""
        self.assertEqual(endpoint("a", api_key=SECRET).stored()["api_key"], SECRET)

    def test_the_url_is_normalized_the_three_ways_users_write_it(self):
        for written in ("https://x/v1", "https://x/v1/", "https://x/v1/chat/completions"):
            item = LlmEndpoint(id="a", url=written, model="m")
            self.assertEqual(item.chat_url, "https://x/v1/chat/completions")

    def test_a_blank_address_or_model_is_rejected_at_construction(self):
        """空地址/空模型在运行期就是「每行都失败一次」的端点，所以在构造时就拒绝。"""
        for bad in ({"url": "   "}, {"url": ""}, {"model": ""}, {"id": " "}):
            fields = {"id": "a", "url": "https://a/v1", "model": "m", **bad}
            with self.assertRaises(ValidationError, msg=f"{bad} 应当被拒绝"):
                LlmEndpoint(**fields)

    def test_surrounding_whitespace_in_the_address_is_trimmed(self):
        """从别处复制粘贴地址几乎总会带上空格，那不该变成 404。"""
        self.assertEqual(LlmEndpoint(id="a", url=" https://x/v1 ", model="m").chat_url,
                         "https://x/v1/chat/completions")


# =========================================================================== 响应解析


class ParseTests(unittest.TestCase):
    def test_content_shapes(self):
        self.assertEqual(_extract_content(json.dumps(
            {"choices": [{"message": {"content": "标准"}}]})), "标准")
        self.assertEqual(_extract_content(json.dumps(
            {"choices": [{"text": "补全模型"}]})), "补全模型")
        self.assertEqual(_extract_content(json.dumps(
            {"choices": [{"message": {"content": [
                {"type": "text", "text": "分"}, {"type": "text", "text": "段"}]}}]})), "分段")

    def test_unusable_bodies_return_none(self):
        for body in ("", "not json", "{}", '{"choices": []}', '{"choices": [{}]}',
                     '{"choices": [{"message": {}}]}', "[]"):
            self.assertIsNone(_extract_content(body), f"{body!r} 应当解析不出内容")

    def test_retry_after_forms(self):
        self.assertEqual(_retry_after({"Retry-After": "3"}), 3.0)
        self.assertEqual(_retry_after({"retry-after": "3"}), 3.0)
        self.assertEqual(_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 1.0)
        self.assertEqual(_retry_after({}), 0.0)
        self.assertEqual(_retry_after(None), 0.0)

    def test_json_array_survives_chatter_around_it(self):
        """模型经常在数组前后各写一句话，这不是「格式错误」，是常态。"""
        self.assertEqual(_json_array('```json\n[{"a": 1}]\n```'), [{"a": 1}])
        self.assertEqual(_json_array('好的，结果如下：[{"a": 1}, {"a": 2}] 以上。'),
                         [{"a": 1}, {"a": 2}])
        self.assertIsNone(_json_array('{"a": 1}'), "对象不是数组")
        self.assertIsNone(_json_array("完全不是 JSON"))


# =========================================================================== 生成器


def llm_config(**overrides) -> CleanTaskConfig:
    fields = overrides.pop("fields", None) or [
        FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}")),
    ]
    options = overrides.pop("llm", None) or LlmTaskOptions(endpoint_ids=["a"])
    return CleanTaskConfig(
        source=SourceSpec(mode="upload", upload_id="u1"),
        fields=fields,
        llm=options,
        **overrides,
    )


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.pool = LlmPool([endpoint("a")])
        self.transport = ScriptedTransport([reply("默认回答")])
        self.client = LlmClient(self.pool, transport=self.transport)
        self.notes: list[tuple[str, str]] = []

    def generator(self, spec=None, rule=None, **kwargs):
        spec = spec or llm_config().fields[0]
        rule = rule or spec.generate
        kwargs.setdefault("client", self.client)
        kwargs.setdefault("on_note", lambda level, text: self.notes.append((level, text)))
        return LlmGenerator(spec, rule, seed=7, **kwargs)

    def batch(self, indices=(0, 1), rows=None, attempt=0, feedback=None):
        rows = rows if rows is not None else [{"正文": f"第{i}条"} for i in indices]
        return GenerationBatch(indices=list(indices), rows=list(rows),
                               attempt=attempt, feedback=feedback)

    def test_the_prompt_is_rendered_from_the_row(self):
        self.generator().generate(self.batch([0], [{"正文": "今天下雨了"}]))
        sent = self.transport.calls[0]["payload"]["messages"]
        self.assertIn("今天下雨了", sent[-1]["content"])
        self.assertNotIn("${", sent[-1]["content"], "模板没渲染就发出去了")

    def test_plain_text_output_is_the_field_value(self):
        result = self.generator().generate(self.batch([0]))
        self.assertEqual(result, [{"摘要": "默认回答"}])

    def test_wrapping_quotes_and_fences_are_stripped(self):
        for raw, expected in (("「带引号」", "带引号"), ('"双引号"', "双引号"),
                              ("```\n代码围栏\n```", "代码围栏")):
            transport = ScriptedTransport([reply(raw)])
            generator = self.generator(client=LlmClient(self.pool, transport=transport))
            self.assertEqual(generator.generate(self.batch([0])), [{"摘要": expected}],
                             f"{raw!r} 没有被清理")

    def test_a_json_object_output_is_unpacked(self):
        transport = ScriptedTransport([reply('{"摘要": "对象里的值"}')])
        generator = self.generator(client=LlmClient(self.pool, transport=transport))
        self.assertEqual(generator.generate(self.batch([0])), [{"摘要": "对象里的值"}])

    def test_a_request_failure_yields_none_not_a_crash(self):
        """单行失败不该让整块数据失败 —— 交给约束与回退去处理，并记 `llm_error`。"""
        transport = ScriptedTransport([(500, "down", {})])
        generator = self.generator(client=LlmClient(self.pool, transport=transport))
        self.assertEqual(generator.generate(self.batch([0])), [{"摘要": None}])
        self.assertTrue(any("生成失败" in text for _, text in self.notes))

    def test_a_single_field_takes_the_raw_text_as_the_value(self):
        """单字段、非批量时，整段输出就是字段值 —— prompt 里要 JSON 就得自己写清楚。"""
        rule = GenerateRule(kind="llm", prompt="${正文}")
        transport = ScriptedTransport([reply('["一"]')])
        generator = self.generator(rule=rule,
                                   client=LlmClient(self.pool, transport=transport))
        self.assertEqual(generator.generate(self.batch([0])), [{"摘要": '["一"]'}])

    def test_three_rows_in_one_batch_are_three_values_from_one_request(self):
        """批量生成是 GB 级能跑得动的关键：system 提示只发一遍，延迟也摊薄。"""
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=3)
        transport = ScriptedTransport([reply('["一", "二", "三"]')])
        generator = self.generator(rule=rule,
                                   client=LlmClient(self.pool, transport=transport))
        result = generator.generate(self.batch([0, 1, 2]))
        self.assertEqual(result, [{"摘要": "一"}, {"摘要": "二"}, {"摘要": "三"}])
        self.assertEqual(len(transport.calls), 1, "三行没有合并成一次请求")
        prompt = transport.calls[0]["payload"]["messages"][-1]["content"]
        self.assertIn("3", prompt)
        self.assertIn("JSON 数组", prompt)

    def test_a_batch_that_ignores_the_array_format_falls_back_to_row_by_row(self):
        """整批作废会让三行一起失败；退回逐行只多花几次请求，还能救回两行。"""
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=3)
        transport = ScriptedTransport([
            reply("我不太明白你的意思"),          # 批量响应不合法
            reply("一"), reply("二"), reply("三"),
        ])
        generator = self.generator(rule=rule,
                                   client=LlmClient(self.pool, transport=transport))
        result = generator.generate(self.batch([0, 1, 2]))
        self.assertEqual([item["摘要"] for item in result], ["一", "二", "三"])
        self.assertEqual(len(transport.calls), 4)
        self.assertTrue(any("退回逐行" in text for _, text in self.notes))

    def test_a_batch_with_the_wrong_element_count_is_not_trusted(self):
        """少一个元素的数组意味着行与值的对应关系已经错了，不能猜。"""
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=3)
        transport = ScriptedTransport([reply('["一", "二"]'), reply("一"), reply("二"), reply("三")])
        generator = self.generator(rule=rule,
                                   client=LlmClient(self.pool, transport=transport))
        self.assertEqual(len(generator.generate(self.batch([0, 1, 2]))), 3)
        self.assertEqual(len(transport.calls), 4)

    def test_a_leftover_partial_batch_is_handled(self):
        """3 行按 2 行一批 = 两个窗口（第二个只剩 1 行），都要有值。

        答案按内容给而不是按调用顺序给：两个窗口是**并发**发的，脚本式回放会错位。
        """
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=2)
        transport = ParallelTransport(lambda rows: json.dumps([f"值{row}" for row in rows]))
        generator = self.generator(rule=rule,
                                   client=LlmClient(self.pool, transport=transport))
        result = generator.generate(self.batch([0, 1, 2]))
        self.assertEqual([item["摘要"] for item in result], ["值0", "值1", "值2"])
        self.assertEqual(len(transport.calls), 2)

    def test_several_output_fields_share_one_request(self):
        rule = GenerateRule(kind="llm", prompt="${正文}", output_fields=["摘要", "标签"])
        spec = FieldSpec(dest="摘要", generate=rule)
        transport = ScriptedTransport([reply('{"摘要": "短摘要", "标签": "天气"}')])
        generator = self.generator(spec=spec, rule=rule,
                                   client=LlmClient(self.pool, transport=transport))
        self.assertEqual(generator.generate(self.batch([0])),
                         [{"摘要": "短摘要", "标签": "天气"}])
        self.assertEqual(generator.output_fields, ("摘要", "标签"))

    def test_batch_with_multiple_output_fields_is_rejected_up_front(self):
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=5,
                            output_fields=["摘要", "标签"])
        spec = FieldSpec(dest="摘要", generate=rule)
        with self.assertRaises(Exception) as caught:
            self.generator(spec=spec, rule=rule).validate(["正文"])
        self.assertIn("批量", str(caught.exception))

    def test_the_feedback_is_appended_for_a_retry(self):
        """盲重采样比「告诉它哪里不合格」差得多。"""
        generator = self.generator()
        generator.generate(self.batch([0], attempt=1, feedback=["长度超了"]))
        content = self.transport.calls[0]["payload"]["messages"][-1]["content"]
        self.assertIn("长度超了", content)

    def test_an_unknown_field_in_the_prompt_is_rejected(self):
        rule = GenerateRule(kind="llm", prompt="${不存在的字段}")
        spec = FieldSpec(dest="摘要", generate=rule)
        with self.assertRaises(Exception) as caught:
            self.generator(spec=spec, rule=rule).validate(["正文"])
        self.assertIn("不存在的字段", str(caught.exception))

    def test_a_reference_with_a_default_is_accepted(self):
        """`${可选列:-未知}` 是合法的：它声明了一个可缺的列。"""
        rule = GenerateRule(kind="llm", prompt="${可选列:-未知}")
        spec = FieldSpec(dest="摘要", generate=rule)
        self.generator(spec=spec, rule=rule).validate(["正文"])

    def test_the_rule_temperature_overrides_the_task_which_overrides_the_endpoint(self):
        pool = LlmPool([endpoint("a", temperature=0.9)])
        transport = ScriptedTransport([reply("x")])
        client = LlmClient(pool, transport=transport)
        rule = GenerateRule(kind="llm", prompt="${正文}", temperature=0.1)
        spec = FieldSpec(dest="摘要", generate=rule)
        self.generator(spec=spec, rule=rule, client=client,
                       task=LlmTaskOptions(endpoint_ids=["a"], temperature=0.5)) \
            .generate(self.batch([0]))
        self.assertEqual(transport.calls[0]["payload"]["temperature"], 0.1)

    def test_the_task_temperature_is_used_when_the_rule_has_none(self):
        pool = LlmPool([endpoint("a", temperature=0.9)])
        transport = ScriptedTransport([reply("x")])
        generator = self.generator(client=LlmClient(pool, transport=transport),
                                   task=LlmTaskOptions(endpoint_ids=["a"], temperature=0.5))
        generator.generate(self.batch([0]))
        self.assertEqual(transport.calls[0]["payload"]["temperature"], 0.5)

    def test_describe_is_safe_for_the_report(self):
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=4)
        spec = FieldSpec(dest="摘要", generate=rule)
        text = self.generator(spec=spec, rule=rule).describe()
        self.assertIn("大模型", text)
        self.assertIn("4", text)
        self.assertNotIn(SECRET, text)

    def test_the_factory_plugs_into_the_generator_builder(self):
        factory = build_llm_factory(self.client, LlmTaskOptions(endpoint_ids=["a"]))
        generator = factory(llm_config().fields[0], llm_config().fields[0].generate, 3)
        self.assertIsInstance(generator, LlmGenerator)
        self.assertEqual(generator.field_name, "摘要")

    def test_a_cached_generator_does_not_call_the_transport_twice(self):
        cache = LlmCache()
        client = LlmClient(self.pool, transport=self.transport, cache=cache)
        generator = self.generator(client=client)
        first = generator.generate(self.batch([0], [{"正文": "相同的输入"}]))
        second = generator.generate(self.batch([1], [{"正文": "相同的输入"}]))
        self.assertEqual(first, second)
        self.assertEqual(len(self.transport.calls), 1)


# =========================================================================== 一批之内的并发


class ConcurrencyTests(unittest.TestCase):
    """`max_concurrency` 得真的是并发。

    之前引擎是严格串行的：端点池写着「最多同时 4 个请求」，实际一个一个发 —— 50 行 5 次调用
    跑了 23.6 秒。这些用例的意义就是让「并发 4」从面板上的一句话变成可验证的事实。
    """

    def setUp(self):
        self.notes: list[tuple[str, str]] = []

    def client(self, transport, *, concurrency: int = 4, budget: CallBudget | None = None):
        # slot_wait 收短：池子真出问题时立刻失败，别把测试挂满一分钟
        return LlmClient(LlmPool([endpoint("a", concurrency=concurrency)]),
                         transport=transport, budget=budget, slot_wait=1.0)

    def generator(self, client, *, batch_size: int = 1, **kwargs):
        rule = GenerateRule(kind="llm", prompt="${正文}", batch_size=batch_size)
        spec = FieldSpec(dest="摘要", generate=rule)
        kwargs.setdefault("on_note", lambda level, text: self.notes.append((level, text)))
        return LlmGenerator(spec, rule, seed=7, client=client, **kwargs)

    def batch(self, count: int) -> GenerationBatch:
        indices = list(range(count))
        return GenerationBatch(indices=indices,
                               rows=[{"正文": f"第{i}条"} for i in indices], attempt=0)

    def test_a_single_row_field_actually_runs_in_parallel(self):
        """batch_size=1 的字段（调用次数最多的那类）必须真的并行。

        串行实现下 `max_in_flight` 会是 1 —— 这一条明确失败，而不是挂住。
        """
        transport = ParallelTransport(lambda rows: f"值{rows[0]}", expected=4)
        budget = CallBudget(max_calls=100)
        result = self.generator(self.client(transport, budget=budget)).generate(self.batch(8))
        self.assertEqual([item["摘要"] for item in result], [f"值{i}" for i in range(8)])
        self.assertEqual(transport.max_in_flight, 4, "并发没生效：请求还是一个一个发的")
        self.assertEqual(len(transport.calls), 8)
        self.assertEqual(budget.calls, 8)

    def test_batch_windows_are_dispatched_in_parallel_too(self):
        transport = ParallelTransport(lambda rows: json.dumps([f"值{row}" for row in rows]),
                                      expected=2)
        result = self.generator(self.client(transport, concurrency=2),
                                batch_size=2).generate(self.batch(8))
        self.assertEqual([item["摘要"] for item in result], [f"值{i}" for i in range(8)])
        self.assertEqual(transport.max_in_flight, 2, "批量窗口是串行发的")
        self.assertEqual(len(transport.calls), 4, "8 行按 2 行一批该是 4 次请求")

    def test_the_result_follows_the_row_not_the_completion_order(self):
        """第 0 行故意最慢：它最后一个回来，但必须留在结果列表的第 0 位。"""
        transport = ParallelTransport(lambda rows: f"值{rows[0]}",
                                      slow=lambda rows: 0.4 if rows[0] == 0 else 0.0)
        result = self.generator(self.client(transport)).generate(self.batch(4))
        self.assertEqual(transport.finished[-1], 0, "第 0 行没有最后完成，这条用例没测到乱序")
        self.assertEqual([item["摘要"] for item in result], [f"值{i}" for i in range(4)])

    def test_the_width_never_exceeds_the_pool(self):
        """宽度来自 client（池子现在可用的名额），不能自己想开多少开多少。"""
        transport = ParallelTransport(lambda rows: f"值{rows[0]}", expected=2)
        result = self.generator(self.client(transport, concurrency=2)).generate(self.batch(6))
        self.assertEqual(transport.max_in_flight, 2)
        self.assertEqual(len(transport.calls), 6)
        self.assertEqual(len(result), 6)

    def test_one_slot_stays_serial_and_gives_the_same_answers(self):
        """单并发端点是退化情形：仍走纯串行那条路，结果必须与并发时一模一样。"""
        serial = ParallelTransport(lambda rows: f"值{rows[0]}", expected=1)
        parallel = ParallelTransport(lambda rows: f"值{rows[0]}", expected=3)
        one = self.generator(self.client(serial, concurrency=1)).generate(self.batch(6))
        many = self.generator(self.client(parallel, concurrency=3)).generate(self.batch(6))
        self.assertEqual(serial.max_in_flight, 1)
        self.assertEqual(parallel.max_in_flight, 3)
        self.assertEqual(one, many)

    def test_a_budget_that_runs_out_mid_flight_does_not_crash(self):
        """预算耗尽必须是「确定性收尾」，不是「抛出来」也不是「继续发」。

        上限本身是**检查-记账**而不是「预留名额」：`check()` 通过之后才发请求，所以并发下最多会
        多发出「宽度−1」次。这条断言的是这个真实上界（12 行、宽度 2 → 最多 4 次），而不是「恰好
        3 次」—— 后者依赖线程调度，会时绿时红。
        """
        budget = CallBudget(max_calls=3)
        transport = ParallelTransport(lambda rows: f"值{rows[0]}")
        result = self.generator(self.client(transport, concurrency=2, budget=budget)) \
            .generate(self.batch(12))
        self.assertEqual(len(result), 12)
        self.assertTrue(budget.exhausted)
        self.assertLessEqual(len(transport.calls), 4, "预算用尽后还在接着发请求")
        self.assertEqual(budget.calls, len(transport.calls), "记账与实际发出的请求对不上")

    def test_row_by_row_fallback_stops_after_two_bad_windows(self):
        """批量响应连续不是 JSON 数组时，只让前两个窗口退回逐行。

        并发下几个窗口会同时坏掉，每个都退回逐行的话，一轮能烧掉「窗口数 × batch_size」次调用，
        而告警是去重的 —— 用户只看见一条，账单看得见。
        """
        transport = ParallelTransport(lambda rows: "我不太明白你的意思")
        result = self.generator(self.client(transport, concurrency=4),
                                batch_size=2).generate(self.batch(8))
        # 4 个窗口 + 前 2 个窗口各 2 行逐行回退；后 2 个窗口直接留空
        self.assertEqual(len(transport.calls), 8, "后两个窗口不该再逐行重试")
        values = [item["摘要"] for item in result]
        self.assertEqual(values.count(None), 4, "超出额度的窗口应当整窗留空")
        self.assertTrue(any("停止逐行重试" in text for _, text in self.notes))

    def test_parsing_success_again_resets_the_fallback_budget(self):
        """限流只针对**连续**失败：中间成功过一次，后面还可以再回退。"""
        transport = ParallelTransport(lambda rows: "不是数组" if rows[0] < 4
                                      else json.dumps([f"值{row}" for row in rows]))
        result = self.generator(self.client(transport, concurrency=2),
                                batch_size=2).generate(self.batch(8))
        self.assertEqual([item["摘要"] for item in result[4:]], ["值4", "值5", "值6", "值7"])


# =========================================================================== 预估


class EstimateTests(unittest.TestCase):
    def test_the_call_count_is_rows_times_fields_divided_by_batch(self):
        config = llm_config(fields=[
            FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}",
                                                         batch_size=5)),
            FieldSpec(dest="标签", generate=GenerateRule(kind="llm", prompt="${正文}")),
        ])
        estimate = estimate_llm(config, rows=1000, concurrency=8)
        self.assertEqual(estimate.calls, 200 + 1000)
        self.assertFalse(estimate.calls_capped)

    def test_more_rows_means_more_time(self):
        config = llm_config()
        small = estimate_llm(config, rows=100, concurrency=8)
        large = estimate_llm(config, rows=10000, concurrency=8)
        self.assertGreater(large.seconds_high, small.seconds_high)

    def test_the_range_is_a_range_not_a_single_number(self):
        """GB 级的单点数字必然是谎话。"""
        estimate = estimate_llm(llm_config(), rows=10000, concurrency=8)
        self.assertLess(estimate.seconds_low, estimate.seconds_high)

    def test_the_sample_limit_is_surfaced(self):
        config = llm_config(llm=LlmTaskOptions(endpoint_ids=["a"], sample_rows=500))
        estimate = estimate_llm(config, rows=1_000_000, concurrency=8)
        self.assertEqual(estimate.calls, 500)
        self.assertTrue(any("500" in note for note in estimate.notes))

    def test_the_hard_cap_is_surfaced(self):
        """截断必须显示出来，否则用户以为「任务跑完了 = 每行都生成了」。"""
        config = llm_config(llm=LlmTaskOptions(endpoint_ids=["a"], max_calls=1000))
        estimate = estimate_llm(config, rows=1_000_000, concurrency=8)
        self.assertEqual(estimate.calls, 1000)
        self.assertTrue(estimate.calls_capped)
        self.assertTrue(any("上限" in note for note in estimate.notes))

    def test_more_concurrency_means_less_time(self):
        config = llm_config()
        slow = estimate_llm(config, rows=10000, concurrency=2)
        fast = estimate_llm(config, rows=10000, concurrency=32)
        self.assertGreater(slow.seconds_high, fast.seconds_high)

    def test_an_empty_pool_is_reported_not_silently_estimated(self):
        estimate = estimate_llm(llm_config(), rows=100, concurrency=0)
        self.assertTrue(any("端点" in note for note in estimate.notes))
        # 0 会被界面显示成「约 0 秒」，而用户接下来要等的是别的东西 —— 给 None 不给 0
        self.assertIsNone(estimate.seconds_low)
        self.assertIsNone(estimate.seconds_high)

    # -- 新模型：实测对得上才叫预估 ----------------------------------------

    def batch(self, size, **over):
        return llm_config(fields=[
            FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}",
                                                         batch_size=size))], **over)

    def learned(self, *, seconds=4.856, rows=10, concurrency=4):
        """一个有历史观测的池：一次请求 rows 行花了 seconds 秒（用户实测的那组数）。"""
        pool = LlmPool([endpoint("a", concurrency=concurrency)])
        pool.report_ok(pool.endpoints[0], seconds, rows=rows)
        return pool

    def test_the_same_batch_size_is_not_discounted_twice(self):
        """这次报错的核心：10 行一批量出来的 4856 ms 就是「一次请求的耗时」，不能再折一次。

        50 行、批量 10、并发 4 → 5 次调用分 2 轮，每轮 4.856 秒 → 约 6.8–19.1 秒。
        用户实测改前 23.6 秒（串行 5 次），改后约 10 秒 —— 区间能罩住实测才有意义，
        旧公式给的「2 秒 – 5 秒」罩不住。
        """
        estimate = estimate_llm(self.batch(10), self.learned(), rows=50)
        self.assertEqual(estimate.calls, 5)
        self.assertEqual(estimate.rounds, 2, "5 次调用按 4 并发是 2 轮")
        self.assertAlmostEqual(estimate.seconds_low, 2 * 4.856 * 0.7, places=3)
        self.assertAlmostEqual(estimate.seconds_high, 2 * (4.856 * 1.8 + 0.8), places=3)

    def test_an_unknown_historical_batch_means_no_discount(self):
        """老端点池文件没有 `ewma_rows`：不知道历史批量时按原值算，而不是瞎折一个系数。"""
        estimate = estimate_llm(self.batch(10), self.learned(rows=0), rows=50)
        self.assertAlmostEqual(estimate.seconds_low, 2 * 4.856 * 0.7, places=3)

    def test_a_bigger_batch_gets_the_batch_discount(self):
        """批量从 10 调到 20：3 次调用 1 轮，单次耗时按批效率折到 0.758 倍。"""
        estimate = estimate_llm(self.batch(20), self.learned(), rows=50)
        self.assertEqual((estimate.calls, estimate.rounds), (3, 1))
        self.assertAlmostEqual(estimate.seconds_low,
                               4.856 * (20 / 10) ** (BATCH_EFFICIENCY - 1) * 0.7, places=3)

    def test_splitting_into_single_rows_is_not_cheaper(self):
        """把批量拆成 1 行只会更慢。旧公式在这里也是错的（会算出「更快」）。"""
        ten = estimate_llm(self.batch(10), self.learned(), rows=50)
        one = estimate_llm(self.batch(1), self.learned(), rows=50)
        self.assertEqual(one.calls, 50)
        self.assertGreater(one.seconds_low, ten.seconds_low,
                           "拆成一行一次居然比批量还快 —— 历史延迟被当成单行耗时了")

    def test_the_chunk_size_caps_how_many_requests_a_round_can_hold(self):
        """引擎按 `commit_rows`（有大模型字段时 2000）切块，一块之内才谈得上并发。

        2 万行、批量 200 → 100 次调用，但只有 10 个块：每块 10 个请求，并发再高也超不过
        「块里有多少请求」，所以轮数是 10 而不是 `100 / 16` 的 7。
        """
        estimate = estimate_llm(self.batch(200), self.learned(concurrency=16), rows=20_000)
        self.assertEqual(estimate.calls, 100)
        self.assertEqual(estimate.rounds, 10)

    def test_fields_are_serial_so_their_rounds_add_up(self):
        """两个字段各 5 次调用、并发 4：是 2 轮 + 2 轮 = 4 轮，不是 10 次调用 ÷ 4 的 3 轮 ——
        第二个字段只能等第一个跑完。"""
        config = llm_config(fields=[
            FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}",
                                                         batch_size=10)),
            FieldSpec(dest="标签", generate=GenerateRule(kind="llm", prompt="${正文}",
                                                         batch_size=10)),
        ])
        estimate = estimate_llm(config, self.learned(), rows=50)
        self.assertEqual(estimate.calls, 10)
        self.assertEqual(estimate.rounds, 4)
        self.assertAlmostEqual(estimate.seconds_low, 4 * 4.856 * 0.7, places=3)

    def test_the_hard_cap_is_handed_out_field_by_field(self):
        """上限按字段顺序分配：第一个字段先花完，第二个字段一次请求都发不出去。"""
        config = llm_config(
            fields=[
                FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}")),
                FieldSpec(dest="标签", generate=GenerateRule(kind="llm", prompt="${正文}")),
            ],
            llm=LlmTaskOptions(endpoint_ids=["a"], max_calls=30),
        )
        estimate = estimate_llm(config, self.learned(), rows=100)
        self.assertEqual(estimate.calls, 30)
        self.assertTrue(estimate.calls_capped)
        # 30 次调用全给第一个字段（并发 4 → 8 轮），第二个字段拿到 0 次、0 轮
        self.assertEqual(estimate.rounds, 8)
        self.assertAlmostEqual(estimate.seconds_low, 8 * 4.856 * 0.7, places=3)

    def test_the_note_says_what_the_number_does_not_cover(self):
        """面板上的秒数是「大模型请求」的时间，不含读文件、写文件、用户函数与约束检查。"""
        estimate = estimate_llm(self.batch(10), self.learned(), rows=50)
        self.assertTrue(any("只估大模型请求的时间" in note for note in estimate.notes),
                        estimate.notes)

    def test_the_pool_latency_and_concurrency_are_used_when_available(self):
        pool = LlmPool([endpoint("a", concurrency=4, weight=2), endpoint("b", concurrency=4)])
        for item in pool.endpoints:
            pool.report_ok(item, 0.5)
        with_pool = estimate_llm(llm_config(), pool, rows=1000)
        self.assertEqual(with_pool.concurrency, 8.0, "并发按 Σ max_concurrency 算，不含权重")
        self.assertAlmostEqual(with_pool.avg_latency_ms, 500.0)
        # 端点池越用越准：有历史延迟时不该再打「按缺省估算」的提示
        self.assertFalse(any("保守估算" in note for note in with_pool.notes))

    def test_the_weight_does_not_change_the_estimated_concurrency(self):
        """权重改变的是「谁来干」，不是「能干多少」—— 墙钟只跟并发与延迟有关。"""
        plain = LlmPool([endpoint("a", concurrency=4), endpoint("b", concurrency=4)])
        weighted = LlmPool([endpoint("a", concurrency=4, weight=9), endpoint("b", concurrency=4)])
        self.assertEqual(estimate_llm(llm_config(), weighted, rows=1000).concurrency,
                         estimate_llm(llm_config(), plain, rows=1000).concurrency)

    def test_a_task_without_llm_fields_reports_no_number(self):
        """没有大模型字段时给 None，不给 0 —— 0 会被界面显示成「约 0 秒」，而用户接下来
        要等的是真实存在的磁盘与函数时间。"""
        config = llm_config(fields=[FieldSpec(dest="标题", source="标题")])
        estimate = estimate_llm(config, rows=1_000_000, concurrency=8)
        self.assertEqual(estimate.calls, 0)
        self.assertIsNone(estimate.seconds_low)
        self.assertIsNone(estimate.seconds_high)
        self.assertIsNone(estimate.hours_low)
        self.assertIsNone(estimate.as_dict()["seconds_high"])
        self.assertTrue(any("没" in note or "给不出" in note for note in estimate.notes))

    def test_the_shape_is_json_ready(self):
        payload = estimate_llm(llm_config(), rows=10, concurrency=4).as_dict()
        for key in ("rows", "calls", "seconds_low", "seconds_high", "notes"):
            self.assertIn(key, payload)
        json.dumps(payload)                       # 不可序列化会在 SSE 上炸

    def test_the_notes_never_contain_a_key(self):
        estimate = estimate_llm(llm_config(), rows=10, concurrency=4)
        self.assertNotIn(SECRET, json.dumps(estimate.as_dict(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
