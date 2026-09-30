# 批量检测 API 调用说明（企业接入版）

服务：`app/api.py`，默认 `127.0.0.1:8000`。适合被客服系统、质检平台、数据管道直接调用。
在线文档：`http://<host>:8000/docs`；静态规范：[openapi.json](openapi.json)。集合：[postman_collection.json](postman_collection.json)。

## 1. 启动

```bash
# 本机开发（默认匿名可用）
.venv/bin/python -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1

# 企业形态：绑到网络地址必须配置 API_KEYS，否则服务拒绝启动
API_BIND_HOST=0.0.0.0 API_PORT=8000 \
API_KEYS='sk-cs-9f2a|customer-service|detect,read|2000|120;sk-ops|customer-service|detect,read,admin|0|600' \
.venv/bin/python -B -m uvicorn app.api:app --host 0.0.0.0 --port 8000 --workers 1

# 容器
docker compose up --build -d
```

- 只允许 `--workers 1`：任务队列与磁盘锁都假设单进程，多 worker 会互相抢锁。
- 真实模型配置（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`）只放服务端 `.env`，调用方不传密钥。

## 2. 鉴权与配额

请求头二选一：

```
Authorization: Bearer <token>
X-Api-Key: <token>
```

Key 配置格式（`API_KEYS`，多条用分号分隔）：

```
<token>|<tenant>|<scopes>|<daily_quota>|<rate_per_minute>
```

| 字段 | 说明 |
|---|---|
| scopes | `detect`（提交检测）、`read`（查询/评估/报告）、`admin`（删除任务、查看服务配置），`*` 表示全部 |
| daily_quota | 该 Key 每日可提交的样本条数，0=不限；落盘在 `outputs/api/quota.json` |
| rate_per_minute | 该 Key 每分钟请求上限，0=使用 `API_RATE_LIMIT` |

未在 Key 上指定时使用 `API_RATE_LIMIT` 与 `API_DAILY_QUOTA`。`tenant` 决定数据可见范围：
非 admin 的 Key 只能看到自己租户的任务，跨租户访问返回 404（不泄露任务是否存在）。

## 3. 接口清单

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| GET | `/healthz` | 无 | 存活检查 |
| GET | `/health` | 无 | 健康检查（兼容旧路径，含上限信息） |
| GET | `/readyz` | 无 | 就绪检查（存储 + llm 配置），未就绪返回 503 |
| GET | `/metrics` | 无 | Prometheus 指标 |
| GET | `/api/service/config` | admin | 脱敏后的服务配置与调用方配额余量 |
| POST | `/api/checks` | detect | 提交单条或批量检测，返回 202 |
| GET | `/api/checks` | read | 任务列表（按租户过滤，支持 `status_filter`/`limit`/`offset`） |
| GET | `/api/checks/{task_id}` | read | 任务进度、逐条结果与错误 |
| POST | `/api/checks/{task_id}/evaluate` | read | 提交人工标签做独立评估 |
| GET | `/api/checks/{task_id}/report` | read | 返回可离线保存的 HTML 报告 |
| POST | `/api/checks/{task_id}/cancel` | read | 请求取消任务 |
| DELETE | `/api/checks/{task_id}` | admin | 删除任务及其本地产物 |

## 4. 提交检测

```bash
curl -X POST http://127.0.0.1:8000/api/checks \
  -H 'Authorization: Bearer sk-cs-9f2a' \
  -H 'Idempotency-Key: order-20260930-0001' \
  -H 'Content-Type: application/json' \
  -d '{"mode":"mock","items":[{"id":"case-001","user_question":"支持退货吗？","system_reply":"支持七天退货。","knowledge_base":"普通商品支持七天无理由退货。"}]}'
