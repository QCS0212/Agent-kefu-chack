# Postman / Apifox 调用说明

## 启动服务

在项目根目录执行：

```bash
# 首次安装或换机器时（已有环境可跳过）
uv pip install --python .venv/bin/python -r requirements.txt
# 启动，保持此终端运行
.venv/bin/python -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
```

- 服务地址：`http://127.0.0.1:8000`
- 在线接口文档：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`
- 真实模型配置从服务端 `.env` 读取，客户端不传模型密钥。mock 无需模型密钥。
- 当前仅供本机开发使用，无多用户鉴权；拒绝远程客户端和其他站点的浏览器请求。不要直接作为公网服务部署。
- 使用一个服务进程；同一任务目录有进程锁，不能使用多个 workers。修改代码后停止并重启，不建议处理任务时使用 reload。

## Postman

1. Import 导入 `docs/postman_collection.json`。
2. 集合变量 `base_url` 默认是 `http://127.0.0.1:8000`。
3. 先发送「健康检查」，再发送「提交 mock 检测」。
4. 提交响应为 202，集合脚本会自动将 `task_id` 写入集合变量。
5. 发送「查询任务进度与结果」，建议每 2–5 秒查询一次。
6. `status=completed` 后可获取 HTML 报告，或提交人工标签评估。
7. 需要真实检测时发送「提交真实检测」；这会使用服务端配置并产生上游模型调用。

如果集合脚本没有执行，手动复制响应中的 task_id 到集合变量。示例使用附件中的 h12，配套人工标签也只对应 h12；更换输入后需同步更换评估标签。

## Apifox

导入 `docs/openapi.json`，也可使用本机 OpenAPI URL。设置环境前置 URL 为 `http://127.0.0.1:8000`。请求体选择 JSON；在查询、评估和报告接口的路径参数中填入提交响应的 task_id。

静态 OpenAPI 文件反映当前版本；修改接口后应重新导出或使用在线 OpenAPI。部分客户端导入后需要手动填写下面的请求示例。

## 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 进程健康检查，不测试模型连通性 |
| POST | `/api/checks` | 提交检测，立即返回 202 |
| GET | `/api/checks/{task_id}` | 获取进度、逐条结果与错误 |
| POST | `/api/checks/{task_id}/evaluate` | 提交人工标签，本地独立评估 |
| GET | `/api/checks/{task_id}/report` | 返回可离线保存的 HTML 报告 |

## 提交请求

```json
{
  "mode": "mock",
  "items": [
    {
      "id": "case-001",
      "user_question": "支持退货吗？",
      "system_reply": "支持七天退货。",
      "knowledge_base": "支持七天退货。"
    }
  ]
}
```

`mode` 默认为 mock；改为 llm 才会调用真实模型。单条和批量使用相同接口，items 为 1–20 条。ID 在本次任务内唯一且最长100字符，三个内容字段分别最多20,000字符，字段必须为非空字符串。禁止附带 ground_truth、密钥或其他未定义字段。

响应示意：

```json
{
  "task_id": "生成的 UUID",
  "status": "accepted",
  "status_url": "/api/checks/生成的 UUID",
  "report_url": "/api/checks/生成的 UUID/report"
}
```

状态码 202 只代表已接收任务，不代表检测成功。

## 任务状态与结果

- `queued`：排队。
- `running`：检测中，可查询已完成结果。
- `completed`：全部检测及报告生成完成。
- `partial_failed`：部分样本技术失败，其余成功结果保留。
- `failed`：全部检测失败，或报告/存储处理失败；检查 errors 或 error。
- `interrupted`：服务重启时发现未完成任务，保留已有结果，需要重新提交。

`progress.processed` 为成功加失败数，`succeeded` 为成功数，`failed` 为技术失败数。`results` 中包含 verdict、types、severity、reason、claims。技术失败单独放入 errors，不冒充正常或证据不足。

当前最多同时运行2个任务，排队加运行最多8个。重复提交会创建新任务，也可能重复收费；首版不支持幂等键、取消和自动恢复。请保留 task_id，网络断开后先查询，不要直接重复点击提交。

## 独立评估

检测完整完成后，发送：

```json
{
  "labels": [
    {"id": "case-001", "is_hallucination": false}
  ]
}
```

标签 ID 必须与任务完整一致，值必须为 JSON 布尔值，不能使用字符串。标签只在本地评估，不发送模型。响应包含混淆矩阵、Precision、Recall、F1、Accuracy、错例 ID。mock 不提供正式指标。

没有标签时也能查看报告，但不显示检出率。评估后刷新同一报告 URL 即可看到指标。重复评估会使用最新提交的标签替换该任务的本地评估结果，不会重新调用模型。

## 错误码

| HTTP 状态 | 含义 |
|---|---|
| 403 | 非本机调用或不允许的浏览器来源 |
| 404 | task_id 不存在 |
| 409 | 任务未完整完成，评估/报告暂不可用 |
| 422 | 参数不合法、重复 ID 或标签不匹配 |
| 429 | 当前任务队列已满 |
| 503 | 提交真实任务时，服务端模型配置缺失或无效 |

上游模型认证失败、网络失败等发生在后台任务中，应查询任务状态和 errors，而不是只看提交请求的 202。

## curl 示例

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/api/checks \
  -H 'Content-Type: application/json' \
  --data-binary @docs/sample_request.json
curl http://127.0.0.1:8000/api/checks/替换为返回的task_id
```

## 保存与限制

任务位于 `outputs/api/<task_id>/`，含 task.json、raw/（真实响应）、report.html、evaluation.json（提交标签后）。当前服务重启后能查询已保存任务，未完成任务不会自动继续，避免自动产生重复调用。数据保存在本机，暂不自动清理，也不提供生产级队列和多用户隔离。

开发验证使用离线测试和本机 HTTP 客户端；导入文件按标准格式生成，未声称已经在 Postman/Apifox GUI 内完成导入验证。
