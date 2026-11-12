"""端到端：同步接口、链查询、冲突、撤销与失败保留。"""
from __future__ import annotations


def _chains_by_id(payload: dict) -> dict:
    return {c["chain_id"]: c for c in payload["chains"]}


def _orphans_by_no(payload: dict) -> dict:
    return {o["notice_no"]: o for o in payload["orphans"]}


def test_full_sync_and_counts(client, site_base_url):
    c, _ = client
    resp = c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["total_listed"] == 18
    assert body["fetched_ok"] == 18
    assert body["failed_count"] == 0
    # 首次同步没有任何缓存，不存在 304
    assert body["not_modified"] == 0

    result = c.get("/api/v1/chains").json()
    # 7 条链：A、B、C、G-001、G-101、H、I
    assert len(result["chains"]) == 7
    # 4 条孤立异常：D-001、E-001、E-002、F-001
    assert len(result["orphans"]) == 4


def test_normal_chain_status_and_effective_deadline(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chain = _chains_by_id(c.get("/api/v1/chains").json())["A-001"]

    assert chain["status"] == "ok"
    # A-002 未给出截止时间 -> 沿用链上此前的截止时间
    # A-003 给出更晚的截止时间 -> 有效截止时间取最新
    assert chain["effective_deadline"] == "2026-11-25 15:00:00"
    assert [a["notice_no"] for a in chain["announcements"]] == [
        "A-001", "A-002", "A-003"
    ]
    # 链上公告都带有来源链接
    assert all(a["source_url"].endswith(f"/notice/{a['notice_no']}")
               for a in chain["announcements"])


def test_revocation_invalidates_chain(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chain = _chains_by_id(c.get("/api/v1/chains").json())["B-001"]

    assert chain["status"] == "cancelled"
    assert chain["effective_deadline"] is None
    assert [a["notice_no"] for a in chain["announcements"]] == [
        "B-001", "B-002", "B-003"
    ]


def test_parallel_corrections_conflict_without_guessing_deadline(
    client, site_base_url
):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chain = _chains_by_id(c.get("/api/v1/chains").json())["C-001"]

    assert chain["status"] == "conflict"
    # 冲突时不推测截止时间
    assert chain["effective_deadline"] is None
    for ann in chain["announcements"]:
        assert "parallel_correction" in ann["conflict_reasons"]
        assert ann["effective_deadline"] is None


def test_same_project_first_notices_remain_independent(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    chains = _chains_by_id(c.get("/api/v1/chains").json())

    g1 = chains["G-001"]
    g2 = chains["G-101"]
    assert g1["project_no"] == g2["project_no"] == "PRJ-G"
    assert [a["notice_no"] for a in g1["announcements"]] == ["G-001"]
    assert [a["notice_no"] for a in g2["announcements"]] == ["G-101", "G-102"]
    assert g1["status"] == g2["status"] == "ok"
    assert g2["effective_deadline"] == "2026-11-28 09:30:00"


def test_orphans_missing_reference_and_cycle(client, site_base_url):
    c, _ = client
    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    orphans = _orphans_by_no(c.get("/api/v1/chains").json())

    assert "missing_reference" in orphans["D-001"]["conflict_reasons"]
    assert "missing_reference" in orphans["F-001"]["conflict_reasons"]

    for no in ("E-001", "E-002"):
        assert "reference_cycle" in orphans[no]["conflict_reasons"]
        assert orphans[no]["effective_deadline"] is None
        assert orphans[no]["status"] == "orphan"


def test_detail_failure_keeps_last_complete_result(
    client, site_base_url, set_fail
):
    c, _ = client
    list_url = f"{site_base_url}/announcements"

    # 第一次同步：全部成功
    first = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert first["status"] == "success"
    before = c.get("/api/v1/announcements/H-001").json()
    assert before["deadline"] == "2026-11-30 09:30:00"
    assert before["last_fetch_error"] is None

    # 打开详情页故障后再次同步
    set_fail(True)
    second = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert second["status"] == "partial"
    assert second["failed_count"] == 1
    assert second["kept_last"] == 1

    after = c.get("/api/v1/announcements/H-001").json()
    # 上次完整结果保留
    assert after["deadline"] == "2026-11-30 09:30:00"
    assert after["notice_type"] == "first"
    assert "500" in after["last_fetch_error"]
    # 链 H 仍可正常计算
    chains = _chains_by_id(c.get("/api/v1/chains").json())
    assert chains["H-001"]["status"] == "ok"

    # 故障恢复后同步，错误信息清空
    set_fail(False)
    third = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert third["status"] == "success"
    healed = c.get("/api/v1/announcements/H-001").json()
    assert healed["last_fetch_error"] is None


def test_sync_runs_recorded_and_health(client, site_base_url):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}

    c.post("/api/v1/sync", json={"list_url": f"{site_base_url}/announcements"})
    runs = c.get("/api/v1/sync-runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["total_listed"] == 18


def test_sync_bad_url_rejected(client):
    c, _ = client
    resp = c.post("/api/v1/sync", json={"list_url": "not-a-url"})
    assert resp.status_code == 400


def test_sync_list_page_unreachable(client):
    c, _ = client
    # 监听一个保证无人监听的端口
    resp = c.post(
        "/api/v1/sync",
        json={"list_url": "http://127.0.0.1:1/announcements"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["total_listed"] == 0


def _cache_by_url(client) -> dict:
    c, _ = client
    entries = c.get("/api/v1/page-cache").json()["entries"]
    return {e["url"]: e for e in entries}


def test_second_sync_uses_conditional_requests_and_304_cache(client,
                                                             site_base_url):
    c, _ = client
    list_url = f"{site_base_url}/announcements"

    first = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert first["status"] == "success"

    cache = _cache_by_url(client)
    # 列表页 + 18 个详情页全部有缓存，且带至少一种条件校验值
    assert len(cache) == 19
    list_entry = cache[list_url]
    assert list_entry["page_kind"] == "list"
    assert list_entry["etag"] or list_entry["last_modified"]
    assert list_entry["last_used_at"] is None
    detail_entry = cache[f"{site_base_url}/notice/A-001"]
    assert detail_entry["page_kind"] == "detail"
    assert detail_entry["content_length"] > 0

    # 第二次同步：所有页面都未变化 -> 列表 + 18 详情全部 304 复用
    second = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert second["status"] == "success"
    assert second["total_listed"] == 18
    assert second["not_modified"] == 19
    # 304 复用不算重新抓取成功，但也没有失败
    assert second["failed_count"] == 0

    # 复用后刷新了 last_used_at，fetched_at（缓存写入时间）保持不变
    reused = _cache_by_url(client)[list_url]
    assert reused["last_used_at"] is not None
    assert reused["etag"] == list_entry["etag"]
    assert reused["last_modified"] == list_entry["last_modified"]
    assert reused["fetched_at"] == list_entry["fetched_at"]


def test_list_304_still_detects_same_url_detail_update(client, site_base_url,
                                                       set_content):
    c, _ = client
    list_url = f"{site_base_url}/announcements"
    detail_url = f"{site_base_url}/notice/I-001"

    c.post("/api/v1/sync", json={"list_url": list_url})
    before = c.get("/api/v1/announcements/I-001").json()
    assert before["deadline"] == "2026-12-05 09:30:00"
    old_etag = _cache_by_url(client)[detail_url]["etag"]

    # 切换 I-001 的同址内容版本（URL 不变、正文与校验值变化）
    set_content(2)

    # 再次同步：列表页 304，但 I-001 详情 200，其余 17 个详情 304
    body = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert body["status"] == "success"
    assert body["not_modified"] == 18  # 列表 1 + 未变详情 17
    assert body["failed_count"] == 0

    after = c.get("/api/v1/announcements/I-001").json()
    assert after["deadline"] == "2026-12-12 15:00:00"
    assert after["last_fetch_error"] is None
    # 详情缓存被新版本替换（新 ETag），链按新内容重算
    new_entry = _cache_by_url(client)[detail_url]
    assert new_entry["etag"] != old_etag
    assert new_entry["last_used_at"] is None
    chains = _chains_by_id(c.get("/api/v1/chains").json())
    assert chains["I-001"]["status"] == "ok"
    assert chains["I-001"]["effective_deadline"] == "2026-12-12 15:00:00"

    # 恢复版本后，缓存可以再次正常更新回旧内容
    set_content(1)
    c.post("/api/v1/sync", json={"list_url": list_url})
    healed = c.get("/api/v1/announcements/I-001").json()
    assert healed["deadline"] == "2026-12-05 09:30:00"


def test_detail_failure_keeps_old_detail_cache_and_marks_partial(
    client, site_base_url, set_fail
):
    c, _ = client
    list_url = f"{site_base_url}/announcements"
    detail_url = f"{site_base_url}/notice/H-001"

    c.post("/api/v1/sync", json={"list_url": list_url})
    good_cache = _cache_by_url(client)[detail_url]

    set_fail(True)
    body = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert body["status"] == "partial"
    assert body["failed_count"] == 1
    assert body["kept_last"] == 1

    # 抓取失败：旧详情缓存原样保留（校验值与写入时间均不变）
    kept_cache = _cache_by_url(client)[detail_url]
    assert kept_cache["etag"] == good_cache["etag"]
    assert kept_cache["fetched_at"] == good_cache["fetched_at"]
    assert kept_cache["last_used_at"] == good_cache["last_used_at"]

    set_fail(False)
    healed = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert healed["status"] == "success"
    assert c.get("/api/v1/announcements/H-001").json()["last_fetch_error"] is None


def test_304_without_cache_falls_back_to_unconditional_fetch(
    client, site_base_url, monkeypatch
):
    c, _ = client
    list_url = f"{site_base_url}/announcements"

    # 先正常同步一次建立缓存
    c.post("/api/v1/sync", json={"list_url": list_url})

    # 模拟“缓存行丢失但校验值仍被携带”的边缘情况：
    # 直接删掉某个详情的缓存行，_open_page 应在 304 后无条件重取成功
    from app import db
    conn = db.connect()
    conn.execute("DELETE FROM page_cache WHERE url = ?",
                 (f"{site_base_url}/notice/A-001",))
    conn.commit()
    conn.close()

    body = c.post("/api/v1/sync", json={"list_url": list_url}).json()
    assert body["status"] == "success"
    assert body["failed_count"] == 0
    # 缓存行被重新建立
    assert f"{site_base_url}/notice/A-001" in _cache_by_url(client)


def test_failed_list_sync_rolls_back_cache_touch(client, site_base_url):
    c, _ = client
    list_url = f"{site_base_url}/announcements"
    c.post("/api/v1/sync", json={"list_url": list_url})
    entry_before = _cache_by_url(client)[list_url]

    # 列表页不可达：同步失败，且不能改动既有缓存
    failed = c.post(
        "/api/v1/sync",
        json={"list_url": "http://127.0.0.1:1/announcements"},
    ).json()
    assert failed["status"] == "failed"

    entry_after = _cache_by_url(client)[list_url]
    assert entry_after["etag"] == entry_before["etag"]
    assert entry_after["fetched_at"] == entry_before["fetched_at"]
    assert entry_after["last_used_at"] == entry_before["last_used_at"]
