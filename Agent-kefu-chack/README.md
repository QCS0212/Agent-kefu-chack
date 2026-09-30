# 客服回复幻觉检测

用于作业「0110 · 客服回复幻觉检测」的完整工程：一端是离线批量检测与独立评估（`app/`），另一端是在其之上构建的持续监测平台（`monitor/`）。Python 3.12+，HTTP 服务使用 FastAPI / Uvicorn，依赖版本见 `requirements.txt`。

开发约定、数据隔离规则和服务边界以 [AGENTS.md](AGENTS.md) 为准；本文件说明交付内容、运行方式和实测结果。

## 交付范围

| 模块 | 目录 | 作用 |
|---|---|---|
| 批量检测与评估 | `app/`、`main.py` | 逐条核验回复中的事实声明，输出判断、分类、严重程度与证据；独立对照人工标签计算指标 |
| 本机 HTTP 接口 | `app/api.py`、`app/jobs.py` | 提交任务、查询进度与结果、提交标签评估、打开 HTML 报告 |
| 持续监测平台 | `monitor/` | 接入外部会话事件，自动装配知识，抽样后转上游检测，落库、告警并输出 Prometheus 指标 |
| 知识源 | `knowledge/` | 政策、类目、SKU、联系人名单，供监测平台装配核验依据 |
| 前端面板 | `frontend/` | Vue 3 面板：本地结果总览、结果明细、提交与查询任务 |
| 文档 | `docs/` | 接口说明、Postman/OpenAPI 文件、需求对话记录 |
| 企业接入层 | `common/` | API Key、scope、租户、限流、每日配额、结构化日志与请求 ID |
| 运维脚本 | `scripts/`、`Makefile` | 一键启动/停止、常用命令 |
| 容器化与 CI | `Dockerfile`、`docker-compose.yml`、`.github/workflows/` | 非 root 容器 + 健康检查；CI 跑离线单元测试 |
| 调用示例 | `examples/client.py` | 提交检测、轮询、评估、事件接入、复核的完整示例 |

已包含 20 条原始回复和人工标签，每条自带 `knowledge_base`；已实现输入校验、mock、独立评估、JSON 输出、静态 HTML 报告、HTTP 接口、监测链路与单元测试。

## 快速开始

在项目目录使用现有虚拟环境（Windows 为 `.venv\Scripts\python.exe`）：

```bash
.venv/bin/python main.py demo
.venv/bin/python -m unittest discover -s tests -v
```

也可在 PyCharm 直接运行 `main.py`，默认执行 mock 演示，报告输出到 `outputs/mock/report.html`。

一条命令起两个服务（自动复用/创建 `.venv`、按需装依赖、缺 `.env` 时从示例生成）：

```bash
pwsh -File scripts\start_all.ps1     # Windows
bash scripts/start_all.sh            # macOS / Linux
docker compose up --build            # 容器方式（先 cp .env.example .env 并设置令牌）
```

启动后：批量检测 API `http://127.0.0.1:8000/docs`，监测平台 `http://127.0.0.1:8010/docs`，面板 `cd frontend && npm install && npm run dev`。

企业形态（绑到网络地址必须配置 Key，否则服务拒绝启动）：

```bash
cp .env.example .env      # 填 API_KEYS / MONITOR_API_KEYS / MONITOR_PANEL_TOKEN / LLM_*
docker compose up --build -d
docker compose ps         # 两个服务都 healthy 才算成功
API_KEY=<key> MONITOR_KEY=<key> .venv/bin/python -B examples/client.py   # 端到端自检
```

## 企业接入（Key / 租户 / 幂等）

调用方只需要一把 Key，服务端不暴露模型密钥：

```
API_KEYS            = <token>|<tenant>|<scopes>|<daily_quota>|<rate_per_minute>   # 分号分隔多条
MONITOR_API_KEYS    = 同上（scopes 为 ingest/read/admin）
MONITOR_PANEL_TOKEN = 面板/读接口令牌（绑非本机地址时必需）
```

