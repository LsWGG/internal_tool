"""事件通道：扇出、背压、断线重放、日志有界化。

这层的失败模式全是「并发下偶尔错」，而且大部分不会抛异常：
- 从引擎线程往 `asyncio.Queue` 直接 put —— 压力下静默损坏队列（订阅者丢事件）
- 队列无界 —— 一个卡住的标签页把内存吃光
- 序号不全局 —— `Last-Event-ID` 补齐变成歧义
- 告警不去重 —— GB 级任务把日志写成几百万行

所以下面的测试刻意覆盖这四条，而不是只测「emit 之后能读到」。
"""

from __future__ import annotations

import asyncio
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clean_events import (
    KIND_LOG,
    KIND_PROGRESS,
    KIND_RESYNC,
    Event,
    EventBus,
    EventLog,
    LogGate,
    Subscription,
    level_rank,
)


class BusCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def bus(self, **kwargs):
        bus = EventBus(**kwargs)
        self.addCleanup(bus.close)
        return bus


# =========================================================================== 扇出


class FanoutTests(BusCase):
    def test_emits_to_every_subscriber(self):
        bus = self.bus()
        first = bus.subscribe("t1")
        second = bus.subscribe("t1")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        bus.log("t1", "第一行")
        self.assertEqual([e.message for e in first.drain()], ["第一行"])
        self.assertEqual([e.message for e in second.drain()], ["第一行"])

    def test_task_filter_isolates_a_stream(self):
        bus = self.bus()
        only = bus.subscribe("t1")
        self.addCleanup(only.close)
        bus.log("t1", "给 t1")
        bus.log("t2", "给 t2")
        self.assertEqual([e.message for e in only.drain()], ["给 t1"])

    def test_global_stream_still_sees_task_events(self):
        """全局流是任务列表的数据源，它必须收全 —— 但 debug 不收，否则它就成了日志流。"""
        bus = self.bus()
        everything = bus.subscribe("")
        self.addCleanup(everything.close)
        bus.log("t1", "信息")
        bus.log("t2", "调试", level="debug")
        messages = [e.message for e in everything.drain()]
        self.assertEqual(messages, ["信息"])

    def test_level_filter_on_a_task_stream(self):
        bus = self.bus()
        noisy = bus.subscribe("t1", min_level="warn")
        self.addCleanup(noisy.close)
        bus.log("t1", "信息")
        bus.log("t1", "告警", level="warn")
        self.assertEqual([e.message for e in noisy.drain()], ["告警"])

    def test_closed_subscription_stops_receiving(self):
        bus = self.bus()
        sub = bus.subscribe("t1")
        bus.log("t1", "之前")
        sub.close()
        bus.log("t1", "之后")
        self.assertEqual([e.message for e in sub.drain()], ["之前"])
        self.assertEqual(bus.subscriber_count, 0)

    def test_sequence_numbers_are_global_and_monotonic(self):
        bus = self.bus()
        sub = bus.subscribe()
        self.addCleanup(sub.close)
        for index in range(5):
            bus.log("t1" if index % 2 else "t2", str(index))
        seqs = [e.seq for e in sub.drain()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), 5)
        self.assertEqual(bus.last_seq, seqs[-1])

    def test_last_seq_survives_the_ring_wrapping(self):
        bus = self.bus(ring=4)
        for index in range(10):
            bus.log("t", str(index))
        self.assertEqual(bus.last_seq, 10)

    def test_event_serialises_for_sse(self):
        bus = self.bus()
        event = bus.log("t1", "带 | 与\n换行", data={"n": 1})
        frame = event.sse()
        self.assertEqual(frame["id"], str(event.seq))
        self.assertEqual(frame["event"], KIND_LOG)
        self.assertIn("\\n", frame["data"])          # 换行被转义，不会截断 SSE 帧
        self.assertNotIn("\n", frame["data"])


# =========================================================================== 背压


