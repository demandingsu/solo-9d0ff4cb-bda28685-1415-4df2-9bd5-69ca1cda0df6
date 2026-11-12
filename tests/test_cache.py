"""缓存替换与成套提交（事务原子性）的服务级测试。"""
from __future__ import annotations

import pytest

from app import parser, service


def test_parse_failure_keeps_old_cache_and_announcement(client, site_base_url,
                                                        monkeypatch):
    c, _ = client
    list_url = f"{site_base_url}/announcements"
    detail_url = f"{site_base_url}/notice/A-001"

    c.post("/api/v1/sync", json={"list_url": list_url})
    cache_before = c.get("/api/v1/page-cache").json()["entries"]
    old_entry = next(e for e in cache_before if e["url"] == detail_url)
    ann_before = c.get("/api/v1/announcements/A-001").json()

    # 让 A-001 的 200 新页面解析失败：缓存与公告都必须保持旧值
    real_parse_detail = parser.parse_detail

    def flaky_parse(html: str, source_url: str):
        if source_url == detail_url:
            raise ValueError("模拟详情解析失败")
        return real_parse_detail(html, source_url)

    monkeypatch.setattr(service.parser, "parse_detail", flaky_parse)

    body = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert body["status"] == "partial"
    assert body["failed_count"] == 1
    assert body["kept_last"] == 1

    entries = c.get("/api/v1/page-cache").json()["entries"]
    kept_entry = next(e for e in entries if e["url"] == detail_url)
    assert kept_entry["etag"] == old_entry["etag"]
    assert kept_entry["fetched_at"] == old_entry["fetched_at"]
    assert kept_entry["content_length"] == old_entry["content_length"]

    ann_after = c.get("/api/v1/announcements/A-001").json()
    assert ann_after["deadline"] == ann_before["deadline"]
    assert "模拟详情解析失败" in ann_after["last_fetch_error"]


def test_chain_merge_failure_rolls_back_cache_and_announcements(
    client, site_base_url, monkeypatch
):
    c, _ = client
    list_url = f"{site_base_url}/announcements"

    first = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert first["status"] == "success"

    cache_snapshot = c.get("/api/v1/page-cache").json()["entries"]
    runs_before = len(c.get("/api/v1/sync-runs").json()["runs"])

    # 让链归并在事务提交前失败：整笔事务（含缓存与公告更新）必须回滚
    def boom(_conn, commit=True):  # noqa: ANN001
        raise RuntimeError("模拟链归并失败")

    monkeypatch.setattr(service.chains, "recompute", boom)

    failed = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert failed["status"] == "failed"
    assert "回滚" in (failed["message"] or "")

    # 缓存没有任何变化（第二次同步本应全部 304，仅 last_used_at 也被回滚）
    cache_after = c.get("/api/v1/page-cache").json()["entries"]
    assert cache_after == cache_snapshot

    # 公告数据仍是上一次成功同步的结果，链状态可正常查询
    chains = c.get("/api/v1/chains").json()["chains"]
    assert {ch["chain_id"] for ch in chains} == {
        "A-001", "B-001", "C-001", "G-001", "G-101", "H-001", "I-001",
    }
    runs = c.get("/api/v1/sync-runs").json()["runs"]
    assert len(runs) == runs_before + 1
    assert runs[0]["status"] == "failed"


def test_304_without_valid_cache_unconditionally_refetches(
    client, site_base_url, monkeypatch
):
    """服务层 _open_page：304 但无有效缓存正文时退回无条件请求并成功。"""
    from app import db, fetcher

    url = f"{site_base_url}/announcements"
    c, _ = client  # 初始化库表
    conn = db.connect()
    try:
        # 预置“不可用”缓存行：有 ETag（会触发条件请求），但正文为空，
        # 模拟缓存写入失败/被截断后只剩校验值的边缘情况
        conn.execute(
            "INSERT INTO page_cache (url, page_kind, content, etag, "
            "last_modified, fetched_at) VALUES (?, 'list', '', ?, NULL, ?)",
            (url, '"stale-etag"', "2026-11-01 00:00:00"),
        )
        conn.commit()

        calls: list[tuple[object, object]] = []
        real_fetch = fetcher.fetch_conditional

        def fake_fetch(fetch_url, etag=None, last_modified=None):
            calls.append((etag, last_modified))
            if etag or last_modified:
                # 源站对条件请求回答 304，但本地缓存正文不可用
                return fetcher.FetchResult(status_code=304, text=None)
            # 无条件请求走真实网络，应能取回 200 正文
            return real_fetch(fetch_url)

        monkeypatch.setattr(service.fetcher, "fetch_conditional", fake_fetch)
        html, etag, last_modified, was_304 = service._open_page(
            conn, url, service.LIST_KIND
        )

        assert was_304 is False
        assert etag and last_modified
        assert "announcement-list" in html
        # 第一次带条件头收到 304，第二次退化为无条件请求
        assert len(calls) == 2
        assert calls[0] == ('"stale-etag"', None)
        assert calls[1] == (None, None)
    finally:
        conn.close()


def test_304_without_cache_and_refetch_failure_reports_error(
    client, site_base_url, monkeypatch
):
    """304 无有效缓存且无条件重取也失败时，必须向上报告失败。"""
    from app import db, fetcher

    url = f"{site_base_url}/announcements"
    c, _ = client
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO page_cache (url, page_kind, content, etag, "
            "last_modified, fetched_at) VALUES (?, 'list', '', ?, NULL, ?)",
            (url, '"stale-etag"', "2026-11-01 00:00:00"),
        )
        conn.commit()

        def fake_fetch(fetch_url, etag=None, last_modified=None):
            if etag or last_modified:
                return fetcher.FetchResult(status_code=304, text=None)
            raise fetcher.FetchError("无条件重取仍然失败", retryable=False)

        monkeypatch.setattr(service.fetcher, "fetch_conditional", fake_fetch)
        with pytest.raises(fetcher.FetchError, match="无条件重取仍然失败"):
            service._open_page(conn, url, service.LIST_KIND)
    finally:
        conn.close()