```bash
# 提交检测（带幂等键，重复提交不会重复计费）
curl -X POST http://127.0.0.1:8000/api/checks \
  -H 'Authorization: Bearer sk-cs-9f2a' -H 'Idempotency-Key: order-0001' \
  -H 'Content-Type: application/json' \
  -d '{"mode":"llm","items":[{"id":"case-1","user_question":"支持退货吗？","system_reply":"支持30天无理由退货。","knowledge_base":"普通商品支持7天无理由退货。"}]}'

# 查询 / 列表 / 取消 / 删除
curl -H 'Authorization: Bearer sk-cs-9f2a' http://127.0.0.1:8000/api/checks/<task_id>
curl -H 'Authorization: Bearer sk-cs-9f2a' 'http://127.0.0.1:8000/api/checks?status_filter=completed'
curl -X POST -H 'Authorization: Bearer sk-cs-9f2a' http://127.0.0.1:8000/api/checks/<task_id>/cancel
curl -X DELETE -H 'Authorization: Bearer sk-ops' http://127.0.0.1:8000/api/checks/<task_id>

# 监控与就绪
curl http://127.0.0.1:8000/healthz; curl http://127.0.0.1:8000/readyz; curl http://127.0.0.1:8000/metrics
```

- 非 admin 的 Key 只能看到自己租户的任务/事件，跨租户访问返回 404。
- 限流与每日配额按 Key 计算，超出返回 429（带 `Retry-After`）。
- 响应头回写 `X-Request-ID`，日志为单行 JSON，可按请求 ID 全链路排查。
- 完整参数与错误码见 [API.md](docs/API.md)、[MONITOR.md](docs/MONITOR.md)；部署与值班见
  [DEPLOYMENT.md](docs/DEPLOYMENT.md)、[OPERATIONS.md](docs/OPERATIONS.md)。
CLI 核心使用标准库；HTTP API、监测平台和接口测试需按 `requirements.txt` 安装依赖。`.env.example` 为占位模板，真实检测读取根目录 `.env`，环境变量优先。

## 独立检测与评估

```bash
.venv/bin/python main.py detect --mode mock --output outputs/mock/predictions.json
.venv/bin/python main.py evaluate --predictions outputs/mock/predictions.json --output outputs/mock/metrics.json
```

`detect` 不读取人工标签；`evaluate` 按 ID 严格对齐，缺失或重复直接报错。
`--mode llm` 使用真实接口且不会回退到 mock；正式批量运行推荐使用 `app.run`，以保留逐条结果与原始响应。

```bash
.venv/bin/python -B -m app.run --output outputs/real-first
```

## 分类与判定边界

| 分类 | 定义 | 默认严重程度 |
|---|---|---|
| 政策与优惠错误 | 编造或错误描述优惠、发票、售后政策 | 高 |
| 产品事实错误 | 错误描述材质、规格、接口、功能 | 中，关键决策可升高 |
| 业务信息编造 | 编造地址、门店、品牌关联 | 中，错误寄件等升高 |
| 能力越界与虚假执行 | 系统无相关能力却声称查询、修改或升级 | 高 |
| 安全提示失真 | 与知识依据中的安全限制冲突 | 高 |
| 条件遗漏与过度概括 | 遗漏条件导致结论或建议失真 | 中 |

一条回复允许多标签。严重程度按实际后果调整，低等级用于不影响决策的次要事实问题。
逐声明区分支持、冲突、无依据和不可核验。无依据的确定性业务断言可构成幻觉，但不代表已经证伪现实事实。
普通省略不算幻觉；只有省略导致误导时才判定。原文引用必须确实出现在输入中。

## 检测方法与后续 API 接入

真实 LLM 将回复拆为事实声明，与单条 `knowledge_base` 对照，输出结构化结果。
`providers.py` 定义统一检测接口；`prompts.py` 与 `llm.py` 保存提示词和结果契约。
无需向量数据库或多 Agent。`llm.py` 使用 120 秒请求超时，对网络错误、限流和部分服务端错误最多尝试两次；不自动重试语义校验失败。
失败必须显式报告，不得默认视为正常。密钥放环境变量，禁止提交。

## 评估口径

人工标签中有 18 条幻觉、2 条正常（h12、h16）。这只是标签分布，不是检测结果。
全部报幻觉即有 100% Recall、90% Accuracy，所以必须同时展示 TP/FP/FN/TN、Precision、Recall、F1 和具体错例。
`Precision=TP/(TP+FP)`，`Recall=TP/(TP+FN)`，`F1=2TP/(2TP+FP+FN)`，`Accuracy=(TP+TN)/N`。
分母为零输出 `null`。仅明确报幻觉视为检出；待核验的真实幻觉仍计为漏检，并单列待核验 ID。
mock 不生成正式指标；真实结果以对应运行目录中的 `metrics.json` 为准。人工标签无严重程度，不宣称验证严重程度准确率。
已在方案阶段查看人工标签，因此后续成绩是给定样本验证，不是盲测或泛化能力证明。

## 预期误判难点（尚非实测错例）