class BackpressureTests(BusCase):
    def test_a_stuck_subscriber_drops_oldest_instead_of_growing(self):
        bus = self.bus()
        sub = bus.subscribe("t", maxsize=5)
        self.addCleanup(sub.close)
        for index in range(50):
            bus.log("t", f"第 {index} 条")
        items = sub.drain()
        kept = [item for item in items if item.kind == KIND_LOG]
        self.assertLessEqual(len(kept), 5, "队列没有上界")
        # 每一条要么还在队列里，要么被计进了 gap —— 一条都不能凭空消失
        self.assertEqual(len(kept) + sub.gap, 50)
        self.assertGreater(sub.dropped_by_backpressure, 40)
        self.assertEqual(kept[-1].message, "第 49 条", "丢最旧应当留下最新的")

    def test_a_stuck_subscriber_does_not_block_the_engine(self):
        """引擎在工作线程里 emit：订阅者是死的也不能让 emit 变慢。"""
        bus = self.bus()
        sub = bus.subscribe("t", maxsize=2)
        self.addCleanup(sub.close)
        started = time.monotonic()
        for index in range(20000):
            bus.log("t", f"第 {index} 条")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, f"20000 条 emit 用了 {elapsed:.2f}s，说明被订阅者拖住了")

    def test_the_drop_is_announced_with_a_resync(self):
        """丢事件必须**可见**：不然前端会以为日志断了是任务卡了。

        不变式是「每一条要么还在队列里，要么被计进 gap」，而不是某个具体的数字。
        """
        bus = self.bus()
        sub = bus.subscribe("t", maxsize=3)
        self.addCleanup(sub.close)
        for index in range(20):
            bus.log("t", f"第 {index} 条")
        items = sub.drain()
        notices = [item for item in items if item.kind == KIND_RESYNC]
        self.assertTrue(notices, "丢了 17 条却一条 resync 都没有")
        self.assertIn("已省略", notices[0].message)
        kept = [item for item in items if item.kind == KIND_LOG]
        self.assertEqual(len(kept) + sub.gap, 20)
        self.assertEqual(notices[0].data["gap"], sub.gap)
        self.assertEqual(sub.gap, 17, "3 格队列留 3 条、丢 17 条")

    def test_the_notice_is_reported_once_and_not_repeated(self):
        """通知是「增量」而不是每次都报累计值，否则前端会重复提示。"""
        bus = self.bus()
        sub = bus.subscribe("t", maxsize=2)
        self.addCleanup(sub.close)
        for index in range(10):
            bus.log("t", f"第 {index} 条")
        self.assertEqual(len(sub.drain()), 3)        # resync + 2 条真事件
        self.assertEqual(sub.drain(), [], "没有新丢失却又报了一次")

    def test_one_stuck_subscriber_does_not_starve_another(self):
        bus = self.bus()
        stuck = bus.subscribe("t", maxsize=2)
        healthy = bus.subscribe("t", maxsize=500)
        self.addCleanup(stuck.close)
        self.addCleanup(healthy.close)
        for index in range(100):
            bus.log("t", f"第 {index} 条")
        self.assertEqual(len(healthy.drain()), 100)


# =========================================================================== 跨线程


