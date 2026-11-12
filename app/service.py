"""同步服务：条件请求抓取列表页 + 详情页，缓存与链归并成套提交。

缓存与事务语义（需求约定）
--------------------------
1. 按页面 URL 缓存**成功解析**的列表/详情原文以及 ETag、Last-Modified；
   下次同步带 ``If-None-Match`` / ``If-Modified-Since`` 发条件请求。
2. 收到 304 时只复用该 URL 的有效缓存；没有缓存则无条件重取，
   无条件重取仍失败则报告该 URL 失败。
3. 列表页返回 304 仍要继续抓取其中的详情页，以发现同址内容更新。
4. 新的 200 页面只有**解析成功后**才能替换缓存；解析失败保留旧缓存。
5. 缓存写入、公告更新与链归并在同一事务内成套提交；归并失败则整体
   回滚，旧缓存与旧链都保留。
6. 单条详情抓取/解析失败时保留旧详情（含旧缓存），同步标记 partial。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from . import chains, config, db, fetcher, parser
from .fetcher import FetchError

logger = logging.getLogger(__name__)

LIST_KIND = "list"
DETAIL_KIND = "detail"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _content_hash(detail: parser.NoticeDetail) -> str:
    raw = "|".join(
        str(getattr(detail, field) or "")
        for field in (
            "notice_no", "project_no", "notice_type", "publish_time",
            "deadline", "ref_notice_no",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _start_run(conn: sqlite3.Connection, list_url: str) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO sync_runs (run_id, list_url, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        (run_id, list_url, _now()),
    )
    conn.commit()
    return run_id


def _finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    total: int,
    fetched: int,
    failed: int,
    kept: int,
    not_modified: int,
    message: str | None,
) -> None:
    conn.execute(
        "UPDATE sync_runs SET finished_at = ?, status = ?, total_listed = ?, "
        "fetched_ok = ?, failed_count = ?, kept_last = ?, not_modified = ?, "
        "message = ? WHERE run_id = ?",
        (_now(), status, total, fetched, failed, kept, not_modified,
         message, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 页面缓存
# ---------------------------------------------------------------------------

def _get_cache(
    conn: sqlite3.Connection, url: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM page_cache WHERE url = ?", (url,)
    ).fetchone()


def _put_cache(
    conn: sqlite3.Connection,
    url: str,
    page_kind: str,
    content: str,
    etag: Optional[str],
    last_modified: Optional[str],
) -> None:
    """写入/替换缓存。仅允许在页面解析成功后调用。"""
    conn.execute(
        "INSERT INTO page_cache (url, page_kind, content, etag, last_modified, "
        "fetched_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT(url) DO UPDATE SET page_kind = excluded.page_kind, "
        "content = excluded.content, etag = excluded.etag, "
        "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at, "
        "last_used_at = NULL",
        (url, page_kind, content, etag, last_modified, _now()),
    )


def _touch_cache(conn: sqlite3.Connection, url: str) -> None:
    """304 复用：仅刷新最近使用时间，正文与校验值保持不变。"""
    conn.execute(
        "UPDATE page_cache SET last_used_at = ? WHERE url = ?",
        (_now(), url),
    )


def _open_page(
    conn: sqlite3.Connection, url: str, page_kind: str
) -> tuple[str, Optional[str], Optional[str], bool]:
    """按缓存条件抓取一个页面，返回 (正文, etag, last_modified, 是否304复用)。

    - 有缓存校验值 -> 发条件请求；304 且缓存有效则复用缓存正文；
    - 304 但本地无缓存 -> 退回无条件请求；无条件请求仍失败则抛出 FetchError；
    - 200 -> 返回新正文与新校验值（调用方解析成功后自行落缓存）。
    """
    cached = _get_cache(conn, url)
    # 校验值存在即发送条件请求；但正文为空的缓存行不能用于 304 复用
    etag = cached["etag"] if cached else None
    last_modified = cached["last_modified"] if cached else None
    cache_valid = cached is not None and bool(cached["content"])

    if etag or last_modified:
        result = fetcher.fetch_conditional(url, etag, last_modified)
        if result.not_modified:
            if cache_valid:
                _touch_cache(conn, url)
                logger.info("页面未变（304），复用缓存：%s", url)
                return cached["content"], etag, last_modified, True
            # 源站认为未变，但本地没有可复用的有效缓存：无条件重取。
            # 重取仍失败（网络/5xx/4xx）时把原始错误向上抛出，由上层报告失败。
            logger.warning("收到 304 但缺少 %s 的有效缓存，无条件重取", url)
            result = fetcher.fetch_conditional(url)
            if result.status_code != 200 or result.text is None:
                raise FetchError(
                    f"抓取 {url} 失败：HTTP {result.status_code} "
                    "且无可用缓存，无条件重取仍未取得页面",
                    retryable=False,
                )
            return result.text, result.etag, result.last_modified, False
    else:
        result = fetcher.fetch_conditional(url)

    if result.status_code != 200 or result.text is None:
        raise FetchError(
            f"抓取 {url} 失败：HTTP {result.status_code}",
            retryable=False,
        )
    return result.text, result.etag, result.last_modified, False


# ---------------------------------------------------------------------------
# 公告持久化
# ---------------------------------------------------------------------------

def _upsert_detail(
    conn: sqlite3.Connection,
    detail: parser.NoticeDetail,
    list_url: str,
) -> None:
    digest = _content_hash(detail)
    existing = conn.execute(
        "SELECT id FROM announcements WHERE notice_no = ?", (detail.notice_no,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE announcements SET project_no = ?, notice_type = ?, "
            "publish_time = ?, deadline = ?, ref_notice_no = ?, source_url = ?, "
            "list_url = ?, content_hash = ?, last_synced_at = ?, "
            "last_fetch_error = NULL WHERE notice_no = ?",
            (
                detail.project_no, detail.notice_type, detail.publish_time,
                detail.deadline, detail.ref_notice_no, detail.source_url,
                list_url, digest, _now(), detail.notice_no,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO announcements (notice_no, project_no, notice_type, "
            "publish_time, deadline, ref_notice_no, source_url, list_url, "
            "content_hash, last_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                detail.notice_no, detail.project_no, detail.notice_type,
                detail.publish_time, detail.deadline, detail.ref_notice_no,
                detail.source_url, list_url, digest, _now(),
            ),
        )


def _record_failure(
    conn: sqlite3.Connection, notice_no: str, list_url: str, error: str
) -> None:
    """详情抓取失败：已有记录则保留上次完整结果，仅刷新同步时间与错误信息。"""
    existing = conn.execute(
        "SELECT id FROM announcements WHERE notice_no = ?", (notice_no,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE announcements SET last_synced_at = ?, last_fetch_error = ? "
            "WHERE notice_no = ?",
            (_now(), error, notice_no),
        )
    else:
        # 从未成功抓取过：落一条占位记录，交由链计算归入孤立异常
        conn.execute(
            "INSERT INTO announcements (notice_no, project_no, notice_type, "
            "list_url, last_synced_at, last_fetch_error) VALUES (?, ?, ?, ?, ?, ?)",
            (notice_no, None, None, list_url, _now(), error),
        )


# ---------------------------------------------------------------------------
# 同步编排
# ---------------------------------------------------------------------------

def sync_list(list_url: str) -> dict:
    """执行一次完整同步并返回统计信息。"""
    conn = db.connect()
    db.init_db(conn)
    run_id = _start_run(conn, list_url)

    def _fail_run(message: str, total: int = 0, fetched: int = 0,
                  failed: int = 0, kept: int = 0,
                  not_modified: int = 0) -> dict:
        _finish_run(conn, run_id, "failed", total, fetched, failed, kept,
                    not_modified, message)
        conn.close()
        return {
            "run_id": run_id, "status": "failed", "total_listed": total,
            "fetched_ok": fetched, "failed_count": failed, "kept_last": kept,
            "not_modified": not_modified, "message": message,
        }

    total = fetched_ok = failed = kept = not_modified = 0
    listed: list[parser.ListedNotice] = []

    # 缓存写入、公告更新与链归并在同一事务内成套提交：
    # 任何未预期错误（含列表页失败、链归并失败）都整体回滚，
    # 旧缓存与旧链完整保留。
    try:
        with conn:  # 无异常退出时 COMMIT，抛异常时 ROLLBACK
            # 1) 列表页：条件抓取 + 解析。失败则整笔回滚，库内数据不动。
            list_html, list_etag, list_lm, list_304 = _open_page(
                conn, list_url, LIST_KIND
            )
            listed = parser.parse_list(list_html, list_url)
            total = len(listed)
            if list_304:
                not_modified += 1
            else:
                # 解析已成功，方可替换列表页缓存
                _put_cache(conn, list_url, LIST_KIND, list_html,
                           list_etag, list_lm)

            # 2) 详情页逐条条件抓取。单条失败降级为 partial，不中断事务。
            for item in listed:
                try:
                    detail_html, etag, lm, was_304 = _open_page(
                        conn, item.detail_url, DETAIL_KIND
                    )
                    # 解析成功之后才允许替换详情缓存
                    detail = parser.parse_detail(detail_html, item.detail_url)
                    if not was_304:
                        _put_cache(conn, item.detail_url, DETAIL_KIND,
                                   detail_html, etag, lm)
                    _upsert_detail(conn, detail, list_url)
                    fetched_ok += 1
                    if was_304:
                        not_modified += 1
                except (FetchError, ValueError) as exc:
                    failed += 1
                    logger.warning(
                        "详情页 %s 抓取/解析失败：%s", item.notice_no, exc
                    )
                    existed = conn.execute(
                        "SELECT id FROM announcements WHERE notice_no = ?",
                        (item.notice_no,),
                    ).fetchone()
                    _record_failure(
                        conn, item.notice_no, list_url, str(exc)
                    )
                    if existed and config.KEEP_LAST_ON_DETAIL_FAILURE:
                        kept += 1

            # 3) 基于库内现有数据全量重算链状态；归并失败则整笔回滚
            chains.recompute(conn, commit=False)
    except (FetchError, ValueError) as exc:
        logger.error("列表页抓取/解析失败，事务回滚：%s", exc)
        return _fail_run(str(exc))
    except Exception as exc:  # noqa: BLE001 - 必须兜底回滚并报告
        logger.exception("同步事务失败，已回滚，保留旧缓存与旧链：%s", exc)
        return _fail_run(
            f"同步事务回滚：{exc}", total=total, fetched=fetched_ok,
            failed=failed, kept=kept, not_modified=not_modified,
        )

    status = "success" if failed == 0 else "partial"
    message = None
    if failed:
        message = f"{failed} 条详情抓取失败，其中 {kept} 条保留上次完整结果"
    _finish_run(conn, run_id, status, total, fetched_ok, failed, kept,
                not_modified, message)
    conn.close()

    return {
        "run_id": run_id, "status": status, "total_listed": total,
        "fetched_ok": fetched_ok, "failed_count": failed, "kept_last": kept,
        "not_modified": not_modified, "message": message,
    }
