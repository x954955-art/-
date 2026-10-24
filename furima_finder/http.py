"""サイトに負荷をかけないよう、リクエスト間隔とリトライを管理するHTTPクライアント。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# 一時的なエラーとみなして待ってから再試行するステータス
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """リトライしても取得できなかったときに送出する。"""


HeadersArg = Mapping[str, str] | Callable[[], Mapping[str, str]] | None


class HttpClient:
    def __init__(
        self,
        *,
        min_interval: float = 1.5,
        timeout: float = 20.0,
        max_retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: HeadersArg = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        allow_status: tuple[int, ...] = (),
    ) -> requests.Response:
        """リクエストを送る。headers に関数を渡すと試行ごとに作り直す（署名付きヘッダー用）。"""
        host = urlsplit(url).netloc
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            merged = {"User-Agent": self.user_agent, "Accept-Language": "ja-JP,ja;q=0.9"}
            extra = headers() if callable(headers) else headers
            if extra:
                merged.update(extra)
            backoff = 2.0 ** (attempt + 1)
            try:
                response = self.session.request(
                    method, url, headers=merged, params=params, json=json, timeout=self.timeout
                )
            except (requests.exceptions.ProxyError, requests.exceptions.SSLError) as exc:
                # 待っても直らない種類の失敗なので再試行しない
                last_error = _describe_exception(exc)
                break
            except requests.RequestException as exc:
                last_error = _describe_exception(exc)
            else:
                if response.status_code < 400 or response.status_code in allow_status:
                    return response
                last_error = _describe_status(response.status_code)
                if response.status_code not in RETRY_STATUSES:
                    break
                backoff = max(backoff, _retry_after(response))
            if attempt < self.max_retries:
                self._sleep(backoff)
        raise FetchError(f"{host} から取得できませんでした（{last_error}）")


def _describe_exception(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.exceptions.ProxyError):
        return "プロキシに接続を拒否されました"
    if isinstance(exc, requests.exceptions.SSLError):
        return "SSL証明書のエラー"
    if isinstance(exc, requests.exceptions.Timeout):
        return "タイムアウト"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "接続できません。インターネット接続を確認してください"
    return type(exc).__name__


def _describe_status(status: int) -> str:
    if status == 403:
        return "HTTP 403: アクセスを拒否されました。サイトの仕様変更やアクセス制限の可能性があります"
    if status == 429:
        return "HTTP 429: アクセスが多すぎます。時間をおくか --interval を長くしてください"
    if status >= 500:
        return f"HTTP {status}: サイト側のエラーです。時間をおいて試してください"
    return f"HTTP {status}"


def _retry_after(response: requests.Response) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        return min(float(value), 120.0)
    except ValueError:
        return 0.0
