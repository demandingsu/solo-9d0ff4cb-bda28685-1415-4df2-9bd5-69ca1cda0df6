"""fetcher 条件请求单元测试：用 httpx MockTransport 模拟源站响应。"""
from __future__ import annotations

import httpx
import pytest

from app import fetcher


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture()
def mock_httpx(monkeypatch):
    """把 fetcher 模块内的 httpx.get 替换为走 MockTransport 的客户端。"""
    def install(handler):
        client = httpx.Client(transport=_transport(handler))
        monkeypatch.setattr(fetcher.httpx, "get", client.get)
        return client

    return install


def test_fetch_conditional_sends_etag_and_last_modified(mock_httpx):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["inm"] = request.headers.get("if-none-match")
        seen["ims"] = request.headers.get("if-modified-since")
        return httpx.Response(304, headers={"ETag": '"v1"'})

    mock_httpx(handler)
    result = fetcher.fetch_conditional(
        "http://x/notice/A", etag='"v1"', last_modified="Wed, 04 Nov 2026 00:00:00 GMT"
    )

    assert result.status_code == 304
    assert result.not_modified is True
    assert result.text is None
    assert seen["inm"] == '"v1"'
    assert seen["ims"] == "Wed, 04 Nov 2026 00:00:00 GMT"


def test_fetch_conditional_200_returns_body_and_validators(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        # 无条件请求：不允许携带条件头
        assert request.headers.get("if-none-match") is None
        assert request.headers.get("if-modified-since") is None
        return httpx.Response(
            200,
            headers={"ETag": '"v2"', "Last-Modified": "Thu, 05 Nov 2026 00:00:00 GMT"},
            content="<html>new</html>",
        )

    mock_httpx(handler)
    result = fetcher.fetch_conditional("http://x/notice/A")

    assert result.status_code == 200
    assert result.not_modified is False
    assert result.text == "<html>new</html>"
    assert result.etag == '"v2"'
    assert result.last_modified == "Thu, 05 Nov 2026 00:00:00 GMT"


def test_fetch_conditional_4xx_raises_non_retryable(mock_httpx):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    mock_httpx(handler)
    with pytest.raises(fetcher.FetchError) as exc_info:
        fetcher.fetch_conditional("http://x/missing")
    assert exc_info.value.retryable is False
    assert calls["n"] == 1  # 4xx 不重试


def test_fetch_conditional_retries_5xx_then_succeeds(mock_httpx, monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_RETRIES", 2)
    monkeypatch.setattr(fetcher.config, "RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content="<html>ok</html>")

    mock_httpx(handler)
    result = fetcher.fetch_conditional(
        "http://x/notice/A", etag='"v1"', last_modified=None
    )
    assert result.status_code == 200
    assert result.text == "<html>ok</html>"
    assert calls["n"] == 2
