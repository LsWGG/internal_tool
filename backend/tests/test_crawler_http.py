import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from app.crawler_http import (
    BACKOFF_SECONDS,
    CrawlerHttpClient,
    CrawlerHttpError,
    DomainRateLimiter,
    UA_DESKTOP,
    sanitize_url,
)


def response_for(status, url="https://example.com/a"):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = b"{}"
    response.encoding = "utf-8"
    return response


class FakeSession:
    """按脚本回放：列表里放 Response 就返回它，放异常就抛它。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RetryBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = patch("app.crawler_http.time.sleep", side_effect=self.sleeps.append)
        self.addCleanup(patcher.stop)
        patcher.start()

    def client_for(self, script, **kwargs):
        session = FakeSession(script)
        client = CrawlerHttpClient(**kwargs)
        return client, session

    def test_retryable_status_is_retried_then_succeeds_with_backoff_table(self):
        client, session = self.client_for([response_for(500), response_for(500), response_for(200)])
        result = client.get("https://example.com/a", session=session, rate=False)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(self.sleeps, [BACKOFF_SECONDS[0], BACKOFF_SECONDS[1]])

    def test_retryable_status_exhausted_raises_http_error(self):
        client, session = self.client_for([response_for(429)] * 3)
        with self.assertRaises(CrawlerHttpError) as caught:
            client.get("https://example.com/a", session=session, rate=False)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(session.calls), 3)

    def test_terminal_status_is_not_retried(self):
        client, session = self.client_for([response_for(404)])
        with self.assertRaises(CrawlerHttpError) as caught:
            client.get("https://example.com/a", session=session, rate=False)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(session.calls), 1)

    def test_forbidden_is_terminal_not_retried(self):
        """403 对多数站点是确定性拒绝（反爬判定），重试只会加深刻度。"""
        client, session = self.client_for([response_for(403)])
        with self.assertRaises(CrawlerHttpError):
            client.get("https://example.com/a", session=session, rate=False)
        self.assertEqual(len(session.calls), 1)

    def test_network_error_is_retried_then_reraised(self):
        client, session = self.client_for([requests.Timeout("超时")] * 3)
        with self.assertRaises(requests.Timeout):
            client.get("https://example.com/a", session=session, rate=False)
        self.assertEqual(len(session.calls), 3)

    def test_network_error_recovers_within_budget(self):
        client, session = self.client_for([requests.ConnectionError("断连"), response_for(200)])
        self.assertEqual(client.get("https://example.com/a", session=session, rate=False).status_code, 200)
        self.assertEqual(len(session.calls), 2)

    def test_retries_zero_means_single_attempt(self):
        """搜狗索引这类调用方自带重试语义，HTTP 层不能再叠加。"""
        client, session = self.client_for([response_for(500)])
        with self.assertRaises(CrawlerHttpError):
            client.get("https://example.com/a", session=session, retries=0, rate=False)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_attempt_count_can_be_configured(self):
        client, session = self.client_for([response_for(503)] * 4, default_retries=3)
        with self.assertRaises(CrawlerHttpError):
            client.get("https://example.com/a", session=session, rate=False)
        self.assertEqual(len(session.calls), 4)

    def test_http_error_is_a_request_exception(self):
        """调用方有 8 处 except requests.RequestException 兜底，继承链不能断。"""
        self.assertIsInstance(CrawlerHttpError(404, "https://example.com/a"), requests.RequestException)


class RequestShapeTests(unittest.TestCase):
    def test_default_headers_carry_desktop_ua_and_accept_language(self):
        session = FakeSession([response_for(200)])
        CrawlerHttpClient().get("https://example.com/a", session=session, rate=False)
        headers = session.calls[0]["headers"]
        self.assertEqual(headers["User-Agent"], UA_DESKTOP)
        self.assertIn("zh-CN", headers["Accept-Language"])

    def test_call_headers_override_defaults(self):
        session = FakeSession([response_for(200)])
        CrawlerHttpClient().get("https://example.com/a", session=session,
                                headers={"User-Agent": "自定义", "Referer": "https://x.test/"},
                                rate=False)
        headers = session.calls[0]["headers"]
        self.assertEqual(headers["User-Agent"], "自定义")
        self.assertEqual(headers["Referer"], "https://x.test/")

    def test_instance_session_is_reused_across_calls(self):
        client = CrawlerHttpClient()
        client.session = FakeSession([response_for(200), response_for(200)])
        client.get("https://example.com/a", rate=False)
        client.get("https://example.com/b", rate=False)
        self.assertEqual(len(client.session.calls), 2)

    def test_no_proxy_passes_none(self):
        session = FakeSession([response_for(200)])
        CrawlerHttpClient().get("https://example.com/a", session=session, rate=False)
        self.assertIsNone(session.calls[0]["proxies"])

    def test_proxy_is_chosen_from_the_list_and_normalised(self):
        session = FakeSession([response_for(200)])
        CrawlerHttpClient().get("https://example.com/a", proxies=["1.2.3.4:8080"],
                                session=session, rate=False)
        self.assertEqual(session.calls[0]["proxies"], {"http": "http://1.2.3.4:8080",
                                                       "https": "http://1.2.3.4:8080"})

    def test_proxy_rotates_randomly_not_by_timestamp(self):
        """原来是 int(time.time()*1000) % len(proxies) —— 同毫秒内的连续请求会撞同一个代理。"""
        session = FakeSession([response_for(200)] * 40)
        client = CrawlerHttpClient()
        with patch("app.crawler_http.random.choice", side_effect=lambda items: items[0]) as chooser:
            client.get("https://example.com/a", proxies=["a.test:1", "b.test:2"],
                       session=session, rate=False)
        chooser.assert_called_once()

    def test_timeout_is_forwarded(self):
        session = FakeSession([response_for(200)])
        CrawlerHttpClient().get("https://example.com/a", session=session, timeout=7, rate=False)
        self.assertEqual(session.calls[0]["timeout"], 7)


class RateLimiterTests(unittest.TestCase):
    def test_limiter_enforces_minimum_interval(self):
        limiter = DomainRateLimiter(max_per_second=20, jitter=0)
        limiter.acquire()
        started = time.time()
        for _ in range(4):
            limiter.acquire()
        # 20/s → 0.05s 间隔，4 次至少要 0.15s（放宽到 0.12 容忍调度抖动）。
        self.assertGreaterEqual(time.time() - started, 0.12)

    def test_jitter_waits_inside_the_lock(self):
        """抖动要真的睡，而且是在锁内睡 —— 否则并发调用方会挤在同一窗口发射。"""
        sleeps = []
        with patch("app.crawler_http.time.sleep", side_effect=sleeps.append), \
                patch("app.crawler_http.random.uniform", return_value=0.02):
            limiter = DomainRateLimiter(max_per_second=1000, jitter=0.02)
            limiter.acquire()
            self.assertEqual(sleeps, [0.02])
            limiter.acquire()
        self.assertEqual(sleeps[-1], 0.02)

    def test_limiter_serialises_concurrent_callers(self):
        limiter = DomainRateLimiter(max_per_second=50, jitter=0)
        stamps = []

        def worker():
            limiter.acquire()
            stamps.append(time.time())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stamps.sort()
        for earlier, later in zip(stamps, stamps[1:]):
            self.assertGreaterEqual(later - earlier, 0.012)

    def test_same_host_shares_one_limiter_and_others_do_not(self):
        client = CrawlerHttpClient()
        self.assertIs(client._limiter("a.test"), client._limiter("a.test"))
        self.assertIsNot(client._limiter("a.test"), client._limiter("b.test"))

    def test_known_slow_domains_get_their_own_rate(self):
        client = CrawlerHttpClient()
        self.assertEqual(client._limiter("www.google.com").max_per_second, 1.0)
        self.assertEqual(client._limiter("unlisted.test").max_per_second, 2.0)

    def test_rate_can_be_skipped_per_call(self):
        session = FakeSession([response_for(200)])
        client = CrawlerHttpClient()
        with patch.object(DomainRateLimiter, "acquire") as acquire:
            client.get("https://example.com/a", session=session, rate=False)
        acquire.assert_not_called()


class SanitizeUrlTests(unittest.TestCase):
    def test_query_values_are_masked(self):
        safe = sanitize_url("https://example.com/s?timestamp=1&signature=secret")
        self.assertIn("https://example.com/s", safe)
        self.assertNotIn("secret", safe)
        self.assertNotIn("timestamp", safe)

    def test_plain_url_is_kept(self):
        self.assertEqual(sanitize_url("https://example.com/a/b"), "https://example.com/a/b")

    def test_error_message_does_not_leak_query(self):
        error = CrawlerHttpError(404, "https://example.com/s?token=leak")
        self.assertNotIn("leak", str(error))


if __name__ == "__main__":
    unittest.main()
