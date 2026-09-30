# 接入智选票（zhixuanpiao）：商城知识源与执行证据

面向 `D:\projects\zhixuanpiao` 的接入说明。检测侧适配器已实现（`monitor/shop_knowledge.py`），
商城侧只需按本文暴露**一个只读接口**（或提供一个只读库账号），检测结果就能从"手写知识"升级为"真实业务依据"。

## 1. 现状与结论

- 智选票是 Spring Cloud 票务商城：网关 6085、user 6082、base-data 6083、customize 6084、program 6086、order 8081、pay 6087、admin 10082。
- 仓库中**没有客服 / 对话 / IM / 机器人模块**（`zhixuanpiao-ai-master` 已在提交 0655d97 删除），因此不存在"在回复生成处加一行上报"的现成落点。
- 但有完整的**事实依据**：节目退改政策、订单状态、支付账单、退款账单。把它接进来，检测才有据可依。

接入分三层，按投入产出排序：

| 层 | 内容 | 改动位置 | 状态 |
|---|---|---|---|
| ① 知识源（拉取） | 检测时按 `category/sku/store_id` 取真实政策与执行记录 | 检测侧已实现；商城侧加 1 个只读接口 | ✅ 检测侧完成 |
| ② 执行事件（推送） | 退款成功、订单取消等真实结果推给监测平台 | 商城 pay / order 服务 | 待确认 |
| ③ 客服回复上报 | 有 AI/人工客服后，在回复落库处上报 | 未来的客服模块 | 暂无落点 |

## 2. 检测侧已实现的能力

新增 `monitor/shop_knowledge.py`：实现与 `FileKnowledgeSource` 相同的 `retrieve()` 协议，按配置切换知识源。

`.env` 配置：

```bash
MONITOR_KNOWLEDGE_SOURCE=shop
MONITOR_SHOP_KNOWLEDGE_URL=http://127.0.0.1:6085/zhixuanpiao/customize/knowledge/get
MONITOR_SHOP_TOKEN=<与商城约定的内网令牌>
MONITOR_SHOP_TIMEOUT=5
MONITOR_SHOP_FALLBACK=file   # 商城接口挂掉时回退本地 knowledge/；填 none 则直接标记"不可核验"
```

行为约定：

- 调用失败（超时/4xx/5xx/非 JSON）→ 记 WARNING 日志，按 `MONITOR_SHOP_FALLBACK` 回退；回退时知识文本前缀标注
  `【商城知识接口不可用（原因），以下为本地知识库依据】`，不会把"取不到依据"误判成幻觉。
- 接口可用但返回空知识 → 视为失败，同样按回退策略处理。
- 知识版本（`version`）、命中项（`matched_by`）随检测结果落库，便于追溯依据来源。

## 3. 商城侧接口契约（需实现）

**位置建议**：`zhixuanpiao-customize-service` 新增只读 `KnowledgeController`（单服务内改动）。
网关已配置 `/zhixuanpiao/customize/**` → `StripPrefix=2`，因此 Controller 映射 `/knowledge/get` 对外即
`/zhixuanpiao/customize/knowledge/get`。若放到其它服务，同步改 `MONITOR_SHOP_KNOWLEDGE_URL`。

**请求**（POST + JSON，与现有风格一致）：

```json
{ "category": "after_sale", "sku": "1001", "store_id": null }
```

- `category`：`after_sale`（退改/售后）、`payment`（支付退款）、`product`（节目与票档）、`default`
- `sku`：节目 ID 或票档 ID；`store_id`：多门店/渠道场景预留，可为空

**响应**（兼容现有 `ApiResponse` 包装，检测侧会自动解包 `data`）：

```json
{
  "code": 200,
  "msg": "ok",
  "data": {
    "knowledge": "【退改政策】《XX 演唱会》为条件退：开演前 24 小时可申请退票，收取票面 10% 手续费。\n【订单执行记录】订单 202609300001 状态=已退单，取消时间 2026-09-29 10:12。\n【退款记录】退款金额 488 元，状态=已退款，到账时间 2026-09-30 09:05，原因=用户申请。",
    "version": "2026-09-30T20:00:00+08:00",
    "matched_by": ["program:1001", "order:202609300001", "refund:202609300001"],
    "source": "zhixuanpiao"
  }
}
```

硬性要求：**只读、幂等、无副作用**；单次响应建议 < 8000 字符（检测提示词会一并发送）；
超时/异常返回 5xx 或 `code != 200` 即可，检测侧自有回退逻辑。

## 4. 数据映射（基于仓库现有表，字段已核对）