class CrossThreadTests(BusCase):
    def test_delivery_from_many_worker_threads(self):
        """引擎线程 emit、事件循环消费 —— 这正是生产里的形状。

        `asyncio.Queue` 不是线程安全的，直接 put 在高并发下会丢事件；这里用真循环 + 真
        线程把那条路跑一遍。
        """

        async def main():
            bus = EventBus()
            sub = bus.subscribe("t")
            received = []

            async def consume():
                while len(received) < 300:
                    received.append(await sub.get())

            reader = asyncio.create_task(consume())
            threads = [
                threading.Thread(target=lambda worker=index: [
                    bus.log("t", f"{worker}-{row}") for row in range(100)
                ])
                for index in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            await asyncio.wait_for(reader, timeout=10)
            sub.close()
            bus.close()
            return received

        received = asyncio.run(main())
        self.assertEqual(len(received), 300)
        self.assertEqual(len({event.message for event in received}), 300)

    def test_subscribe_inside_the_loop_uses_an_async_queue(self):
        async def main():
            bus = EventBus()
            sub = bus.subscribe("t")
            self.assertTrue(sub._asyncio)
            bus.log("t", "来自同一线程")
            event = await asyncio.wait_for(sub.get(), timeout=2)
            sub.close()
            bus.close()
            return event.message

        self.assertEqual(asyncio.run(main()), "来自同一线程")

    def test_subscribe_outside_the_loop_is_synchronous(self):
        bus = self.bus()
        sub = bus.subscribe("t")
        self.addCleanup(sub.close)
        self.assertFalse(sub._asyncio)
        with self.assertRaises(RuntimeError):
            asyncio.run(sub.get())


# =========================================================================== 重放


class ReplayTests(BusCase):
    def test_replay_returns_events_after_an_id(self):
        bus = self.bus()
        bus.log("t", "一")
        bridge = bus.log("t", "二")
        bus.log("t", "三")
        items, gap = bus.replay(bridge.seq - 1)
        self.assertEqual([item.message for item in items], ["二", "三"])
        self.assertEqual(gap, 0)

    def test_replay_from_the_beginning(self):
        bus = self.bus()
        bus.log("t", "一")
        bus.log("t", "二")
        items, gap = bus.replay(0)
        self.assertEqual([item.message for item in items], ["一", "二"])
        self.assertEqual(gap, 0)

    def test_replay_treats_a_wrapped_ring_as_a_gap(self):
        """断线太久、缓冲已经绕过去了：必须报 gap，不能假装「没有新事件」。"""
        bus = self.bus(ring=5)
        for index in range(20):
            bus.log("t", str(index))
        items, gap = bus.replay(3)
        self.assertGreater(gap, 0)
        self.assertEqual(items[0].message, "15")     # 缓冲里最老的那条
        self.assertEqual(len(items), 5)

    def test_replay_can_be_scoped_to_a_task(self):
        bus = self.bus()
        bus.log("t1", "一")
        bus.log("t2", "二")
        bus.log("t1", "三")
        items, _ = bus.replay(0, task_id="t1")
        self.assertEqual([item.message for item in items], ["一", "三"])

    def test_replay_honours_the_limit(self):
        bus = self.bus()
        for index in range(30):
            bus.log("t", str(index))
        items, _ = bus.replay(0, limit=10)
        self.assertEqual(len(items), 10)


# =========================================================================== 进度


class ProgressTests(BusCase):
    def test_the_first_progress_goes_out_immediately(self):
        """节流不能把第一条也压住：点下「开始」后 0.5 秒内看不到任何反应是 bug 级的体验。"""
        bus = self.bus()
        sub = bus.subscribe("t")
        self.addCleanup(sub.close)
        bus.progress("t", {"done": 0}, message="开始")
        items = sub.drain()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].message, "开始")

    def test_throttled_progress_is_merged_not_dropped(self):
        """被节流掉的那几次必须并进下一次 —— 否则任务结束时最后一次进度永远发不出去。"""
        bus = self.bus()
        sub = bus.subscribe("t")
        self.addCleanup(sub.close)
        bus.progress("t", {"done": 1}, message="第一步")
        sub.drain()
        bus.progress("t", {"done": 2}, message="第二步")
        bus.progress("t", {"done": 3}, message="第三步")
        self.assertEqual(sub.drain(), [], "间隔内的第二次进度不该发出去")
        bus.flush_progress("t")
        items = sub.drain()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].data["done"], 3, "第三次的值被丢了")
        self.assertEqual(items[0].message, "第三步")

    def test_force_always_publishes(self):
        bus = self.bus()
        sub = bus.subscribe("t")
        self.addCleanup(sub.close)
        bus.progress("t", {"done": 1})
        bus.progress("t", {"done": 2}, force=True)
        self.assertEqual(len(sub.drain()), 2)

    def test_progress_is_throttled_per_task(self):
        bus = self.bus()
        sub = bus.subscribe()
        self.addCleanup(sub.close)
        bus.progress("a", {"done": 1})
        bus.progress("b", {"done": 1})
        self.assertEqual(len(sub.drain()), 2, "节流是按任务算的，不是全局的")

    def test_a_burst_of_progress_does_not_flood_the_queue(self):
        """进度是高频低价值事件。节流 + 合并之后，2000 次上报只应产出一个事件 ——

        这条同时保护两件事：订阅者队列不被灌满（gap 保持 0），以及引擎不必为每次进度
        都付一次扇出 + 序列化的代价。
        """
        bus = self.bus()
        sub = bus.subscribe("t", maxsize=200)
        self.addCleanup(sub.close)
        for index in range(2000):
            bus.progress("t", {"done": index})
        self.assertEqual(sub.gap, 0, "进度事件把队列灌满并触发了丢最旧")
        self.assertEqual(len(sub.drain()), 1, "节流没生效，进度把队列当成了日志")

    def test_forced_progress_is_expected_to_bypass_the_throttle(self):
        """force 是显式要求每条都发（任务收尾），节流不该拦它 —— 拦了才是 bug。"""
        bus = self.bus()
        sub = bus.subscribe("t", maxsize=200)
        self.addCleanup(sub.close)
        for index in range(50):
            bus.progress("t", {"done": index}, force=True)
        self.assertEqual(len(sub.drain()), 50)

    def test_flush_progress_emits_the_last_state(self):
        bus = self.bus()
        sub = bus.subscribe("t")
        self.addCleanup(sub.close)
        bus.progress("t", {"done": 7})
        sub.drain()
        bus.progress("t", {"done": 9})
        bus.flush_progress("t")
        items = sub.drain()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].data["done"], 9)

    def test_forget_clears_the_throttle_state(self):
        bus = self.bus()
        sub = bus.subscribe("t")
        self.addCleanup(sub.close)
        bus.progress("t", {"done": 1})
        bus.forget("t")
        bus.progress("t", {"done": 2})
        self.assertEqual(len(sub.drain()), 2)


