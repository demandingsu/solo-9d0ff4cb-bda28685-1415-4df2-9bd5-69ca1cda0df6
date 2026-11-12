"""示例公共采购公告站（仅依赖 Python 标准库）。

用于在本地/容器内演示采集 API 的条件请求与缓存能力，覆盖以下场景：
  链 A：首次 + 更正（含截止时间继承）
  链 B：首次 + 更正 + 撤销（撤销使链失效）
  链 C：并行更正（同一公告有两个后继 -> 冲突）
  链 D：引用编号不存在（missing_reference -> 冲突 + 孤立）
  链 E：引用成环（reference_cycle -> 孤立）
  链 F：更正无引用编号（missing_reference -> 孤立）
  链 G：同一项目两个独立首次公告（保持两条独立链）
  公告 H：可通过 /fail-toggle 控制详情页返回 500，验证失败保留上次结果
  公告 I：URL 不变、内容可通过 /content-toggle 切换版本，验证
          “列表页 304 仍检查详情，发现同址内容更新”

所有页面都携带 ETag 与 Last-Modified，并响应条件请求：
  If-None-Match / If-Modified-Since 命中 -> 304（无正文）
  校验值不命中 -> 200（新正文 + 新 ETag/Last-Modified）

控制接口：
  GET /fail-toggle?enabled=1|0      开关 H-001 详情页的 500 故障
  GET /fail-status                  查询 H-001 故障开关
  GET /content-toggle?version=1|2   切换 I-001 同址内容版本
  GET /content-status               查询 I-001 当前内容版本
"""
from __future__ import annotations

