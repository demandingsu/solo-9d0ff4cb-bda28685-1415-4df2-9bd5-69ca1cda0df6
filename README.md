# 招投标公告采集 API

面向招投标专员的公共采购公告采集服务：同步指定公告列表页与详情页，抽取
**公告编号、项目编号、公告类型、发布时间、投标截止时间、引用编号**，沿引用链
归并首次公告、更正公告与撤销公告，自动识别缺失引用、引用成环、并行更正等冲突，
并输出每条链的状态、有效截止时间与来源链接。

按页面 URL 缓存成功解析的页面原文及 `ETag` / `Last-Modified`，后续同步发送
条件请求（`If-None-Match` / `If-Modified-Since`），对未变更页面只发轻量
304 请求并复用本地缓存，避免重复下载未变公告。

技术栈：FastAPI + BeautifulSoup4 + SQLite，Docker Compose 同时启动采集 API
与一个内置的示例公告站（支持 304 与同址内容更新场景）。

## 目录结构

```
.
├── app/                    # 采集 API
│   ├── config.py           # 环境变量配置
│   ├── db.py               # SQLite 连接与表结构（含 page_cache）
│   ├── fetcher.py          # HTTP 抓取（超时 + 重试 + 条件请求）
│   ├── parser.py           # 列表页/详情页解析（BeautifulSoup）
│   ├── chains.py           # 引用链归并、冲突识别、有效截止时间计算
│   ├── service.py          # 同步编排、页面缓存与成套事务提交
│   ├── models.py           # 请求/响应模型
│   └── main.py             # FastAPI 路由
├── sample_site/            # 示例公告站（仅 Python 标准库）
│   ├── server.py
│   └── Dockerfile
├── tests/                  # pytest 测试
├── Dockerfile              # API 镜像
├── docker-compose.yml      # api + sample-site 两个服务
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集 API：http://localhost:8000 （交互式文档 /docs）
- 示例公告站：http://localhost:8080/announcements

触发一次同步：

```bash
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements"}'
```

查询引用链与孤立异常公告：

```bash
curl http://localhost:8000/api/v1/chains
```

SQLite 数据文件存放在 Docker 卷 `api-data`（容器内 `/data/procurement.db`），
容器重建后数据不丢失。

## 本地开发（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 终端 1：示例公告站
SAMPLE_PORT=8080 python -I sample_site/server.py

# 终端 2：API
DB_PATH=./data/procurement.db uvicorn app.main:app --reload --port 8000

# 测试
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/sync` | 同步指定列表页及其全部详情页，请求体 `{"list_url": "..."}` |
| GET | `/api/v1/chains` | 返回全部引用链 `chains` 与孤立异常公告 `orphans` |
| GET | `/api/v1/announcements/{notice_no}` | 查询单条公告（含抓取错误、链状态） |
| GET | `/api/v1/sync-runs?limit=20` | 同步执行记录（含 `not_modified` 304 复用计数） |
| GET | `/api/v1/page-cache` | 查看各 URL 的缓存条目与 ETag/Last-Modified（不含正文） |

`/api/v1/sync` 响应在原有字段基础上增加 `not_modified`：本次同步中收到
304 并复用本地缓存的页面数（列表页与详情页合计）。

`/api/v1/chains` 响应示例（节选）：

```json
{
  "chains": [
    {
      "chain_id": "A-001",
      "project_no": "PRJ-A",
      "status": "ok",
      "effective_deadline": "2026-11-25 15:00:00",
      "announcements": [
        {
          "notice_no": "A-001",
          "notice_type": "first",
          "deadline": "2026-11-20 09:30:00",
          "ref_notice_no": null,
          "source_url": "http://sample-site:8080/notice/A-001",
          "conflict_reasons": [],
          "effective_deadline": "2026-11-25 15:00:00"
        }
      ]
    }
  ],
  "orphans": [
    {
      "notice_no": "D-001",
      "status": "orphan",
      "conflict_reasons": ["missing_reference"],
      "ref_notice_no": "X-999",
      "effective_deadline": null,
      "source_url": "http://sample-site:8080/notice/D-001"
    }
  ]
}
```

## 业务规则

### 链归并

- 公告类型映射：含「首次」→ `first`，含「更正」→ `correction`，
  含「撤销」→ `revocation`。
- 更正/撤销通过「引用编号」指向上一条公告；沿引用反向追溯，链标识取链上
  首次公告的公告编号。
- **同一项目编号下的不同首次公告保持独立**，不做合并（示例中 PRJ-G 有两条链）。
- 链状态：
  - `ok`：结构正常，返回有效截止时间；
  - `cancelled`：链上存在撤销公告，整条链失效，有效截止时间为 `null`；
  - `conflict`：链上存在冲突，**不推测截止时间**，有效截止时间为 `null`。
- 有效截止时间：取链上明确给出的最新截止时间；更正未给出截止时间时沿用链上
  此前的截止时间（继承已有数据，而非推测）。
