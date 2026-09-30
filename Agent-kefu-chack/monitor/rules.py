"""规则快检：结构性矛盾不调用 LLM 直接判定。

覆盖两类场景：

1. 执行类声明——回复声称“已退款/已修改地址/已升级工单”。
   只有知识依据明确说明系统不具备该能力时才判定为幻觉；否则记为待核验，
   因为规则无法证明操作没有发生，必须与工单/订单系统核对。
2. 编造联系人——回复中出现知识库未登记的专属人名或仓库，属于启发式信号，
   命中后标记 needs_review，由人工复核确认。

规则只做证伪与线索提示，不负责判定“正常”；判正常必须交给模型。
"""
from __future__ import annotations

import re

from .knowledge import scan_unsupported_names
from .models import Claim, DetectionRecord, IngestEvent

EXECUTION_CLAIM = re.compile(
    r"(已(?:经)?(?:帮您|为您)?(?:完成|处理|修改|退款|升级|提交|登记)"
    r"|已退|已改|已.*?提交|已.*?升级|已.*?登记|马上.*?(?:给您|为您).{0,4}(?:退款|修改|办理))"
)

# 知识依据明确否定执行能力时，执行类声明才是确凿的能力越界
CAPABILITY_DENIAL = re.compile(
    r"(未接入|没有接入|不具备|无法(?:查询|修改|办理|操作|核实|执行|取消)"
    r"|不支持.{0,8}(?:查询|修改|升级|登记|退款|改地址|取消)"
    r"|暂无.{0,6}(?:接口|能力|权限)|无.{0,4}(?:查询|修改|操作).{0,4}(?:接口|能力|权限))"
)

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


class RuleEngine:
    """命中即返回 DetectionRecord，未命中返回 None。"""

    def __init__(self, knowledge_names: list[str] | None = None):
        self.knowledge_names = knowledge_names or []

    def check(self, event: IngestEvent, knowledge: str) -> DetectionRecord | None:
        reply = event.system_reply
        knowledge = knowledge or ""
        claims: list[Claim] = []
        types: list[str] = []
        findings: list[str] = []
        confirmed = False
        needs_review = False
        rank = 0

        execution_hit = EXECUTION_CLAIM.search(reply)
        if execution_hit:
            denial = CAPABILITY_DENIAL.search(knowledge)
            claims.append(
                Claim(
                    text=execution_hit.group(0),
                    relation="unsupported",
                    reply_quote=execution_hit.group(0),
                    knowledge_quote=denial.group(0) if denial else "",
                    reason="回复声称已完成业务操作；"
                    + (
                        "知识依据明确说明系统不具备该能力，属于能力越界"
                        if denial
                        else "未提供执行记录，需与工单/订单系统核对"
                    ),
                )
            )
            types.append("capability_overreach")
            if denial:
                confirmed = True
                rank = max(rank, SEVERITY_RANK["high"])
                findings.append("能力越界：知识依据明确否定执行能力")
            else:
                needs_review = True
                findings.append("执行类声明待核对（缺少执行记录）")

        fabricated = scan_unsupported_names(reply, knowledge, self.knowledge_names)
        if fabricated:
            for name in fabricated[:3]:
                claims.append(
                    Claim(
                        text=f"联系人/名称：{name}",
                        relation="unsupported",
                        reply_quote=name,
                        knowledge_quote="（知识库联系人名单中不存在该名称）",
                        reason="回复中出现知识库未登记的专属联系人或仓库，属于业务信息编造",
                    )
                )
            types.append("business_fabrication")
            confirmed = True
            needs_review = True
            rank = max(rank, SEVERITY_RANK["high"])
            findings.append("业务信息编造：出现知识库外的专属称谓")

        if not claims:
            return None

        severity = None
        verdict = "not_verifiable"
        if confirmed:
            verdict = "hallucination"
            severity = next(
                (name for name in ("high", "medium", "low") if SEVERITY_RANK[name] == rank), "medium"
            )
        return DetectionRecord(
            event_id="",
            verdict=verdict,
            types=sorted(set(types)),
            severity=severity,
            reason="规则快检：" + "；".join(findings),
            claims=claims,
            detector="rule",
            needs_review=needs_review,
        )


def attach_event_id(record: DetectionRecord | None, event_id: str) -> DetectionRecord | None:
    if record is None:
        return None
    record.event_id = event_id
    return record