import hashlib
import os
from email.utils import parsedate_to_datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 每条公告：编号、项目编号、类型、发布时间、截止时间、引用编号
NOTICES: list[dict] = [
    # 链 A：正常链，COR-A-02 不带截止时间（应继承首次的截止时间）
    {"no": "A-001", "project": "PRJ-A", "type": "首次公告",
     "publish": "2026-10-20 09:00:00", "deadline": "2026-11-20 09:30:00",
     "ref": ""},
    {"no": "A-002", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-10-25 14:00:00", "deadline": "",
     "ref": "A-001"},
    {"no": "A-003", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-11-01 10:00:00", "deadline": "2026-11-25 15:00:00",
     "ref": "A-002"},

    # 链 B：撤销使整条链失效
    {"no": "B-001", "project": "PRJ-B", "type": "首次公告",
     "publish": "2026-10-21 09:00:00", "deadline": "2026-11-21 09:30:00",
     "ref": ""},
    {"no": "B-002", "project": "PRJ-B", "type": "更正公告",
     "publish": "2026-10-26 14:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": "B-001"},
    {"no": "B-003", "project": "PRJ-B", "type": "撤销公告",
     "publish": "2026-10-28 16:00:00", "deadline": "",
     "ref": "B-002"},

    # 链 C：并行更正（C-002 与 C-003 同时引用 C-001）
    {"no": "C-001", "project": "PRJ-C", "type": "首次公告",
     "publish": "2026-10-22 09:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": ""},
    {"no": "C-002", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 10:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "C-001"},
    {"no": "C-003", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 15:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "C-001"},

    # 链 D：引用了不存在的公告 X-999
    {"no": "D-001", "project": "PRJ-D", "type": "更正公告",
     "publish": "2026-10-23 09:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "X-999"},

    # 链 E：成环 E-001 -> E-002 -> E-001
    {"no": "E-001", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 09:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "E-002"},
    {"no": "E-002", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 10:00:00", "deadline": "2026-11-25 09:30:00",
     "ref": "E-001"},

    # 链 F：更正公告没有引用编号
    {"no": "F-001", "project": "PRJ-F", "type": "更正公告",
     "publish": "2026-10-25 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},

    # 链 G：同一项目两个独立首次公告
    {"no": "G-001", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-26 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},
    {"no": "G-101", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-27 09:00:00", "deadline": "2026-11-27 09:30:00",
     "ref": ""},
    {"no": "G-102", "project": "PRJ-G", "type": "更正公告",
     "publish": "2026-10-29 09:00:00", "deadline": "2026-11-28 09:30:00",
     "ref": "G-101"},

    # H：详情页故障开关目标（默认正常）
    {"no": "H-001", "project": "PRJ-H", "type": "首次公告",
     "publish": "2026-10-30 09:00:00", "deadline": "2026-11-30 09:30:00",
     "ref": ""},

    # I：同址内容更新目标。URL 始终是 /notice/I-001，
    # 截止时间等字段随 /content-toggle 切换版本（模拟更正后重新发布）
    {"no": "I-001", "project": "PRJ-I", "type": "首次公告",
     "publish": "2026-11-02 09:00:00",
     "deadline": "2026-12-05 09:30:00", "ref": "",
     "deadline_v2": "2026-12-12 15:00:00"},
]

NOTICE_BY_NO = {n["no"]: n for n in NOTICES}

# 运行期开关（内存态，重启容器即恢复默认）
FAIL_ENABLED = {"H-001": False}
# I-001 当前内容版本：1=原始截止时间，2=同址更新后的截止时间
CONTENT_VERSION = {"I-001": 1}

PAGE_CSS = """
body { font-family: sans-serif; margin: 2em; color: #222; }
h1 { font-size: 1.4em; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 6px 10px; text-align: left; }
th { background: #f0f3f7; }
a { color: #1a56c4; }
.meta { color: #666; margin-bottom: 1em; }
"""


def render_list() -> str:
    rows = "\n".join(
        '<tr><td><a href="/notice/{no}">{no}</a></td>'
        '<td>{project}</td><td>{type}</td><td>{publish}</td></tr>'.format(
            no=escape(n["no"]), project=escape(n["project"]),
            type=escape(n["type"]), publish=escape(n["publish"]),
        )
        for n in NOTICES
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公共采购公告列表</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公共采购公告列表（示例站）</h1>
<p class="meta">共 {len(NOTICES)} 条公告 · 仅用于演示采集 API</p>
<table class="announcement-list">
<thead><tr><th>公告编号</th><th>项目编号</th><th>类型</th><th>发布时间</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""


def _row(label: str, value: str) -> str:
    return (
        f"<tr><th>{escape(label)}</th>"
        f"<td>{escape(value) if value else '—'}</td></tr>"
    )


def _effective_notice(no: str) -> dict:
    """返回公告当前应展示的字段（应用同址内容版本覆盖）。"""
    n = dict(NOTICE_BY_NO[no])
    if no == "I-001" and CONTENT_VERSION["I-001"] == 2:
        n["deadline"] = n["deadline_v2"]
    return n


def render_detail(no: str) -> str:
    n = _effective_notice(no)
    rows = "".join([
        _row("公告编号", n["no"]),
        _row("项目编号", n["project"]),
        _row("公告类型", n["type"]),
        _row("发布时间", n["publish"]),
        _row("投标截止时间", n["deadline"]),
        _row("引用编号", n["ref"]),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公告 {escape(n['no'])}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公告详情 {escape(n['no'])}</h1>
<p><a href="/announcements">← 返回列表</a></p>
<table class="detail-table"><tbody>{rows}</tbody></table>
</body></html>"""


def _validators(body: str) -> tuple[str, str]:
    """由正文派生强 ETag；Last-Modified 取进程内固定基准时间 + ETag 秒偏移。

    真实站点一般由文件/记录的修改时间产生；示例站用 ETag 派生一个
    可复现的 HTTP 日期即可，重点是内容变化时两个校验值都变化。
    """
    etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] + '"'
    # 用 ETag 哈希确定一个 2026 年内的“修改时间”，保证同内容时间稳定、
    # 不同内容时间不同。
    offset = int(hashlib.sha1(body.encode("utf-8")).hexdigest()[:8], 16)
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_modified = (base + timedelta(seconds=offset % (300 * 86400))
                     ).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return etag, last_modified


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_cacheable(self, body: str) -> None:
        """发送带 ETag/Last-Modified 的页面，命中条件请求时返回 304。"""
        etag, last_modified = _validators(body)
        inm = self.headers.get("If-None-Match")
        ims = self.headers.get("If-Modified-Since")

        etag_match = bool(inm) and inm.strip() == etag
        ims_match = False
        if ims:
            try:
                ims_dt = parsedate_to_datetime(ims)
                lm_dt = parsedate_to_datetime(last_modified)
                ims_match = bool(ims_dt and lm_dt and lm_dt <= ims_dt)
            except (TypeError, ValueError):
                ims_match = False

        # RFC 7232：同时存在两个条件头时以 If-None-Match 为准
        if (inm and etag_match) or (not inm and ims and ims_match):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            return

        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/announcements"):
            self._send_cacheable(render_list())
            return

        if path == "/fail-toggle":
            enabled = (query.get("enabled", ["0"])[0] in {"1", "true", "yes"})
            FAIL_ENABLED["H-001"] = enabled
            self._send(
                f'{{"H-001_fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/fail-status":
            enabled = FAIL_ENABLED["H-001"]
            self._send(
                f'{{"H-001_fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/content-toggle":
            version = query.get("version", ["1"])[0]
            CONTENT_VERSION["I-001"] = 2 if version in {"2", "v2"} else 1
            self._send(
                f'{{"I-001_version": {CONTENT_VERSION["I-001"]}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/content-status":
            self._send(
                f'{{"I-001_version": {CONTENT_VERSION["I-001"]}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path.startswith("/notice/"):
            no = path.rsplit("/", 1)[-1]
            notice = NOTICE_BY_NO.get(no)
            if notice is None:
                self._send("<h1>404 公告不存在</h1>", status=404)
                return
            if FAIL_ENABLED.get(no):
                self._send("<h1>500 服务器内部错误（模拟故障）</h1>", status=500)
                return
            self._send_cacheable(render_detail(no))
            return

        self._send("<h1>404</h1>", status=404)

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[sample-site] {self.address_string()} {fmt % args}")


def main() -> None:
    port = int(os.getenv("SAMPLE_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[sample-site] 示例公告站监听 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
