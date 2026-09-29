# 客服回复幻觉检测

用于作业「0110 · 客服回复幻觉检测」的基础工程，Python 3.12+，HTTP API 使用 FastAPI / Uvicorn，依赖版本见 requirements.txt。

## 当前完成范围

- 已包含 20 条原始回复和人工标签；每条回复自带 knowledge_base。
- 已实现输入校验、检测接口、mock、独立评估、JSON 输出及 HTML 报告。
- 已提供指标计算、ID 对齐、引用校验测试。
- **已接入用户配置的 Chat Completions 兼容接口。mock 仍仅演示流程；真实运行结果保存在独立目录。**
- HTML 已支持 ECharts 判断分布、评估指标图、搜索筛选、回复与知识依据展开及 JSON 导出；真实运行支持网络错误有限重试、逐条保存和原始响应记录；最终交付截图待完善。

## 快速开始

在项目目录使用现有虚拟环境：

```bash
.venv/bin/python main.py demo
.venv/bin/python -m unittest discover -s tests -v
```

也可在 PyCharm 直接运行 main.py，默认执行 mock 演示。
打开 outputs/mock/report.html 查看报告。
CLI 核心仍使用标准库；HTTP API 和接口测试需按 requirements.txt 安装依赖。.env.example 为占位模板；真实检测读取根目录 .env，环境变量优先。

## 独立检测与评估

```bash
.venv/bin/python main.py detect --mode mock --output outputs/mock/predictions.json
.venv/bin/python main.py evaluate --predictions outputs/mock/predictions.json --output outputs/mock/metrics.json
```

detect 不读取人工标签。evaluate 按 ID 严格对齐，缺失或重复直接报错。
--mode llm 使用真实接口，不会回退到 mock。正式批量运行推荐使用下方 app.run 命令，以保留逐条结果。

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

真实 LLM 将回复拆为事实声明，与单条 knowledge_base 对照，输出结构化结果。
providers.py 定义统一检测接口；prompts.py 与 llm.py 保存提示词和结果契约。
无需向量数据库或多 Agent。llm.py 使用 120 秒请求超时，对网络错误、限流和部分服务端错误最多尝试两次；不自动重试语义校验失败。
失败必须显式报告，不得默认视为正常。密钥放环境变量，禁止提交。

## 评估口径

人工标签中有 18 条幻觉、2 条正常（h12、h16）。这只是标签分布，不是检测结果。
全部报幻觉即有 100% Recall、90% Accuracy，所以必须同时展示 TP/FP/FN/TN、Precision、Recall、F1 和具体错例。
Precision=TP/(TP+FP)，Recall=TP/(TP+FN)，F1=2TP/(2TP+FP+FN)，Accuracy=(TP+TN)/N。
分母为零输出 null。仅明确报幻觉视为检出；待核验的真实幻觉仍计为漏检，并单列待核验 ID。
mock 不生成正式指标；真实结果以对应运行目录中的 metrics.json 为准。人工标签无严重程度，不宣称验证严重程度准确率。
已在方案阶段查看人工标签，因此后续成绩是给定样本验证，不是盲测或泛化能力证明。

## 预期误判难点（尚非实测错例）

- h09/h15：未提供功能或关系依据，不能因没有显式冲突而直接放行。
- h16：色差说明正确，但「都是实物拍摄」缺少依据，严格核验可能与人工标签分歧。
- h12/h20：部分列举与误导性遗漏的区别。
- h04/h06：一条回复包含多个事实，不能因部分正确而放行整条。

## 文件结构

- app/schema.py：输入、结果与证据校验。
- app/providers.py：检测接口和 mock。
- app/detect.py：检测入口，不接触人工标签。
- app/evaluate.py：独立指标计算与错例 ID。
- app/report.py：安全转义后的静态 HTML 报告。
- tests/：关键评估与校验测试。
- data/：用户提供的原始附件。
- outputs/：本地运行产物，不提交版本控制。
- screenshots/：预留真实开发过程和运行结果截图。

## AI 工具使用情况

使用 Codex 辅助方案设计、基础代码、文档和本地验证。
真实 API 使用用户配置的接口与模型；模型生成判断，程序校验证据并独立评估。截图仅能来自实际运行。
每次真实运行保留模型、提示词版本、参数、原始响应和实测指标。

## 后续改进

1. 使用独立案例验证程度词、限定语及无依据断言的判断边界。
2. 增加正常样本，单独验证误报表现。
3. 完成真实报告的浏览器视觉验收和交付截图。

## 报告界面

运行 demo 后打开 outputs/mock/report.html。报告内嵌 ECharts 5.6.0，无需联网即可查看图表。源文件与许可证保存在 app/vendor/。
mock 模式显示待核验分布，不显示正式成绩；真实评估存在时显示精确率、召回率、F1 和准确率柱状图。列表支持搜索、判断筛选、展开原文和知识依据，图表始终统计全部样本。

## 真实检测

在本地 `.env` 填写 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`，不要提交密钥。接口需兼容 Chat Completions。

```bash
.venv/bin/python -B -m app.run --output outputs/real-first
```

每条成功结果立即保存。同一命令会跳过已完成项，只重试未完成项；模型、接口、输入或提示词变化时需要新目录。20 条全部成功后才读取人工标签，生成 metrics.json、errors.json 和 report.html。raw/ 保存接口响应与耗时，prompt.json 保存实际提示词。

## 首次真实检测实测

模型 `glm-5.3-200k`，共完成 20/20 条，未将人工标签发送到检测接口。

| 指标 | 结果 |
|---|---|
| 检出率 Recall | 100% |
| 精确率 Precision | 94.74% |
| F1 | 97.30% |
| 准确率 Accuracy | 95% |
| TP / FP / FN / TN | 18 / 1 / 0 / 1 |

漏检：无；误报：h16。模型认为“都是实物拍摄”无依据，并错误地把“基本准确”与“轻微色差”视为互斥。前者存在标注口径差异，后者属于语义理解过严。本次保留原始标签、首次提示词与实际评分。

正常样本仅 2 条，其中 1 条误报（50%），因此不能只凭 100% 召回率声称生产可用。此次属于给定样本验证。

结果位于 `outputs/real-first/`：`report.html` 为真实数据图表，`analysis.md` 为误判分析，`errors.json` 为错例对照，`raw/` 保存有效响应。运行发生的断连和补跑记在 `run-notes.json`。这些产物默认被 Git 忽略，交付时需另行导出。

## Postman / Apifox HTTP 调用

```bash
.venv/bin/python -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
```

访问 http://127.0.0.1:8000/docs 查看在线文档。Postman 导入 `docs/postman_collection.json`；Apifox 导入 `docs/openapi.json`。完整流程、示例和限制见 [API 使用说明](docs/API.md)。

提交 `POST /api/checks` 后获取 task_id，通过 `GET /api/checks/{task_id}` 查询。默认 mock，mode=llm 使用现有服务端模型配置。报告接口保留现有布局；人工标签通过独立 evaluate 接口提交，无标签时不显示检出率。