# =========================================================================== 日志


class EventLogTests(BusCase):
    def test_lines_are_batched_then_flushed(self):
        log = EventLog(self.tmp / "events.jsonl", batch=5)
        for index in range(4):
            log.append(Event(seq=index + 1, task_id="t", kind=KIND_LOG, level="info",
                             message=str(index), ts="2026-09-26T00:00:00+00:00"))
        self.assertFalse(log.path.exists(), "没到批量就写盘了")
        log.append(Event(seq=5, task_id="t", kind=KIND_LOG, level="info",
                         message="4", ts="2026-09-26T00:00:00+00:00"))
        self.assertTrue(log.path.exists())
        log.close()
        self.assertEqual(len(log.path.read_text("utf-8").splitlines()), 5)

    def test_flush_writes_the_pending_tail(self):
        log = EventLog(self.tmp / "events.jsonl", batch=1000)
        log.append(Event(seq=1, task_id="t", kind=KIND_LOG, level="info", message="一"))
        log.flush()
        log.close()
        self.assertEqual(len(EventLog.read(log.path)), 1)

    def test_interval_triggers_a_flush(self):
        log = EventLog(self.tmp / "events.jsonl", batch=1000, interval=0.0)
        log.append(Event(seq=1, task_id="t", kind=KIND_LOG, level="info", message="一"))
        self.assertTrue(log.path.exists())
        log.close()

    def test_read_skips_a_torn_last_line(self):
        """崩溃时最后一行可能写了一半。读到它不该让整份日志报废。"""
        path = self.tmp / "events.jsonl"
        log = EventLog(path, batch=1)
        log.append(Event(seq=1, task_id="t", kind=KIND_LOG, level="info", message="一"))
        log.close()
        with open(path, "ab") as handle:
            handle.write(b'{"seq": 2, "task_i')
        self.assertEqual([item["message"] for item in EventLog.read(path)], ["一"])

    def test_read_respects_after_and_limit(self):
        path = self.tmp / "events.jsonl"
        log = EventLog(path, batch=1)
        for index in range(10):
            log.append(Event(seq=index + 1, task_id="t", kind=KIND_LOG, level="info",
                             message=str(index)))
        log.close()
        items = EventLog.read(path, after=5, limit=3)
        self.assertEqual([item["seq"] for item in items], [6, 7, 8])

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(EventLog.read(self.tmp / "nope.jsonl"), [])

    def test_bus_persists_to_the_attached_log(self):
        bus = self.bus()
        log = bus.attach_log("t1", EventLog(self.tmp / "t1" / "events.jsonl", batch=1))
        bus.log("t1", "入库")
        bus.log("t2", "别人的日志")
        self.assertEqual([item["message"] for item in EventLog.read(log.path)], ["入库"])

    def test_flush_log_at_a_commit_point(self):
        bus = self.bus()
        log = bus.attach_log("t1", EventLog(self.tmp / "events.jsonl", batch=10000))
        bus.log("t1", "还没到批量")
        self.assertFalse(log.path.exists())
        bus.flush_log("t1")
        self.assertTrue(log.path.exists())
        bus.detach_log("t1")


