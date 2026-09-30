# 监测平台说明

`monitor/` 在上游批量检测服务（`app/api.py`，默认 `127.0.0.1:8000`）之上提供持续监测：外部系统推送客服会话事件，平台装配知识、抽样、批量检测、落库、告警、暴露统计，并支持人工复核闭环。
开发约束与数据隔离规则以 [AGENTS.md](../AGENTS.md) 为准，本文件只说明运行方式、接口、口径和边界。

## 启动

```bash
# 方式一：一键启动两个服务（Windows）
pwsh -File scripts\start_all.ps1
pwsh -File scripts\stop_all.ps1

# 方式一：一键启动两个服务（macOS / Linux）
bash scripts/start_all.sh
bash scripts/stop_all.sh

# 方式二：docker compose（容器内绑 0.0.0.0，必须先设置 MONITOR_PANEL_TOKEN）
cp .env.example .env && docker compose up --build

# 方式三：手工分别启动
.venv/bin/python -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
.venv/bin/python -B -m monitor --port 8010          # 只起 API：加 --no-worker
```

- 服务地址：`http://127.0.0.1:8010`，在线文档：`http://127.0.0.1:8010/docs`
- 配置优先级：环境变量 → 仓库根目录 `.env` → 内置默认值。全部键见 `../.env.example`。
- 默认只绑 `127.0.0.1`。绑到其他地址时读接口强制要求 `MONITOR_PANEL_TOKEN`，否则返回 503。

## 鉴权与安全边界

