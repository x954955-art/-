import pytest
import requests

from furima_finder.http import FetchError, HttpClient


class Response:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.headers_seen = []

    def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        self.headers_seen.append(headers)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def client(outcomes, **kwargs):
    clock = Clock()
    session = Session(outcomes)
    http = HttpClient(session=session, sleep=clock.sleep, clock=clock, **kwargs)
    return http, session, clock


def test_retries_temporary_errors_with_backoff():
    http, session, clock = client([Response(503), Response(429, {"Retry-After": "7"}), Response(200)], min_interval=0)
    assert http.request("GET", "https://example.com/").status_code == 200
    assert session.calls == 3
    assert clock.sleeps == [2.0, 7.0]


def test_does_not_retry_forbidden():
    http, session, _ = client([Response(403), Response(200)], min_interval=0)
    with pytest.raises(FetchError, match="HTTP 403"):
        http.request("GET", "https://example.com/")
    assert session.calls == 1


def test_proxy_errors_fail_fast():
    http, session, _ = client([requests.exceptions.ProxyError("blocked")], min_interval=0)
    with pytest.raises(FetchError, match="プロキシ"):
        http.request("GET", "https://example.com/")
    assert session.calls == 1


def test_gives_up_after_max_retries():
    errors = [requests.exceptions.ConnectionError("down")] * 3
    http, session, _ = client(errors, min_interval=0, max_retries=2)
    with pytest.raises(FetchError, match="接続できません"):
        http.request("GET", "https://example.com/")
    assert session.calls == 3


def test_allowed_status_is_returned():
    http, _, _ = client([Response(404)], min_interval=0)
    assert http.request("GET", "https://example.com/", allow_status=(404,)).status_code == 404


def test_waits_between_requests_and_rebuilds_headers_each_try():
    counter = iter(range(100))
    http, session, clock = client([Response(500), Response(200), Response(200)], min_interval=1.5)
    http.request("GET", "https://example.com/", headers=lambda: {"X-Try": str(next(counter))})
    assert [h["X-Try"] for h in session.headers_seen] == ["0", "1"]
    assert session.headers_seen[0]["User-Agent"].startswith("Mozilla/5.0")
    http.request("GET", "https://example.com/", headers={"X-Try": "x"})
    # 1回目のリトライ待ち（2秒）は間隔より長いので追加の待ちなし、3回目の前は間隔分待つ
    assert clock.sleeps == [2.0, 1.5]
