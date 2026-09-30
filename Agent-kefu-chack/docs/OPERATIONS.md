# 运维手册

面向日常值班与故障处理。部署方式见 [部署说明](DEPLOYMENT.md)，接口细节见 [API](API.md) 与 [监测平台](MONITOR.md)。

## 1. 日常巡检（建议每天）

```bash
curl -s http://127.0.0.1:8000/readyz      # 批量检测服务
curl -s http://127.0.0.1:8010/readyz      # 监测平台（含上游探活）
curl -s http://127.0.0.1:8010/api/monitor/stats?days=1 -H "X-Monitor-Token: $PANEL_TOKEN" | jq .
```

关注四项，缺一项都不能只看幻觉率：

| 指标 | 含义 | 健康参考 |
|---|---|---|
| `hallucination_rate` | 幻觉数 ÷ 明确结论数 | 业务基线上下浮动；突然归零通常意味着没有明确结论 |
| `coverage` | 明确结论 ÷ 已检测 | ≥ 90%；下降说明上游或知识库出问题 |
| `degraded_rate` | 不可核验 ÷ 已检测 | ≤ 10%；上升说明上游不稳定或知识缺失 |
| `review.pending` | 待人工复核条数 | 值班当日清空；积压会导致口径失真 |

## 2. 人工复核流程

1. 打开面板「线上监测」，或 `GET /api/monitor/events?needs_review=true&review_status=pending`。
2. 对每条结论核对原文与知识依据；必要时查询工单/订单系统。
3. `POST /api/monitor/events/{event_id}/review`，`status` 取 `confirmed`（确认幻觉）或 `rejected`（误报），
   在 `note` 写依据。服务端会记录 `reviewer`（Key 标识）与时间，用于审计。
4. 复核完成后 `stats.review.precision_after_review` 才是对外可用的准确率口径。

规则：规则快检与降级（`detector=fallback`）的结论一律要复核；不要为了好看把误报改判为正常。

## 3. 告警与重投

- high 立刻推 webhook（带 `X-Monitor-Signature: sha256=<HMAC>`，用 `MONITOR_ALERT_SECRET` 校验），medium 汇总日报。
- 投递失败会退避重投（15s 起，最多 5 次），记录在 `alerts` 表：

```bash
curl -s http://127.0.0.1:8010/api/monitor/alerts?status_filter=failed -H "X-Monitor-Token: $PANEL_TOKEN" | jq .
curl -X POST http://127.0.0.1:8010/api/monitor/alerts/<alert_id>/replay -H "X-Monitor-Token: $PANEL_TOKEN"
```

- 接收端务必校验签名并做幂等（`event_id + severity` 去重），避免重投造成重复工单。

## 4. 故障处理

| 现象 | 排查 | 处置 |
|---|---|---|
| 监测平台 `/readyz` 返回 503，`checks.upstream=unreachable` | 上游 `curl http://127.0.0.1:8000/readyz` | 先恢复上游；期间事件会退避重试，重试耗尽落 `not_verifiable` 并进复核队列 |
| `/readyz` 报 `ingest_auth` | `.env` 未配置 `MONITOR_API_KEYS` / `MONITOR_API_TOKENS` | 配置令牌后重启；不要用 `MONITOR_REQUIRE_AUTH=0` 对外 |
| 接入返回 503 `AUTH_NOT_CONFIGURED` | 同上 | 同上 |
| 提交检测返回 429 `QUOTA_EXCEEDED` | `GET /api/service/config` 看该 Key 用量 | 提高 `daily_quota` 或次日重试；排查调用方是否有重复提交（建议启用 Idempotency-Key） |
| 提交检测返回 429 队列已满 | `api_tasks{status="queued"}` | 调大 `API_MAX_QUEUED`/`API_MAX_RUNNING`，或降低上游耗时 |
| 任务一直 `running` | 看 `outputs/api/<task_id>/raw/` 是否有新文件 | 模型侧慢；必要时 `POST /api/checks/{id}/cancel`，再用新目录重跑 |
| 任务 `interrupted` | 服务重启导致 | 该设计避免重复计费，重新提交即可（可复用 Idempotency-Key 以外的新键） |
| `monitor_coverage` 下降 | 上游批量失败、知识库缺条目 | 看 `/api/monitor/knowledge-gaps`，补齐 `knowledge/` 或修上游 |
| 磁盘增长过快 | `du -sh outputs data` | 清理超期 `outputs/api/*`；确认 `MONITOR_RETENTION_DAYS` 生效 |

## 5. 备份与恢复

```bash
# 备份（SQLite 用在线备份，避免拷贝到半写状态）
sqlite3 data/monitor.db ".backup 'backup/monitor-$(date +%F).db'"
tar czf backup/outputs-$(date +%F).tgz outputs/api .env

# 恢复
docker compose down
cp backup/monitor-2026-09-30.db data/monitor.db
docker compose up -d
```

- `.env` 属于敏感配置，备份文件要加密存储，不要进 Git。
- `outputs/api/` 保存真实模型原始响应，属于审计证据，建议保留 30–90 天。

## 6. 保留期与删除请求

- `MONITOR_RETENTION_DAYS`（默认 30）控制会话数据保留；工作循环每 30 分钟清理一次。
- 单条删除（个人信息删除请求）：`DELETE /api/monitor/events/{event_id}`（需要 admin scope）。
- 删除任务产物：`DELETE /api/checks/{task_id}`（需要 admin scope），会移除该任务的 `task.json`、`raw/`、报告与评估。
- 删除不可恢复；操作会写 WARNING 日志（含 key_id），便于审计。

## 7. 发布与回滚

1. `make test`（88 项离线测试）通过后再发布。
2. 镜像打 tag（`kefu-check:v1.2.3`），先灰度一台，观察 `/readyz`、5xx 比例与 `coverage`。
3. 回滚即切回上一个 tag；数据库新增列对旧版本兼容。
4. 涉及提示词/模型变更：新目录运行，保留 `outputs/real-first` 首次结果，禁止覆盖。

## 8. 日志

单行 JSON，字段：`ts`、`level`、`service`、`logger`、`request_id`、`message`，访问日志额外带
`method`、`path`、`status`、`duration_ms`、`tenant`、`key_id`。

```bash
# 找某次调用的全部日志
docker compose logs api | grep '"request_id":"<X-Request-ID>"'
# 统计被拒的请求
docker compose logs monitor | grep -c 'MISSING_TOKEN'
```

本地调试可设 `LOG_JSON=0` 输出人类可读格式；日志采集侧建议保持 JSON。