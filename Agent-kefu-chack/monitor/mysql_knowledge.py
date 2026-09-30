"""商城知识源（直连只读库版）。

从智选票（zhixuanpiao）的分库分表里按业务键取核验依据：

- 传订单号（after_sale / payment）：订单状态 + 支付账单 + 退款账单 + 该节目退改政策
- 传节目 ID（product / default）：节目退改规则、入场/配送/发票/实名/限购等原文

为什么这些字段最值钱：`d_program_*.refund_ticket_rule`、`entry_rule`、`invoice_specification`、
`real_ticket_purchase_rule`、限购数量，正是客服最容易编造、也最容易核实的内容；
`d_refund_bill_*.refund_status / refund_time` 则是"已经帮您退款了"这类声明的唯一证据来源。

安全约定：只执行本文件里硬编码的 SELECT，连接后尝试 `SET SESSION TRANSACTION READ ONLY`，
生产环境请使用只读账号（仅 SELECT 权限、限定上述库表）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .knowledge import FileKnowledgeSource, KnowledgeSnippet

logger = logging.getLogger("monitor.mysql_knowledge")

ORDER_STATUS = {1: "未支付", 2: "已取消", 3: "已支付", 4: "已退单"}
REFUND_STATUS = {1: "未退款", 2: "已退款"}
PAY_STATUS = {1: "未支付", 2: "已支付"}
# 注意：d_program.permit_refund 注释为 0/1/2，Java 枚举 PermitRefund 用 0/1/3，两套都兼容
PERMIT_REFUND = {0: "不支持退票", 1: "条件退票", 2: "全部退票", 3: "全部退票"}

PROGRAM_COLUMNS = (
    "id, title, actor, place, permit_refund, refund_ticket_rule, entry_rule, "
    "delivery_instruction, child_purchase, invoice_specification, "
    "real_ticket_purchase_rule, per_order_limit_purchase_count, "
    "per_account_limit_purchase_count, important_notice, kind_reminder, edit_time"
)
ORDER_COLUMNS = (
    "order_number, program_id, program_title, program_place, program_show_time, "
    "order_price, order_status, pay_order_time, cancel_order_time, edit_time"
)
PAY_COLUMNS = "out_order_no, pay_amount, pay_bill_status, pay_time, edit_time"
REFUND_COLUMNS = "out_order_no, refund_amount, refund_status, refund_time, reason, edit_time"

FALLBACK_NOTE = "【商城库不可用（{reason}），以下为本地知识库依据】"
UNAVAILABLE_TEMPLATE = (
    "（知识库未命中：未能从商城库取到依据（{reason}）。"
    "请把回复中的事实声明标注为不可核验，不要臆测结论。）"
)


def _default_connect(config: "ShopDbConfig"):
    """建立只读连接；pymysql 为可选依赖，缺失时给出明确提示。"""
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise RuntimeError("未安装 pymysql，无法使用 mysql 知识源（pip install pymysql）") from exc
    connection = pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        charset=config.charset,
        connect_timeout=config.timeout,
        cursorclass=DictCursor,
        autocommit=True,
    )
    try:  # 只在会话级别打开只读，避免误写
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
    except Exception:  # pragma: no cover - 老版本 MySQL 不支持
        logger.debug("当前实例不支持会话级只读设置，忽略")
    return connection


@dataclass(frozen=True)
class ShopDbConfig:
    """分库分表配置：库名 + 后缀（如 zhixuanpiao_program_0 / _1）。"""

    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    program_db: str = "zhixuanpiao_program"
    order_db: str = "zhixuanpiao_order"
    pay_db: str = "zhixuanpiao_pay"
    db_suffixes: tuple[int, ...] = (0, 1)
    table_suffixes: tuple[int, ...] = (0, 1)
    charset: str = "utf8mb4"
    timeout: float = 5.0

    def _dbs(self, base: str) -> tuple[str, ...]:
        return tuple(f"{base}_{suffix}" for suffix in self.db_suffixes)

    @property
    def program_dbs(self) -> tuple[str, ...]:
        return self._dbs(self.program_db)

    @property
    def order_dbs(self) -> tuple[str, ...]:
        return self._dbs(self.order_db)

    @property
    def pay_dbs(self) -> tuple[str, ...]:
        return self._dbs(self.pay_db)


@dataclass
class MysqlKnowledgeSource:
    """直连只读库取商城依据，接口与 FileKnowledgeSource 一致。"""

    config: ShopDbConfig
    fallback: FileKnowledgeSource | None = None
    connect: Callable[[ShopDbConfig], Any] = _default_connect
    last: KnowledgeSnippet | None = field(default=None, init=False)

    def last_snippet(self) -> KnowledgeSnippet | None:
        return self.last

    def retrieve(self, *, category: str | None, sku: str | None, store_id: str | None) -> str:
        key = (sku or store_id or "").strip()
        try:
            if not key:
                raise ValueError("sku/store_id 为空，无法定位商城数据")
            connection = self.connect(self.config)
            try:
                cursor = connection.cursor()
                parts, matched, version = self._assemble(cursor, key)
            finally:
                connection.close()
            if not parts:
                raise LookupError("商城库未命中该对象")
            self.last = KnowledgeSnippet(
                text="\n".join(parts),
                version=version or f"mysql:{datetime.now(timezone.utc).isoformat()}",
                matched_by=matched,
                source="zhixuanpiao-mysql",
            )
        except Exception as exc:  # 网络/权限/未命中，统一降级，绝不臆造依据
            logger.warning("商城库知识查询失败（key=%s）：%s", key, exc)
            self.last = self._fallback(category, sku, store_id, reason=type(exc).__name__)
        return self.last.text

    # ------------------------------------------------------------------ 组装

    def _assemble(self, cursor, key: str) -> tuple[list[str], list[str], str | None]:
        parts: list[str] = []
        matched: list[str] = []
        versions: list[str] = []

        order = self._fetch(
            cursor, self.config.order_dbs, "d_order", ORDER_COLUMNS, "order_number", key
        )
        program: dict | None = None
        if order:
            matched.append(f"order:{key}")
            versions.append(str(order.get("edit_time") or ""))
            parts.append(self._order_text(order))
            pay = self._fetch(
                cursor, self.config.pay_dbs, "d_pay_bill", PAY_COLUMNS, "out_order_no", key
            )
            if pay:
                matched.append(f"pay:{key}")
                versions.append(str(pay.get("edit_time") or ""))
                parts.append(self._pay_text(pay))
            refund = self._fetch(
                cursor, self.config.pay_dbs, "d_refund_bill", REFUND_COLUMNS, "out_order_no", key
            )
            if refund:
                matched.append(f"refund:{key}")
                versions.append(str(refund.get("edit_time") or ""))
                parts.append(self._refund_text(refund))
            program_id = order.get("program_id")
            if program_id:
                program = self._fetch(
                    cursor,
                    self.config.program_dbs,
                    "d_program",
                    PROGRAM_COLUMNS,
                    "id",
                    program_id,
                )
        else:
            program = self._fetch(
                cursor, self.config.program_dbs, "d_program", PROGRAM_COLUMNS, "id", key
            )

        if program:
            matched.append(f"program:{program.get('id')}")
            versions.append(str(program.get("edit_time") or ""))
            parts.append(self._program_text(program))

        version = max([item for item in versions if item and item != "None"], default="")
        return parts, matched, version or None

    @staticmethod
    def _fetch(cursor, dbs: Sequence[str], table: str, columns: str, key_column: str, value):
        """跨库跨表探测：库或表不存在时跳过，不影响其它分片。"""
        for db in dbs:
            for suffix in (0, 1):
                sql = (
                    f"SELECT {columns} FROM `{db}`.`{table}_{suffix}`"
                    f" WHERE `{key_column}`=%s LIMIT 1"
                )
                try:
                    cursor.execute(sql, (value,))
                except Exception as exc:
                    logger.debug("跳过 %s.%s_%s：%s", db, table, suffix, exc)
                    continue
                row = cursor.fetchone()
                if row:
                    return dict(row)
        return None

    # ------------------------------------------------------------------ 文本

    @staticmethod
    def _program_text(row: dict) -> str:
        lines = ["【节目信息】"]
        if row.get("title"):
            lines.append(f"节目标题：{row['title']}")
        if row.get("actor"):
            lines.append(f"艺人：{row['actor']}")
        if row.get("place"):
            lines.append(f"地点：{row['place']}")
        permit = row.get("permit_refund")
        if permit is not None:
            lines.append(f"退票政策：{PERMIT_REFUND.get(int(permit), f'未定义({permit})')}")
        if row.get("refund_ticket_rule"):
            lines.append(f"退票/换票规则原文：{row['refund_ticket_rule']}")
        if row.get("entry_rule"):
            lines.append(f"入场规则：{row['entry_rule']}")
        if row.get("delivery_instruction"):
            lines.append(f"配送信息说明：{row['delivery_instruction']}")
        if row.get("child_purchase"):
            lines.append(f"儿童购票：{row['child_purchase']}")
        if row.get("invoice_specification"):
            lines.append(f"发票说明：{row['invoice_specification']}")
        if row.get("real_ticket_purchase_rule"):
            lines.append(f"实名购票规则：{row['real_ticket_purchase_rule']}")
        if row.get("per_order_limit_purchase_count") is not None:
            lines.append(f"每笔订单最多购买：{row['per_order_limit_purchase_count']} 张")
        if row.get("per_account_limit_purchase_count") is not None:
            lines.append(f"每个账号最多购买：{row['per_account_limit_purchase_count']} 张")
        if row.get("important_notice"):
            lines.append(f"重要通知：{row['important_notice']}")
        if row.get("kind_reminder"):
            lines.append(f"温馨提示：{row['kind_reminder']}")
        return "\n".join(lines)

    @staticmethod
    def _order_text(row: dict) -> str:
        status = row.get("order_status")
        lines = [
            "【订单执行记录】",
            f"订单号：{row.get('order_number')}",
            f"订单状态：{ORDER_STATUS.get(int(status), status) if status is not None else '未知'}",
        ]
        if row.get("program_title"):
            lines.append(f"节目：{row['program_title']}")
        if row.get("program_show_time"):
            lines.append(f"演出时间：{row['program_show_time']}")
        if row.get("order_price") is not None:
            lines.append(f"订单金额：{row['order_price']} 元")
        if row.get("pay_order_time"):
            lines.append(f"支付时间：{row['pay_order_time']}")
        if row.get("cancel_order_time"):
            lines.append(f"取消时间：{row['cancel_order_time']}")
        return "\n".join(lines)

    @staticmethod
    def _pay_text(row: dict) -> str:
        status = row.get("pay_bill_status")
        text = [
            "【支付账单】",
            f"商户订单号：{row.get('out_order_no')}",
            f"支付状态：{PAY_STATUS.get(int(status), status) if status is not None else '未知'}",
        ]
        if row.get("pay_amount") is not None:
            text.append(f"支付金额：{row['pay_amount']} 元")
        if row.get("pay_time"):
            text.append(f"支付时间：{row['pay_time']}")
        return "\n".join(text)

    @staticmethod
    def _refund_text(row: dict) -> str:
        status = row.get("refund_status")
        text = [
            "【退款账单】",
            f"商户订单号：{row.get('out_order_no')}",
            f"退款状态：{REFUND_STATUS.get(int(status), status) if status is not None else '未知'}",
        ]
        if row.get("refund_amount") is not None:
            text.append(f"退款金额：{row['refund_amount']} 元")
        if row.get("refund_time"):
            text.append(f"退款到账时间：{row['refund_time']}")
        if row.get("reason"):
            text.append(f"退款原因：{row['reason']}")
        return "\n".join(text)

    # ------------------------------------------------------------------ 降级

    def _fallback(self, category, sku, store_id, reason: str) -> KnowledgeSnippet:
        if self.fallback is not None:
            text = self.fallback.retrieve(category=category, sku=sku, store_id=store_id)
            base = self.fallback.last_snippet()
            return KnowledgeSnippet(
                text=f"{FALLBACK_NOTE.format(reason=reason)}\n{text}",
                version=(base.version if base else "fallback"),
                matched_by=(base.matched_by if base else []),
                source="mysql_fallback",
            )
        return KnowledgeSnippet(
            text=UNAVAILABLE_TEMPLATE.format(reason=reason),
            version="unavailable",
            matched_by=[],
            source="mysql_unavailable",
        )