| 接口组 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/ingest/events` | 必须（scope=ingest） | `Authorization: Bearer <token>` 或 `X-Monitor-Token`，常量时间比较 |
| `GET /api/monitor/*`（读） | scope=read | 配了 `MONITOR_PANEL_TOKEN` 或绑非本机地址时强制鉴权；本机默认开放便于联调 |
| `POST .../review`、`DELETE /api/monitor/events/{id}`、`POST /api/monitor/alerts/{id}/replay` | scope=admin | 复核/删除/重放都会改数据，外部暴露前务必配置面板令牌或 admin Key |
| `/metrics`、`/health`、`/readyz` | 无 | 仅运维探测，默认只绑本机 |

Key 配置（`MONITOR_API_KEYS`，多条用分号分隔）：

```
<token>|<tenant>|<scopes>|<daily_quota>|<rate_per_minute>
```

- `tenant` 决定事件归属与可见范围：非 admin 的 Key 只能看到自己租户的事件与统计，跨租户访问返回 404。
- 兼容旧配置：只设置 `MONITOR_API_TOKENS`（纯令牌、逗号分隔）时按 `tenant=default`、全部 scope 处理。
- `MONITOR_REQUIRE_READ_AUTH=1` 可强制本机也校验读接口；非本机绑定自动强制。

- `MONITOR_REQUIRE_AUTH=1` 且未配置任何令牌时，接入接口返回 **503 `MONITOR_NOT_CONFIGURED`**，而不是放行。
- 令牌错误或缺失返回 401。`MONITOR_REQUIRE_AUTH=0` 仅用于本机调试。
- 限流：`MONITOR_RATE_LIMIT`（每分钟每调用方请求数，0=不限）；超限返回 429 并带 `Retry-After: 60`。
- 单次接入上限：`MONITOR_MAX_BATCH_ITEMS`（默认 200），超限返回 413，避免一条请求压垮存储。
- 读接口 `limit` 会被截断到 1–200，`days` 截断到 1–365。

## 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/ingest/events` | 接入单条、数组或 `{events: [...]}`，返回 202、事件 ID、抽样与幂等状态 |
| GET | `/api/monitor/events` | 事件流，支持 `limit`、`offset`、`status_filter`、`verdict`、`category`、`needs_review`、`review_status` |
| GET | `/api/monitor/events/{event_id}` | 事件详情与检测结果 |
| POST | `/api/monitor/events/{event_id}/review` | 人工复核：`confirmed`（确认幻觉）/ `rejected`（误报），可带 `note` |
| DELETE | `/api/monitor/events/{event_id}` | 删除事件及其检测、告警（个人信息删除请求） |
| GET | `/api/monitor/stats` | 总体统计（口径见下） |
| GET | `/api/monitor/timeseries` | 按天趋势，默认 7 天，最多 90 天 |
| GET | `/api/monitor/knowledge-gaps` | 知识缺口榜（未命中知识且非「正常」的类目） |
| GET | `/api/monitor/config` | 脱敏后的运行配置与调用方配额余量 |
| GET | `/api/monitor/alerts` | 告警投递记录（含 attempts / next_attempt_at / last_error） |
| POST | `/api/monitor/alerts/{alert_id}/replay` | 重放失败的告警（admin） |
| GET | `/metrics` | Prometheus 文本格式指标 |
| GET | `/health` | 健康检查，含待处理队列与上游探活 |
| GET | `/readyz` | 就绪检查：存储 + 接入鉴权配置 + 上游探活，未就绪返回 503 |

Postman/Apifox 可直接导入 `postman_monitor.json`（含接入、事件流、统计、复核、删除、指标共 12 个请求，集合变量里填令牌）。

### 接入请求

```json
{
  "session_id": "s1",
  "user_question": "退款多久到账？",
  "system_reply": "已经帮您退款，1-3 个工作日到账。",
  "category": "after_sale",
  "sku": "SKU1001",
  "store_id": "S001",
  "occurred_at": "2026-09-30T10:00:00+08:00",
  "knowledge_snippet": "可选：外部系统自带的核验依据，优先于本地知识源",
  "metadata": {}
}
```

- 必填：`session_id`、`user_question`、`system_reply`（后两者上限 20,000 字符）；未知字段直接拒绝。
- 批量接入时单条非法只计入 `rejected`，不影响其他条目；`duplicate=true` 表示命中去重键并返回既有事件。
- 事件归属的 `tenant` 由调用方 Key 决定，请求体不能指定（`extra="forbid"`），事件详情里会回显。

## 多租户与审计

- 事件、统计、趋势、知识缺口、事件流都按 Key 的 `tenant` 过滤；admin scope 或不带租户限制的面板令牌可见全部。
- 复核会记录 `reviewer`（Key 标识）、`review_note`、`reviewed_at`，审计字段随事件详情返回。
- 接入、复核、删除都会写结构化日志（含 `request_id`、`tenant`、`key_id`），便于事后追溯。

## 处理流程与判定边界

```text
接入事件 → 幂等去重 → 抽样 → 装配知识 → 规则快检 → 上游批量检测 → 落库 → 告警 → 人工复核
```

1. **幂等**：去重键 = `session_id + user_question + system_reply`，重复接入返回既有 `event_id`，不重复调用模型。
2. **抽样**：`MONITOR_ALWAYS_SAMPLE` 内的高风险类目全量，其余按 `MONITOR_SAMPLE_RATE` 哈希抽样。未抽中为 `skipped`，不计入指标分母。
3. **知识装配**：外部 `knowledge_snippet` 优先；否则按 `category`、`sku`、`store_id` 从 `knowledge/` 组装，并记录知识版本与命中项。
4. **规则快检**（默认开启，不消耗模型调用）：
   - 回复声称「已退款 / 已改地址 / 已升级」等执行类操作：
     - 知识依据**明确说明不具备该能力**（未接入、无法查询或修改等）→ 判 `hallucination` + `capability_overreach`（high），直接告警；
     - 否则 → 记 `not_verifiable` + `needs_review=true`，因为规则无法证明操作没有发生，必须与工单/订单系统核对。**不得默认判成幻觉。**
   - 回复出现知识库外的专属联系人或仓库（如「张经理」「杭州仓」）→ 判 `business_fabrication`，并标 `needs_review=true`（启发式信号）。
   - 规则只做证伪与线索提示，未命中不代表正常。
5. **上游批量检测**：同一批最多 20 条合并成一次任务提交并轮询，减少往返、提高吞吐；单条失败只影响该条，不牵连同批其他事件。
6. **失败重试与降级**：上游网络错误、429、503、超时或返回结构不合法时，按 `MONITOR_RETRY_BACKOFF_SECONDS × 2^n` 退避重试，最多 `MONITOR_RETRY_MAX_ATTEMPTS` 次；重试耗尽才写 `not_verifiable` + `detector=fallback` + `needs_review=true`，并在 `reason` 保留错误码。**不伪造「幻觉」或「正常」**。
7. **告警**：high 立即推 webhook（`X-Monitor-Signature: sha256=<HMAC>`），medium 聚合成日报，low 只入库；支持去重窗口与夜间静默。投递失败只记录，不影响检测结果。
8. **人工复核**：`POST .../review` 记录 `confirmed` / `rejected`，`review_status`、`review_note`、`reviewed_at` 落库，用于计算复核后精确率。

## 指标口径（重要）

| 指标 | 定义 |
|---|---|
| `hallucination_rate` | 幻觉数 ÷ **明确结论数**（`hallucination + no_hallucination`）。不可核验不进分母，避免上游故障时幻觉率被稀释 |
| `coverage` | 明确结论数 ÷ 已检测数，反映结论可用比例 |
| `degraded_rate` | 不可核验数 ÷ 已检测数，反映降级/待核验比例 |
| `detections.fallback` | 由上游失败降级产生的条数 |
| `detections.needs_review` | 标记为待人工复核的条数 |
| `review.precision_after_review` | 已复核样本中的确认数 ÷ (确认 + 误报) |
| `review.pending` | 待复核条数（含规则快检与降级结论） |

Prometheus 暴露：`monitor_events_total`、`monitor_sampled_total`、`monitor_detections_total`、`monitor_hallucination_rate`、`monitor_coverage`、`monitor_degraded_rate`、`monitor_severity_total`、`monitor_type_total`、`monitor_detection_latency_ms`、`monitor_alerts_total`、`monitor_review_total`、`monitor_fallback_total`、`monitor_retry_total`、`monitor_unresolved_total`、`monitor_ingest_requests_total`、`monitor_upstream_calls_total`。

**告警建议**：对 `monitor_coverage` 下降、`monitor_degraded_rate` 上升、`monitor_unresolved_total` 增长配置告警；不要只看幻觉率。

## 知识源：本地目录 / 商城只读接口

- `MONITOR_KNOWLEDGE_SOURCE=file`（默认）：只用本地 `knowledge/` 目录。
- `MONITOR_KNOWLEDGE_SOURCE=shop`：按 `category/sku/store_id` 调用商城只读知识接口。
- `MONITOR_KNOWLEDGE_SOURCE=mysql`：直连商城只读库（`zhixuanpiao_program_*/order_*/pay_*`），
  传订单号可同时取到订单状态、支付账单、退款到账记录与节目退改政策；连接必须用 utf8mb4。
- 两种方式都按 `MONITOR_SHOP_FALLBACK` 回退本地目录（回退会明确标注原因，不会把"取不到依据"当成幻觉）。
- 完整契约、智选票（zhixuanpiao）表映射与商城侧 Java 骨架见 [SHOP_INTEGRATION.md](SHOP_INTEGRATION.md)。

## 脱敏与数据边界

- 转发前对手机号、座机、身份证号、银行卡号、邮箱、订单号执行脱敏（`monitor/knowledge.py::redact`）。
- 只发送当前事件的必要字段，不发送人工标签、其他事件或整个工作目录。
- `knowledge/` 是知识源，不得存放人工标签或答案；人工标签只能通过 `app` 的独立评估入口使用。
- 保留期：`MONITOR_RETENTION_DAYS`（默认 30 天，0=永久），工作循环每 30 分钟清理一次超期会话与告警；`DELETE /api/monitor/events/{id}` 用于单个删除请求。
- `data/monitor.db`、`outputs/`、`.run/`、`out-logs/` 均为本地运行产物，默认被 Git 忽略。

## 已知限制

- 单进程单 worker：不支持多 worker 或分布式消费，数据库是本地 SQLite 文件；多租户是逻辑隔离（同库，按 tenant 过滤）。
- 批量转发已把吞吐提升到「每批一次上游任务」，但整体仍受上游 2 个并发任务与模型时延约束；更大流量需要提高上游并发或改成消息队列。
- 规则快检是基于模式与名单的启发式：对非常规表述可能漏报，对通用职务称谓可能误报；命中结论一律进入复核队列。
- 上游为 mock 模式时，监测结果只验证工程链路，不代表真实模型检出能力。