| 用途 | 表 | 关键字段 |
|---|---|---|
| 退改政策 | `d_program_{0,1}` | `id`、`program_title`、`program_place`、`program_show_time`、`permit_refund`（0 不支持退 / 1 条件退 / 2 全部退） |
| 票档与价格 | `d_ticket_category_*`、`d_seat_{0,1}` | `program_id`、`ticket_category_id`、`price`、`sell_status`（1 未售 2 锁定 3 已售） |
| 订单状态 | `d_order_{0,1}` | `order_number`、`program_id`、`program_title`、`program_show_time`、`order_price`、`order_status`（1 未支付 2 已取消 3 已支付 4 已退单）、`pay_order_time`、`cancel_order_time` |
| 支付账单 | `d_pay_bill_{0,1}` | `out_order_no`、`pay_amount`、`pay_bill_status`、`pay_time` |
| 退款账单 | `d_refund_bill_{0,1}` | `out_order_no`、`pay_bill_id`、`refund_amount`、`refund_status`（1 未退款 2 已退款）、`refund_time`、`reason` |

分片提示：`program / order / pay / refund` 均为 0/1 双分片（ShardingSphere 配置见 program-service 的
`shardingsphere-program-*.yaml`）。按 `order_number` / `out_order_no` 取模定位分片；不确定时两片都查再合并。

**这段映射就是"能不能判准"的关键**：有了 `d_refund_bill.refund_status=2` 与 `refund_time`，
"已经帮您退款了"这类声明才能从"待核验"变成"有依据"，否则检测只能一直标 `not_verifiable`。

Java 侧骨架（放进 customize-service，按现有 `ApiResponse` / DTO / VO 风格）：

```java
@RestController
@RequestMapping("/knowledge")
@Tag(name = "knowledge", description = "检测平台只读知识接口")
public class KnowledgeController {

    @Autowired
    private KnowledgeService knowledgeService;

    @Operation(summary = "按类目/节目取核验依据（只读，供质量监测平台调用）")
    @RequestMapping(value = "/get", method = RequestMethod.POST)
    public ApiResponse<KnowledgeVo> get(@RequestBody KnowledgeDto dto) {
        return ApiResponse.ok(knowledgeService.assemble(dto));
    }
}
```

`assemble()` 里按 `category` 组装文本：`after_sale` → 政策 + 订单/退款记录；`product` → 节目信息 + 票档；
`payment` → 支付/退款状态；最后统一拼成 `knowledge` 字符串与 `matched_by` 列表。
调用方是内网质量平台，鉴权建议用固定内网令牌（对齐 `MONITOR_SHOP_TOKEN`），不要复用面向用户的登录态。

## 5. 实施步骤

1. 商城侧：新增 `KnowledgeController` + `KnowledgeService`（只读 SQL，见上表），返回上述契约。
2. 检测侧：`.env` 设 `MONITOR_KNOWLEDGE_SOURCE=shop` 与 `MONITOR_SHOP_KNOWLEDGE_URL`，重启监测平台。
3. 自检：
   ```bash
   curl -s -X POST http://127.0.0.1:6085/zhixuanpiao/customize/knowledge/get \
     -H 'Content-Type: application/json' -H 'X-Monitor-Token: <token>' \
     -d '{"category":"after_sale","sku":"1001"}' | head -c 400
   ```
   再往监测平台接入一条事件（同一 `sku`），查看 `GET /api/monitor/events/{id}` 里的
   `detection.knowledge_version` 是否变成商城返回的 `version`、`by_type` 是否落在预期分类。
4. 灰度：先 `MONITOR_SAMPLE_RATE=0.1` 观察一周，看 `coverage` 与 `degraded_rate`，再决定放量。

## 6. 直连只读库（已实现，推荐用于智选票）

如果不想动商城代码，给检测平台一个**只读账号**即可（仅 `SELECT`，限定下面这些库表）。
检测侧已内置 `monitor/mysql_knowledge.py`，实现与知识源协议一致：

```bash
MONITOR_KNOWLEDGE_SOURCE=mysql
MONITOR_SHOP_DB_HOST=127.0.0.1      # 容器里连宿主机库用 host.docker.internal
MONITOR_SHOP_DB_PORT=3306
MONITOR_SHOP_DB_USER=kefu_ro        # 仅 SELECT 权限
MONITOR_SHOP_DB_PASSWORD=<只读口令>
MONITOR_SHOP_DB_PROGRAM=zhixuanpiao_program
MONITOR_SHOP_DB_ORDER=zhixuanpiao_order
MONITOR_SHOP_DB_PAY=zhixuanpiao_pay
MONITOR_SHOP_DB_SUFFIXES=0,1
MONITOR_SHOP_FALLBACK=file          # 库不可用时回退本地 knowledge/
```

