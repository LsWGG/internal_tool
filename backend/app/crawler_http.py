"""采集器统一 HTTP 客户端：分类重试、按域名限流、代理随机轮换、session 复用。

移植自兄弟项目 douyin-downloader 的两处成熟做法（`core/api_client.py` 的请求
咽喉 + `control/rate_limiter.py` 的节流器），但**保持本项目的同步线程模型**：
采集任务跑在 `ThreadPoolExecutor` 里，这里不引入 asyncio/aiohttp，锁一律用
`threading.Lock`。

与现状的对应关系：原来 `CrawlerTaskManager._request` 是唯一公共请求函数（16 个
调用点），它单次 GET、无重试、代理按毫秒时间戳取模轮换。现在 `_request` 保留为
薄委托（测试仍可整体替换它），真正的行为在这个类里。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

# User-Agent 常量。收敛散落在 crawler_manager 与 douyin_profiles 里的 5 处重复定义，
# **字符串逐个字符保留原值** —— 站点会按 UA 分支返回不同页面，改值等于改行为。
#
# 桌面短 UA：`_request` 原本用的值，没有 "(KHTML, like Gecko)" 那段。
UA_DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"
# 桌面完整 UA：三处 Playwright context 原本用的值。
UA_DESKTOP_FULL = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# 微信公众号移动索引页要用移动版 UA 才给移动端结构。
UA_WECHAT_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 Mobile/15E148")
# 抖音签名绑定 UA，改这个值会让 X-Bogus 与 UA 对不上。
UA_DOUYIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
# TikTok 评论接口原本就发裸 UA，沿用。
UA_MINIMAL = "Mozilla/5.0"

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}

# 退避表按尝试次数取用。对应 douyin-downloader 的 (1, 2, 5)。
BACKOFF_SECONDS = (1.0, 2.0, 5.0)
# 「初次之后的次数」语义（与那边 RetryHandler 一致）：2 → 共 3 次尝试。
DEFAULT_RETRIES = 2
# 可重试的状态码：限流与网关类瞬时故障。**403 刻意不在内** —— 它对多数站点是确定性
# 拒绝（反爬判定），重试只会加深刻度；这与 douyin-downloader 把 403 当 WAF 限流重试
# 的选择不同，那边有登录态与代理池可换，这里没有。
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_RATE = 2.0
# 搜索引擎更容易因频率触发验证码，单独降速。
DOMAIN_RATES = {
    "html.duckduckgo.com": 1.0,
    "duckduckgo.com": 1.0,
    "www.google.com": 1.0,
    "www.bing.com": 1.0,
    "cn.bing.com": 1.0,
}


def sanitize_url(url: str) -> str:
    """日志用：保留 scheme/host/path，把 query 与 fragment 打码。

    搜索关键词、时间戳签名这类东西都在 query 里，落到日志就等于留痕。
    """
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return "<无法解析>"
    query = "?…" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query.lstrip("?"), ""))


class CrawlerHttpError(requests.RequestException):
    """终态 HTTP 错误（非 2xx/3xx 且不再重试）。

    **必须继承 `requests.RequestException`**：调用方有 8 处
    `except requests.RequestException` 兜底，换一个无关的异常类型会让它们全部脱钩。
    """

    def __init__(self, status: int, url: str, message: str = ""):
        self.status = status
        self.url = sanitize_url(url)
        super().__init__(message or f"HTTP {status}：{self.url}")


class DomainRateLimiter:
    """按域名节流：同域名两次请求之间至少隔 `min_interval`。

    抖动放在锁内（照搬 rate_limiter.py 的注释所指的那条规矩）：要保证下一个请求
    相对**真实发射时刻**计间隔，否则并发调用方会挤在同一窗口里。
    """

    def __init__(self, max_per_second: float = DEFAULT_RATE, jitter: float = 0.5):
        self.max_per_second = max_per_second if max_per_second > 0 else DEFAULT_RATE
        self.min_interval = 1.0 / self.max_per_second
        self.jitter = max(0.0, jitter)
        self.last_request = 0.0
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            elapsed = time.time() - self.last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            if self.jitter:
                time.sleep(random.uniform(0, self.jitter))
            self.last_request = time.time()


class CrawlerHttpClient:
    """采集器的请求咽喉。所有采集源共用，行为集中在这里。

    `session` 可以按调用传入（微信公众号的搜狗索引要在一个 session 里保持
    Referer 与 cookie），不传就用实例自己那个复用的 session。
    """

    def __init__(self, default_retries: int = DEFAULT_RETRIES,
                 domain_rates: dict[str, float] | None = None,
                 jitter: float = 0.5):
        self.default_retries = max(0, int(default_retries))
        self.session = requests.Session()
        self.domain_rates = {**(domain_rates or DOMAIN_RATES)}
        self.jitter = jitter
        self._limiters: dict[str, DomainRateLimiter] = {}
        self._limiters_lock = threading.Lock()

    def _limiter(self, host: str) -> DomainRateLimiter:
        with self._limiters_lock:
            limiter = self._limiters.get(host)
            if limiter is None:
                # 认不出的域名用默认速率；已知域名用自己那一档。
                rate = self.domain_rates.get(host, DEFAULT_RATE)
                limiter = DomainRateLimiter(rate, self.jitter)
                self._limiters[host] = limiter
            return limiter

    @staticmethod
    def _proxy_for(proxies) -> str | None:
        """随机挑一个代理（原来是按毫秒时间戳取模 —— 同一毫秒窗口里的连续请求会
        撞在同一个代理上，且列表轮回完全可预测）。"""
        if not proxies:
            return None
        proxy = str(random.choice(list(proxies))).strip()
        if not proxy:
            return None
        if not proxy.startswith(("http://", "https://", "socks5://")):
            proxy = "http://" + proxy
        return proxy

    def get(self, url: str, proxies=(), timeout: float = 30, session=None,
            headers: dict | None = None, retries: int | None = None,
            rate: bool = True) -> requests.Response:
        """GET 一个地址，按需重试。

        可重试：限流/网关类状态码（RETRYABLE_STATUS）与网络异常。
        终态：其余 4xx/5xx 不重试，抛 CrawlerHttpError。
        `retries=None` 用实例默认；`retries=0` 表示调用方自带重试语义，这里不叠加。
        """
        # 进循环前定死本次执行的重试次数（照搬 RetryHandler 那条注释的理由：
        # 循环内再读可变属性，值被改小会让剩余尝试全部跳过退避、在同一毫秒连发）。
        total_attempts = (self.default_retries if retries is None else max(0, int(retries))) + 1
        client = session or self.session
        request_headers = dict(BASE_HEADERS)
        request_headers["User-Agent"] = UA_DESKTOP
        request_headers.update(headers or {})
        host = urlsplit(str(url)).netloc.lower()

        last_error: Exception | None = None
        for attempt in range(1, total_attempts + 1):
            if rate:
                self._limiter(host).acquire()
            proxy = self._proxy_for(proxies)
            started = time.time()
            try:
                response = client.get(
                    url, headers=request_headers,
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                    timeout=timeout, allow_redirects=True)
            except requests.RequestException as exc:
                last_error = exc
                self._log_attempt(host, url, attempt, total_attempts, "错误", started, exc)
                if attempt < total_attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise
            status = response.status_code
            if status in RETRYABLE_STATUS:
                self._log_attempt(host, url, attempt, total_attempts, status, started)
                # 用异常占位，重试耗尽后抛出的就是这个终态错误。
                last_error = CrawlerHttpError(
                    status, url, f"HTTP {status}（已重试 {total_attempts} 次）：{sanitize_url(url)}")
                if attempt < total_attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise last_error
            if status >= 400:
                self._log_attempt(host, url, attempt, total_attempts, status, started)
                raise CrawlerHttpError(status, url)
            self._log_attempt(host, url, attempt, total_attempts, status, started)
            return response

        # 循环正常走完只可能是不该发生的情况（每次迭代都以 return/raise/continue 收尾）。
        raise last_error or CrawlerHttpError(0, url, f"请求未产生结果：{sanitize_url(url)}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]

    @staticmethod
    def _log_attempt(host, url, attempt, total, status, started, exc=None) -> None:
        """结构化一行一请求，URL 脱敏。只在重试/失败时值得看，成功也留一条便于排查。"""
        logger.info(
            "crawler_http attempt=%d/%d status=%s duration_ms=%d domain=%s url=%s%s",
            attempt, total, status, int((time.time() - started) * 1000), host,
            sanitize_url(url), f" error={exc!r}" if exc is not None else "")


# 给 `CrawlerTaskManager._request` 这类拿不到实例的调用点兜底（例如只做了
# `object.__new__` 的测试替身）。
_DEFAULT_HTTP = CrawlerHttpClient()


def default_client() -> CrawlerHttpClient:
    return _DEFAULT_HTTP
