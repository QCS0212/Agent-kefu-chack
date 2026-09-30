"""数据模型。与既有检测服务的判定格式保持一致，前端可直接复用常量。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Verdict = Literal["hallucination", "no_hallucination", "not_verifiable"]
Severity = Literal["high", "medium", "low"]
Relation = Literal["supported", "contradicted", "unsupported", "not_verifiable"]
Detector = Literal["rule", "llm", "mock", "knowledge_gap", "fallback"]
ReviewStatus = Literal["pending", "confirmed", "rejected"]

CATEGORIES = {
    "policy_error": "政策与优惠错误",
    "product_error": "产品事实错误",
    "business_fabrication": "业务信息编造",
    "capability_overreach": "能力越界与虚假执行",
    "safety_misleading": "安全提示失真",
    "misleading_omission": "条件遗漏与过度概括",
}

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IngestEvent(BaseModel):
    """外部系统推送的一条会话事件。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(
        default="",
        max_length=64,
        description="服务端分配的事件 ID；接入请求无需提供，仅用于内部流转与上游关联",
    )
    session_id: str = Field(min_length=1, max_length=200, description="会话唯一标识")
    user_question: str = Field(min_length=1, max_length=20000)
    system_reply: str = Field(min_length=1, max_length=20000)
    category: str | None = Field(default=None, max_length=64, description="业务类目，用于装配知识与抽样")
    sku: str | None = Field(default=None, max_length=64)
    store_id: str | None = Field(default=None, max_length=64)
    occurred_at: str | None = Field(default=None, description="会话发生时间，缺省用接入时间")
    knowledge_snippet: str | None = Field(
        default=None, max_length=20000, description="外系统可直接附带知识依据；缺省由本地知识源装配"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class Claim(BaseModel):
    text: str
    relation: Relation
    reply_quote: str = ""
    knowledge_quote: str = ""
    reason: str = ""


class DetectionRecord(BaseModel):
    event_id: str
    verdict: Verdict
    types: list[str] = Field(default_factory=list)
    severity: Severity | None = None
    reason: str
    claims: list[Claim] = Field(default_factory=list)
    detector: Detector
    needs_review: bool = False
    knowledge_version: str | None = None
    latency_ms: int = 0
    created_at: str = Field(default_factory=now)


class EventDocument(BaseModel):
    """返回给外系统与面板的事件视图。"""

    event_id: str
    session_id: str
    category: str | None = None
    sku: str | None = None
    store_id: str | None = None
    status: str
    sampled: bool
    created_at: str
    detection: DetectionRecord | None = None
    error: str | None = None
    user_question: str
    system_reply: str
    knowledge_snippet: str | None = None
    tenant: str = "default"
    attempts: int = 0
    next_attempt_at: str | None = None
    review_status: ReviewStatus = "pending"
    review_note: str | None = None
    reviewed_at: str | None = None
    reviewer: str | None = None


class IngestAck(BaseModel):
    event_id: str
    status: str
    sampled: bool
    duplicate: bool = False
    status_url: str


class DetectionError(Exception):
    """检测失败（上游不可用、超时等）。调用方应记录失败，不得伪造判定。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
