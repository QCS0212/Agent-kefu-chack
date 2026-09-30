# 部署说明

本合同适用于把两个服务放到真实环境（本机 / 内网 / 容器）使用。

## 1. 部署形态

| 形态 | 适用 | 命令 |
|---|---|---|
| 本机开发 | 单人联调 | `pwsh -File scripts\start_all.ps1` 或 `bash scripts/start_all.sh` |
| 容器（推荐） | 内网服务化 | `cp .env.example .env`（填 Key）→ `docker compose up --build -d` |
| 裸机 systemd | 无容器环境 | 见下方示例单元 |

两个服务：批量检测 API（8000）与监测平台（8010）。监测平台通过 `MONITOR_UPSTREAM_URL` 调用批量检测 API。

## 2. 必需环境变量

最小可用（生产）：

```bash
# 必须：接入密钥（多条用分号分隔）
API_KEYS=sk-cs-xxxx|customer-service|detect,read|2000|120;sk-ops-xxxx|customer-service|detect,read,admin|0|600
MONITOR_API_KEYS=sk-cc-xxxx|customer-service|ingest,read|0|600;sk-ops-xxxx|ops|ingest,read,admin|0|600
MONITOR_PANEL_TOKEN=<32 位以上随机串>

# 真实模型（mode=llm 时必需）
LLM_API_KEY=<只放本地 .env 或密钥管理服务>
LLM_BASE_URL=https://<你的网关>
LLM_MODEL=<模型名>

# 绑定（容器里必须是 0.0.0.0）
API_BIND_HOST=0.0.0.0
MONITOR_BIND_HOST=0.0.0.0
MONITOR_UPSTREAM_URL=http://api:8000
```

要点：

- **绑非本机地址必须有 Key**：批量检测服务启动时会校验，缺失直接拒绝启动；监测平台接入接口返回 503。
- 读接口在非本机绑定时强制要求 `MONITOR_PANEL_TOKEN`。
- 令牌只放 `.env` 或环境变量/密钥管理服务，不要写进代码、镜像、日志或截图。

## 3. 容器

```bash
cp .env.example .env      # 填上上面的变量
docker compose up --build -d
docker compose ps         # healthy 才算启动成功
curl -s http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8010/readyz
```

镜像特性：非 root 运行（`appuser`）、内置 `HEALTHCHECK`、依赖固定版本。
`docker-compose.yml` 中两个服务都带健康检查、`restart: unless-stopped` 与 2C/2G 资源上限，可按需调整。

## 4. systemd 示例

```ini
# /etc/systemd/system/kefu-api.service
[Unit]
Description=Kefu hallucination check API
After=network.target

[Service]
WorkingDirectory=/opt/kefu-check
EnvironmentFile=/opt/kefu-check/.env
ExecStart=/opt/kefu-check/.venv/bin/python -B -m uvicorn app.api:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5
User=kefu

[Install]
WantedBy=multi-user.target
```

监测平台把 `ExecStart` 换成 `.venv/bin/python -B -m monitor --host 0.0.0.0 --port 8010` 即可。
注意：**不要**使用 `--workers` 大于 1；也不要开 `--reload` 处理线上任务。

## 5. 网关与安全

- 服务本身不做 TLS。放在 Nginx / Ingress / API 网关之后，由网关终止 TLS 与 mTLS。
- 网关应透传 `Authorization`、`X-Api-Key`、`X-Monitor-Token` 与 `X-Request-ID`。
- 面板（前端）与 API 不同源时，把面板地址加入 `API_CORS_ORIGINS` 与 `MONITOR_CORS_ORIGINS`。
- 只在网关或服务层暴露必要路径：监测平台的 `/metrics`、`/readyz` 建议仅内网可达。
- 建议对 `/api/ingest/events` 单独限流（`MONITOR_RATE_LIMIT` + 网关限流），并按租户分配 Key。

## 6. 容量与并发

| 项 | 默认 | 说明 |
|---|---|---|
| `API_MAX_BATCH` | 20 | 单批样本上限，也是监测平台批量转发的分片大小 |
| `API_MAX_RUNNING` | 2 | 并行任务数（= 上游模型并发数） |
| `API_MAX_QUEUED` | 8 | 排队 + 运行的任务上限，超出返回 429 |
| `MONITOR_WORKER_BATCH` | 20 | 单轮领取事件数，一轮一次上游批量调用 |
| LLM 超时/重试 | 120s / 最多 2 次 | 见 `app/llm.py` |

吞吐 ≈ `API_MAX_RUNNING × (60 / 单条模型耗时) × 批量条数`。经验值：单条 10s 时，2 并发 × 20 条/批 ≈
约 240 条/分钟（mock 模式更快）。要再提高，优先调大 `API_MAX_RUNNING` 与模型配额，或改用消息队列。

磁盘：任务产物在 `outputs/api/<task_id>/`（含 raw 响应与 HTML 报告），监测数据在 `data/monitor.db`。
`MONITOR_RETENTION_DAYS` 默认 30 天；`outputs/` 目前需自行清理（建议 cron 删除 30 天前目录）。

## 7. 升级与回滚

1. 升级前备份：`data/monitor.db`、`outputs/api/`、`.env`。
2. 拉取新版本后重建镜像：`docker compose build && docker compose up -d`。
3. 数据库结构变更由 `monitor/store.py::_ensure_columns` 自动补齐（`ALTER TABLE ADD COLUMN`），不需要手工迁移。
4. 回滚：切回上一个镜像 tag；新增列对旧版本无副作用。
5. 真实模型相关变更（模型名、提示词）必须换新的 `outputs/` 运行目录，保留首次结果。

## 8. 前端（可选）

```bash
cd frontend && npm install && npm run dev     # 开发
npm run build                                 # 产物在 frontend/dist，可交给 Nginx 托管
```

Vite 开发服务器把 `/api`、`/health` 代理到 8000，把 `/api/monitor`、`/api/ingest` 代理到 8010。
生产环境建议由 Nginx 按同样规则反向代理；「线上监测」页签需要在页面里填写面板令牌。