- h09/h15：未提供功能或关系依据，不能因没有显式冲突而直接放行。
- h16：色差说明正确，但「都是实物拍摄」缺少依据，严格核验可能与人工标签分歧。
- h12/h20：部分列举与误导性遗漏的区别。
- h04/h06：一条回复包含多个事实，不能因部分正确而放行整条。

## 首次真实检测实测

模型 `glm-5.3-200k`，共完成 20/20 条，未将人工标签发送到检测接口。

| 指标 | 结果 |
|---|---|
| 检出率 Recall | 100% |
| 精确率 Precision | 94.74% |
| F1 | 97.30% |
| 准确率 Accuracy | 95% |
| TP / FP / FN / TN | 18 / 1 / 0 / 1 |

漏检：无；误报：h16。模型认为「都是实物拍摄」无依据，并错误地把「基本准确」与「轻微色差」视为互斥。前者存在标注口径差异，后者属于语义理解过严。本次保留原始标签、首次提示词与实际评分。

正常样本仅 2 条，其中 1 条误报（50%），因此不能只凭 100% 召回率声称生产可用。此次属于给定样本验证。

结果位于 `outputs/real-first/`：`report.html` 为真实数据图表，`analysis.md` 为误判分析，`errors.json` 为错例对照，`raw/` 保存有效响应。运行发生的断连和补跑记在 `run-notes.json`。这些产物默认被 Git 忽略，交付时需另行导出。

## HTTP 接口（批量检测）

```bash
.venv/bin/python -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
```

访问 http://127.0.0.1:8000/docs 查看在线文档。Postman 导入 `docs/postman_collection.json`；Apifox 导入 `docs/openapi.json`。完整流程、示例和限制见 [API 使用说明](docs/API.md)。

提交 `POST /api/checks` 后获取 `task_id`，通过 `GET /api/checks/{task_id}` 查询。默认 mock，`mode=llm` 使用服务端模型配置。人工标签通过独立 evaluate 接口提交，无标签时不显示检出率。

## 持续监测平台

监测平台把「批量检测」变成持续能力：外部系统按事件推送会话，平台装配知识、抽样、检测、落库、告警，并暴露统计接口。

```bash
# 终端 1：上游批量检测服务
.venv/bin/python -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1

# 终端 2：监测平台（默认读取根目录 .env）
.venv/bin/python -B -m monitor --port 8010
```

接入一条事件（`MONITOR_REQUIRE_AUTH=0` 时可不带令牌；默认要求鉴权）：

```bash
curl -X POST http://127.0.0.1:8010/api/ingest/events \
  -H 'Content-Type: application/json' \
  -H 'X-Monitor-Token: change-me-token-1' \
  --data '{"session_id":"s1","user_question":"退款多久到账？","system_reply":"已经帮您退款，1-3 个工作日到账。","category":"after_sale"}'
```

```bash
curl http://127.0.0.1:8010/health
curl http://127.0.0.1:8010/api/monitor/stats
curl http://127.0.0.1:8010/metrics      # Prometheus 文本格式
```

- 规则快检：声称「已退款/已改地址/已升级」时，只有知识依据**明确说明系统不具备该能力**才判能力越界；否则记待核验并进入复核队列。知识库外的专属联系人/仓库判业务编造并待复核。规则不消耗模型调用。
- 批量转发：同一批最多 20 条合并成一次上游任务，减少往返、提高吞吐；单条失败不牵连同批其他事件。
- 失败重试：上游网络错误、429、503、超时按指数退避重试（默认 3 次），重试耗尽才降级为 `not_verifiable`（`detector=fallback`），并保留错误码。
- 人工复核：`POST /api/monitor/events/{id}/review` 记录「确认」或「误报」，用于统计复核后精确率；面板「线上监测」页签可直接操作。
- 指标口径：幻觉率只在**明确结论**上计算，同时暴露覆盖率与降级率，防止上游故障把幻觉率冲淡。
- 抽样与幂等：高风险类目（`after_sale,payment,product`）全量，其余按哈希概率；同一会话、同一问答重复接入返回既有 `event_id`，不重复计费。
- 合规：入参上限 200 条/请求、读接口非本机绑定强制要求面板令牌、会话数据默认保留 30 天并支持按事件删除；转发上游前脱敏手机号、座机、身份证、银行卡、邮箱与订单号。
- 告警：high 立即推 webhook（带 `X-Monitor-Signature` HMAC 签名），medium 聚合成日报，支持去重窗口与夜间静默。

接口清单、鉴权规则、指标口径和运行限制见 [监测平台说明](docs/MONITOR.md)；Postman/Apifox 导入文件为 `docs/postman_monitor.json`。

### 运行口径与复核闭环