- 撤销公告之后又挂接的公告不再属于该链，作为孤立异常公告处理
  （原因 `entry_after_revocation`）。

### 冲突识别（冲突即不推测截止时间）

| 冲突原因 | 触发条件 |
| --- | --- |
| `missing_reference` | 引用编号在库中不存在；或更正/撤销公告未填引用编号 |
| `reference_cycle` | 引用关系成环（含自环） |
| `parallel_correction` | 同一公告被两个及以上公告引用（并行更正） |
| `first_notice_with_reference` | 首次公告却携带引用编号（结构异常） |

无法归入任何链的公告进入 `orphans`，每条都带状态、冲突原因、来源链接，
且有效截止时间为 `null`。

### 条件请求与页面缓存

- 按页面 URL 在 `page_cache` 表缓存**成功解析**的列表页/详情页原文，以及最近一次
  200 响应的 `ETag`、`Last-Modified`；下次同步对该 URL 携带
  `If-None-Match` / `If-Modified-Since` 发送条件请求。
- 源站返回 **304** 时不下载正文，仅复用该 URL 的有效缓存并刷新 `last_used_at`；
  若 304 到达时本地没有可复用缓存（或缓存正文不可用），自动退回**无条件重取**，
  无条件重取仍失败则该 URL 报告失败。
- **列表页返回 304 仍会逐条检查其中的详情页**：详情 URL 不变但内容更新
  （同址更新）时，详情的条件请求会拿到 200 与新校验值，从而被正常发现并更新。
- 新的 **200 页面只有解析成功后才能替换缓存**；解析失败保留旧缓存与旧公告，
  同步标记 `partial`。
- 缓存写入、公告更新与链归并在**同一事务内成套提交**：链归并或任何未预期错误
  都会整体回滚，旧缓存与旧链完整保留。

### 详情抓取失败的容错

- 列表页抓取失败：本次同步标记 `failed`，事务回滚，不动库内现有数据与缓存。
- 单条详情页抓取/解析失败：
  - 此前成功过 → **保留上次完整结果与旧缓存**，仅记录 `last_fetch_error` 与
    同步时间，同步状态为 `partial`，响应中 `kept_last` 计数；
  - 从未成功 → 写入占位记录并归入孤立异常，避免静默丢失。
- 每次同步后基于库内现有数据全量重算链状态。

## 示例公告站内置场景

| 编号 | 场景 | 预期结果 |
| --- | --- | --- |
| A-001 ~ A-003 | 首次 + 两条更正（中间一条无截止时间） | `ok`，有效截止取最新，中间项继承 |
| B-001 ~ B-003 | 首次 + 更正 + 撤销 | `cancelled`，有效截止为 null |
| C-001 ~ C-003 | 两条更正并行引用同一首次公告 | `conflict`（parallel_correction） |
| D-001 | 引用不存在的 X-999 | 孤立，missing_reference |
| E-001 / E-002 | 互相引用成环 | 孤立，reference_cycle |
| F-001 | 更正公告无引用编号 | 孤立，missing_reference |
| G-001 / G-101（+G-102） | 同项目两个独立首次公告 | 两条独立链 |
| H-001 | 可切换 500 故障 | 验证失败保留上次结果与旧缓存 |
| I-001 | URL 不变、内容可切换版本（同址更新） | 列表 304 时仍能发现详情更新并替换缓存 |

示例站所有页面均携带 `ETag` 与 `Last-Modified` 并响应条件请求，校验值命中时
返回 304（无正文），内容变化后校验值随之变化并返回 200。

故障/内容开关（仅示例站）：

```bash
curl "http://localhost:8080/fail-toggle?enabled=1"      # 开启 H-001 详情 500
curl "http://localhost:8080/fail-toggle?enabled=0"      # 恢复
curl "http://localhost:8080/fail-status"
curl "http://localhost:8080/content-toggle?version=2"   # I-001 切换到同址更新版本
curl "http://localhost:8080/content-toggle?version=1"   # 恢复原始版本
curl "http://localhost:8080/content-status"
```

### 条件请求演示

```bash
# 首次同步：全部 200，建立缓存（not_modified=0）
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements"}'

# 再次同步：页面未变，列表 + 18 个详情全部 304 复用（not_modified=19）
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements"}'

# 查看缓存条目与校验值
curl http://localhost:8000/api/v1/page-cache
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/procurement.db` | SQLite 文件路径 |
| `FETCH_TIMEOUT` | `10` | 单次 HTTP 超时秒数 |
| `FETCH_RETRIES` | `2` | 网络错误/5xx 的重试次数（4xx 不重试） |
| `RETRY_BACKOFF` | `0.5` | 重试退避基数秒 |
| `KEEP_LAST_ON_DETAIL_FAILURE` | `true` | 详情失败时保留上次完整结果 |
| `SAMPLE_PORT`（仅示例站） | `8080` | 示例站监听端口 |