```

```json
{ "task_id": "…", "status": "accepted", "status_url": "/api/checks/…", "report_url": "/api/checks/…/report" }
```

- `mode`：`mock`（默认，验证链路）/ `llm`（调用服务端配置的真实模型）。
- `items`：1 ~ `API_MAX_BATCH`（默认 20）条；ID 必须唯一；三个内容字段各 ≤ 20000 字符。
- **Idempotency-Key**（可选但强烈建议）：同一 Key + 同一内容重复提交会返回首次的 `task_id`
  并带 `idempotent_replay: true`；同一 Key 提交不同内容返回 409 `IDEMPOTENCY_CONFLICT`，避免重复计费。
- 请求头 `X-Request-ID` 可自带，服务会回写并写进日志，便于全链路排查。

## 5. 查询、取消与删除

```bash
curl -H 'Authorization: Bearer sk-cs-9f2a' http://127.0.0.1:8000/api/checks/<task_id>
curl -H 'Authorization: Bearer sk-cs-9f2a' 'http://127.0.0.1:8000/api/checks?status_filter=completed&limit=20'
curl -X POST -H 'Authorization: Bearer sk-cs-9f2a' http://127.0.0.1:8000/api/checks/<task_id>/cancel
curl -X DELETE -H 'Authorization: Bearer sk-ops' http://127.0.0.1:8000/api/checks/<task_id>
```

任务状态：`queued` → `running` → `completed` / `partial_failed` / `failed` / `cancelled` / `interrupted`
（`interrupted` = 服务重启时发现未完成任务，需重新提交）。

`progress.processed = succeeded + failed`。技术失败只进 `errors`，不会伪装成正常结论；
`partial_failed` 表示部分样本失败但其余结果可用。取消运行中的任务会保留已完成结果，不再生成完整报告。

## 6. 独立评估

```bash
curl -X POST http://127.0.0.1:8000/api/checks/<task_id>/evaluate \
  -H 'Authorization: Bearer sk-cs-9f2a' -H 'Content-Type: application/json' \
  -d '{"labels":[{"id":"case-001","is_hallucination":true}]}'
```

标签 ID 必须与任务样本完全一致，值为 JSON 布尔；标签只在本地计算，不会发给模型。
返回 TP/FP/FN/TN、Precision、Recall、F1、Accuracy 与误报/漏检 ID；mock 任务不产生正式指标。

## 7. 错误码

| HTTP | code / 场景 | 处理建议 |
|---|---|---|
| 401 | `MISSING_TOKEN` / `INVALID_TOKEN` | 检查 Key 是否配置、是否放在 `Authorization: Bearer` 或 `X-Api-Key` |
| 403 | `SCOPE_DENIED` / 浏览器来源不允许 | 换用具备该 scope 的 Key；面板联调需在 `API_CORS_ORIGINS` 内 |
| 404 | 任务不存在或不在该租户 | 确认 task_id 与 Key 的 tenant |
| 409 | `IDEMPOTENCY_CONFLICT` / 任务未完成 / 报告未生成 | 幂等键换新，或等任务终态再评估/取报告 |
| 422 | 参数非法、ID 重复、超出单批上限、标签不匹配 | 按返回的 `detail` 修正请求体 |
| 429 | `RATE_LIMITED` / `QUOTA_EXCEEDED` / 队列已满 | 按 `Retry-After` 退避；或申请更高额度 |
| 503 | `AUTH_NOT_CONFIGURED` / 真实模型配置缺失 | 检查服务端 `.env`；`/readyz` 会给出具体原因 |

## 8. 观测

- `/metrics`：`api_requests_total{route,status}`、`api_auth_failures_total{code,path}`、
  `api_items_submitted_total`、`api_tasks{status}`。
- 日志：单行 JSON，含 `request_id`、`method`、`path`、`status`、`duration_ms`、`tenant`、`key_id`。
- 建议：对 5xx 比例、`QUOTA_EXCEEDED`、`RATE_LIMITED` 与 `api_tasks{status="failed"}` 配置告警。

## 9. 限制

- 单进程、单机任务队列；任务产物存本地磁盘（`outputs/api/<task_id>/`）。
- 重启不会自动续跑未完成任务（标记 `interrupted`），这是为了避免重复调用付费模型。
- 不做多租户数据物理隔离（同一数据库、按 tenant 逻辑隔离），高隔离场景请按租户独立部署。