# =========================================================================== 告警有界化


class LogGateTests(BusCase):
    def test_first_occurrence_passes(self):
        gate = LogGate()
        self.assertTrue(gate.should_log(("f", "性别", "enum", "keep"), 1))

    def test_repeats_are_suppressed_until_the_next_order_of_magnitude(self):
        gate = LogGate()
        self.assertTrue(gate.should_log(("k",), 1))
        for count in range(2, 10):
            self.assertFalse(gate.should_log(("k",), count), count)
        self.assertTrue(gate.should_log(("k",), 10))
        for count in range(11, 100):
            self.assertFalse(gate.should_log(("k",), count), count)
        self.assertTrue(gate.should_log(("k",), 100))
        self.assertTrue(gate.should_log(("k",), 1000))

    def test_a_jump_over_several_magnitudes_still_logs_once(self):
        """批量累加时可能一次从 3 跳到 5000 —— 那一次必须打，且只打一次。"""
        gate = LogGate()
        gate.should_log(("k",), 1)
        self.assertTrue(gate.should_log(("k",), 5000))
        self.assertFalse(gate.should_log(("k",), 5001))

    def test_groups_are_independent(self):
        gate = LogGate()
        self.assertTrue(gate.should_log(("a",), 1))
        self.assertTrue(gate.should_log(("b",), 1))
        self.assertFalse(gate.should_log(("a",), 2))
        self.assertEqual(gate.groups, 2)

    def test_ten_million_fallbacks_produce_a_handful_of_lines(self):
        """这就是这层存在的理由：GB 级任务上「每次一行」等于把日志写成垃圾。"""
        gate = LogGate()
        lines = sum(
            1 for count in range(1, 1_000_001) if gate.should_log(("k",), count)
        )
        self.assertLessEqual(lines, 7)

    def test_reset_forgets_everything(self):
        gate = LogGate()
        gate.should_log(("k",), 1)
        self.assertFalse(gate.should_log(("k",), 2))
        gate.reset()
        self.assertTrue(gate.should_log(("k",), 2))

    def test_a_stale_count_does_not_reopen_a_group(self):
        """已经打过 1000 之后又来一个 count=5（来自另一个文件的计数器），不该再打。"""
        gate = LogGate()
        gate.should_log(("k",), 1000)
        self.assertFalse(gate.should_log(("k",), 5))


# =========================================================================== 辅助


class HelperTests(BusCase):
    def test_level_rank_orders_correctly(self):
        self.assertLess(level_rank("debug"), level_rank("info"))
        self.assertLess(level_rank("info"), level_rank("warn"))
        self.assertLess(level_rank("warn"), level_rank("error"))

    def test_unknown_level_defaults_to_info(self):
        self.assertEqual(level_rank("nonsense"), level_rank("info"))

    def test_counts_by_kind(self):
        bus = self.bus()
        bus.log("t", "一")
        bus.log("t", "二")
        bus.progress("t", {"done": 1})
        self.assertEqual(bus.counts()[KIND_LOG], 2)

    def test_close_is_idempotent_and_releases_logs(self):
        bus = EventBus()
        log = bus.attach_log("t", EventLog(self.tmp / "events.jsonl", batch=1000))
        bus.log("t", "一")
        bus.close()
        bus.close()
        self.assertTrue(log.path.exists(), "关闭总线的同时应当把日志刷出去")

    def test_context_manager(self):
        with EventBus() as bus:
            bus.log("t", "一")
            self.assertEqual(len(bus.ring), 1)


if __name__ == "__main__":
    unittest.main()