查询与业务键的约定：`after_sale` / `payment` 传**订单号**（`sku`），`product` 传**节目 ID**；
传订单号时会一并带出该节目的退改政策，因此一次查询就能同时拿到"政策"和"执行记录"。
适配器只执行硬编码的 `SELECT`，连接后还会尝试 `SET SESSION TRANSACTION READ ONLY`。

实测输出（本地 MySQL 8 导入仓库 `sql/cloud` + 演示订单）：

```
【订单执行记录】订单号：202609300001 订单状态：已退单 …
【支付账单】支付状态：已支付 支付金额：580 元 …
【退款账单】退款状态：已退款 退款到账时间：2026-09-30 09:05:00 退款原因：用户申请退票
【节目信息】退票政策：不支持退票 / 退票规则原文：…不支持退换。 / 入场规则：须打开【票夹】扫码入场… / 发票说明：… / 实名购票规则：…
```

接入时请确认三件事：

1. **字符集**：连接必须用 `utf8mb4`（适配器已设置）。若导入/ETL 用了 latin1，库里会存成双重编码乱码，检测依据会变成天书——务必先用
   `SELECT HEX(title) FROM d_program_0 WHERE id=…` 校验（正常应为 `E4BA8E…` 这类 UTF-8 字节）。
2. **分片**：`program / order / pay` 都是 `_0/_1` 库 + `_0..3` 表，适配器会自动逐库逐表探测，库表不存在会跳过，不影响其它分片。
3. **权限最小化**：只读账号只授予上面六个库的 `SELECT`，并在数据库侧限制来源网段。

### 与 HTTP 方式的取舍

| | HTTP 只读接口 | 直连只读库 |
|---|---|---|
| 改商城代码 | 需要（1 个 Controller） | 不需要 |
| 与表结构耦合 | 低（商城侧封装） | 高（改表要同步改 SQL） |
| 上线速度 | 中 | 快 |
| 适用 | 长期、跨团队边界清晰 | 快速验证、内部工具 |

## 7. 第二层：把真实执行结果推给监测平台（可选）

如果还想让"退款/取消/改期"这类**动作发生后立即被监测**（而不是等有人提问才查库），
在 pay / order 服务的关键分支后加一行上报：

```java
// 退款成功后（PayService.refund 成功分支）
monitorReporter.report(Map.of(
    "session_id", refundDto.getOutOrderNo(),
    "user_question", "退款处理",
    "system_reply", "已为该订单退款 " + refundAmount + " 元",
    "category", "after_sale",
    "knowledge_snippet", "退款账单 " + outOrderNo + "：状态=已退款，金额=" + refundAmount
                         + "，到账时间=" + refundTime
));
```

上报器建议：独立线程池 + 有界队列 + 3 次指数退避重试 + 开关（默认关闭），
实现类可参照检测项目 `examples/client.py` 的 HTTP 调用，或直接用 `RestTemplate`/`WebClient` 发到
`POST http://<monitor-host>:8010/api/ingest/events`（Header `X-Monitor-Token`，作用域 `ingest`）。

## 8. 已知数据风险（需要商城确认）

1. **退改取值不一致**：`d_program.permit_refund` 注释是 `0 不支持退 / 1 条件退 / 2 全部退`，
   而 Java 枚举 `PermitRefund` 是 `NO_REFUND(0) / CONDITIONAL_REFUND(1) / FULL_REFUND(3)`。
   检测结论会直接引用这个口径，接入前请确认以哪套为准（否则"支持全退"可能被判错）。
2. **退票手续费/时效规则没有独立配置表**：目前只能从节目字段与订单记录推断。
   建议把"开演前 N 小时可退、手续费 X%"落到配置表，检测才有稳定依据。
3. **执行记录是分片表**：查询需按分片键路由，注意跨片聚合与性能。

## 9. 验收清单

- [ ] 商城侧只读接口返回契约字段齐全（`knowledge` / `version` / `matched_by`），响应 < 8000 字符
- [ ] 检测侧 `.env` 切到 `shop` 后，事件详情里的 `knowledge_version` 变成商城版本号
- [ ] 断网/停商城服务时，检测仍能按回退策略工作，且结果为 `not_verifiable` 而非 `hallucination`
- [ ] "已退款/已取消"类声明在有退款记录时可判定，不再长期停留在待核验
- [ ] 灰度一周后复核队列清空率、`precision_after_review` 达到约定基线