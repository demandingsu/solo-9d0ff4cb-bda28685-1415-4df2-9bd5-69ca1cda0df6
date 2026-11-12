"""HTTP 抓取层：带超时、有限重试与条件请求（ETag / Last-Modified）。

条件请求语义
------------
- 调用方提供该 URL 此前成功响应缓存下来的 ETag / Last-Modified 时，
  抓取层发送 ``If-None-Match`` / ``If-Modified-Since``；
- 源站返回 304 时，抓取层不持有缓存正文，仅通过 :class:`FetchResult`
  告知调用方“页面未变”，由调用方复用自己的有效缓存；
- 没有可用校验值时发送普通 GET（无条件请求）；
- 源站返回 200 时连同响应头中的新 ETag / Last-Modified 一并返回，
  调用方须在**解析成功之后**才能用它们更新缓存。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """页面抓取失败（网络错误或非 2xx/304 响应）。

    ``retryable`` 标记是否允许重试：网络错误与 5xx 可重试，
    4xx（含无条件重取后仍失败的情形）立即失败。
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class FetchResult:
    """一次条件 GET 的结果。

    status_code:
        源站响应状态码（200 或 304）。
    text:
        响应正文；304 时为 None，调用方应改用本地缓存正文。
    etag / last_modified:
        响应头中的校验值；304 响应通常不携带，此时为 None，
        调用方应继续沿用缓存中的旧校验值。
    """

    status_code: int
    text: Optional[str]
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


def _conditional_headers(
    etag: Optional[str], last_modified: Optional[str]
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def fetch_conditional(
    url: str,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
) -> FetchResult:
    """发送（可能带条件的）GET，返回 200 正文或 304 标记。

    网络错误与 5xx 按配置重试（重试时继续携带条件头）；4xx 立即失败。
    """
    headers = _conditional_headers(etag, last_modified)
    last_error: Optional[Exception] = None

    for attempt in range(config.FETCH_RETRIES + 1):
        try:
            resp = httpx.get(
                url,
                params=None,
                headers=headers,
                timeout=config.FETCH_TIMEOUT,
                follow_redirects=True,
            )
            if resp.status_code == 304:
                return FetchResult(
                    status_code=304,
                    text=None,
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                )
            if resp.status_code == 200:
                resp.encoding = resp.charset_encoding or "utf-8"
                return FetchResult(
                    status_code=200,
                    text=resp.text,
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                )
            if resp.status_code >= 500:
                raise FetchError(
                    f"服务端错误 HTTP {resp.status_code}", retryable=True
                )
            # 404 等客户端错误不重试
            raise FetchError(f"HTTP {resp.status_code}", retryable=False)
        except (httpx.HTTPError, FetchError) as exc:
            last_error = exc
            retryable = (
                isinstance(exc, httpx.HTTPError)
                or getattr(exc, "retryable", False)
            )
            if not retryable or attempt == config.FETCH_RETRIES:
                break
            sleep_for = config.RETRY_BACKOFF * (attempt + 1)
            logger.warning(
                "抓取 %s 第 %d 次失败：%s，%.1fs 后重试",
                url, attempt + 1, exc, sleep_for,
            )
            time.sleep(sleep_for)

    raise FetchError(f"抓取 {url} 失败：{last_error}")


def fetch(url: str) -> str:
    """无条件抓取页面文本，保留供不需要缓存的场景使用。"""
    result = fetch_conditional(url)
    return result.text or ""