| 指标 | 含义 |
|---|---|
| `hallucination_rate` | 幻觉数 ÷ 明确结论数（不含不可核验） |
| `coverage` | 明确结论数 ÷ 已检测数，反映结论可用比例 |
| `degraded_rate` | 不可核验数 ÷ 已检测数，反映降级与待核验比例 |
| `review.pending` / `precision_after_review` | 待复核条数 / 已复核样本的确认率 |

生产使用建议：把 `coverage` 与 `degraded_rate` 一起纳入告警（覆盖率下跌、降级率上升都要看），幻觉率只作为「已判定样本」上的指标；规则快检与降级结论一律走人工复核，复核结果才是对外可用的口径。

## 接入商城（智选票 zhixuanpiao）

检测要判得准，回复里的"能不能退、有没有退款、演出信息"必须和商城的真实数据核对。
检测侧已内置商城知识源适配器（`monitor/shop_knowledge.py`）：配置 `MONITOR_KNOWLEDGE_SOURCE=shop`
指向商城的只读知识接口即可，接口不可用会回退本地 `knowledge/` 并明确标注。
支持两种取数方式：`MONITOR_KNOWLEDGE_SOURCE=shop`（调商城只读接口）或 `=mysql`（直连只读库，无需改商城代码）。
接口契约、表映射（`d_program_*` / `d_order_*` / `d_pay_bill_*` / `d_refund_bill_*`）、
商城侧 Java 骨架、字符集/分片注意事项与验收清单见 [docs/SHOP_INTEGRATION.md](docs/SHOP_INTEGRATION.md)。

## 前端面板

```bash
cd frontend && npm install && npm run dev
```

面板默认从 `/data/*.json` 读取已合并的真实结果，也可切换到「在线接口」提交任务、按 `task_id` 查看结果；「线上监测」页签显示幻觉率/覆盖率/降级率/复核后精确率、判断趋势与待复核队列，并可一键确认或标记误报。Vite 开发服务器把 `/api` 与 `/health` 代理到 `http://127.0.0.1:8000`，把 `/api/monitor`、`/api/ingest` 代理到 `http://127.0.0.1:8010`，因此使用在线功能前需先启动对应服务。

## 文件结构

- `app/`：`schema.py` 契约校验、`providers.py` 检测接口与 mock、`detect.py` 检测入口、`evaluate.py` 独立评估、`report.py` 安全转义的静态报告。
- `app/llm.py`、`app/run.py`：真实 API 适配与批量运行。
- `app/api.py`、`app/jobs.py`：本机异步 HTTP 接口与任务持久化。
- `monitor/`：接入 API、工作循环、知识装配、规则快检、抽样、SQLite 存储、告警路由、Prometheus 指标。
- `knowledge/`：政策、类目、SKU、联系人名单。
- `frontend/`：Vue 3 面板。
- `tools/merge_api_results.py`：把多次 HTTP 任务结果合并成一次完整评估与报告。
- `tests/`：88 项离线单元测试，覆盖指标口径、ID 对齐、证据引用、接口契约、鉴权与租户隔离、限流配额、幂等提交、任务取消/删除、监测链路、批量转发、失败重试与重试耗尽、复核审计、告警重投、保留期清理、接入上限与脱敏。
- `common/`：跨服务共用的鉴权、限流配额与结构化日志。
- `examples/`、`Makefile`、`scripts/`、`Dockerfile`、`docker-compose.yml`、`.github/workflows/tests.yml`：调用示例、常用命令、一键启动、容器化与 CI。
- `data/`：用户提供的原始附件；`outputs/`、`data/monitor.db` 为本地运行产物，不提交版本控制。

## AI 工具使用情况

使用 Codex 辅助方案设计、基础代码、文档、接口联调与本地验证。
真实 API 使用用户配置的接口与模型；模型生成判断，程序校验证据并独立评估。截图仅能来自实际运行。
每次真实运行保留模型、提示词版本、参数、原始响应和实测指标。

## 后续改进

1. 使用独立案例验证程度词、限定语及无依据断言的判断边界。
2. 增加正常样本，单独验证误报表现。
3. 完成真实报告的浏览器视觉验收和交付截图。
4. 前端面板补充构建验证（本仓库环境未执行 `npm install`，仅做了脚本语法检查）。
5. 接入工单/订单系统，让「已退款/已改地址」这类执行类声明可以直接核对，而不是停留在待核验。
6. 流量继续增长时提高上游并发或改成消息队列，把任务队列从单机搬到 Redis/Celery 或同类组件。
7. 多租户目前是同库逻辑隔离，若需物理隔离或强 SLA，按租户独立部署并接入统一网关计费。