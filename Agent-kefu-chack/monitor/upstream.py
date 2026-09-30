"""对接既有批量检测服务。

监测平台把装配好知识、脱敏后的对话按批转发到 POST /api/checks，再轮询结果。
上游任何失败都只能降级为 not_verifiable：评估层不得伪造“幻觉”或“正常”结论。
"""
from __future__ import annotations

import time

import httpx

from .config import Settings
from .knowledge import redact
from .models import CATEGORIES, Claim, DetectionError, DetectionRecord, IngestEvent

VERDICTS = {"hallucination", "no_hallucination", "not_verifiable"}
RELATIONS = {"supported", "contradicted", "unsupported", "not_verifiable"}
SEVERITIES = {"low", "medium", "high"}
TERMINAL_STATES = {"completed", "partial_failed", "failed", "interrupted"}

FALLBACK_KNOWLEDGE = (
    "知识库未命中：该回复没有可核验的业务资料。"
    "请把其中的事实声明标注为不可核验（not_verifiable），不要臆测结论。"
)

POLL_INTERVAL_SECONDS = 1.0
POLL_MAX_ATTEMPTS = 180  # 单批最多等 3 分钟
MAX_ITEMS_PER_TASK = 20  # 与上游单批上限一致


def chunks(items: list, size: int = MAX_ITEMS_PER_TASK) -> list[list]:
    size = max(1, size)
    return [items[index : index + size] for index in range(0, len(items), size)]


def build_payload(pairs: list[tuple[IngestEvent, str]], mode: str) -> dict:
    """把 (事件, 知识) 列表转成既有服务入参，发送前一律脱敏。"""
    return {
        "mode": mode,
        "items": [
            {
                "id": event.event_id,
                "user_question": redact(event.user_question),
                "system_reply": redact(event.system_reply),
                "knowledge_base": redact(knowledge) if knowledge else FALLBACK_KNOWLEDGE,
            }
            for event, knowledge in pairs
        ],
    }


def to_record(result: dict, detector: str) -> DetectionRecord:
    """严格校验上游输出，任何不合法都算失败，由调用方降级。"""
    if not isinstance(result, dict):
        raise DetectionError("UPSTREAM_INVALID", "检测结果不是对象")
    if result.get("verdict") not in VERDICTS:
        raise DetectionError("UPSTREAM_INVALID", f"verdict 非法: {result.get('verdict')!r}")
    types = result.get("types")
    if not isinstance(types, list) or any(t not in CATEGORIES for t in types):
        raise DetectionError("UPSTREAM_INVALID", "types 分类非法")
    severity = result.get("severity")
    if severity is not None and severity not in SEVERITIES:
        raise DetectionError("UPSTREAM_INVALID", "severity 非法")
    reason = result.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise DetectionError("UPSTREAM_INVALID", "缺少判断理由")
    raw_claims = result.get("claims")
    if not isinstance(raw_claims, list):
        raise DetectionError("UPSTREAM_INVALID", "claims 必须为数组")
    claims: list[Claim] = []
    for item in raw_claims:
        if not isinstance(item, dict) or item.get("relation") not in RELATIONS:
            raise DetectionError("UPSTREAM_INVALID", "事实声明缺少合法 relation")
        reply_quote = item.get("reply_quote")
        knowledge_quote = item.get("knowledge_quote")
        if not isinstance(reply_quote, str) or not reply_quote:
            raise DetectionError("UPSTREAM_INVALID", "声明缺少回复引用")
        if not isinstance(knowledge_quote, str):
            raise DetectionError("UPSTREAM_INVALID", "知识引用必须为字符串")
        if item["relation"] == "contradicted" and not knowledge_quote.strip():
            raise DetectionError("UPSTREAM_INVALID", "冲突声明缺少知识引用")
        claims.append(
            Claim(
                text=str(item.get("text") or reply_quote),
                relation=item["relation"],
                reply_quote=reply_quote,
                knowledge_quote=knowledge_quote,
                reason=str(item.get("reason") or ""),
            )
        )
    problems = any(c.relation in {"contradicted", "unsupported"} for c in claims)
    verdict = result["verdict"]
    if verdict == "hallucination" and not (problems and types and severity and claims):
        raise DetectionError("UPSTREAM_INVALID", "幻觉结论缺少分类、严重程度或事实依据")
    if verdict == "no_hallucination" and (problems or types or severity is not None):
        raise DetectionError("UPSTREAM_INVALID", "正常结论与声明/分类自相矛盾")
    return DetectionRecord(
        event_id=result.get("id") or "",
        verdict=verdict,
        types=list(types),
        severity=severity,
        reason=reason,
        claims=claims,
        detector=detector,
    )


def fallback_record(event_id: str, error: DetectionError) -> DetectionRecord:
    """上游不可用时降级：只标注不可核验，不伪造任何判定。"""
    return DetectionRecord(
        event_id=event_id,
        verdict="not_verifiable",
        types=[],
        severity=None,
        reason=f"上游检测不可用（{error.code}），已降级为不可核验：{error.message}",
        claims=[],
        detector="fallback",
        needs_review=True,
    )


class UpstreamClient:
    """调用既有检测服务的客户端。"""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.base_url = settings.upstream_url
        self.mode = settings.upstream_mode
        self.detector_name = "llm" if self.mode == "llm" else "mock"
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0, transport=transport)

    def close(self) -> None:
        self.client.close()

    def probe(self) -> dict:
        """/health 探活，供监测平台健康检查使用。"""
        try:
            response = self.client.get("/health")
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)}

    def detect(self, event: IngestEvent, knowledge: str) -> DetectionRecord:
        """单条检测：内部走批量接口，保留原有调用方式。"""
        outcomes = self.detect_many([(event, knowledge)])
        outcome = outcomes.get(event.event_id)
        if isinstance(outcome, DetectionError):
            raise outcome
        if outcome is None:
            raise DetectionError("UPSTREAM_DETECTION_FAILED", "上游未返回该事件的结果")
        return outcome

    def detect_many(self, pairs: list[tuple[IngestEvent, str]]) -> dict[str, DetectionRecord | DetectionError]:
        """批量检测：按上游单批上限分片，返回 事件ID -> 结果或单条错误。

        批次级失败（网络、队列满、上游未配置、轮询超时）抛出 DetectionError，
        由工作循环决定重试；单条失败只影响该条，不牵连同批其他事件。
        """
        outcomes: dict[str, DetectionRecord | DetectionError] = {}
        if not pairs:
            return outcomes
        started = time.monotonic()
        for chunk in chunks(list(pairs)):
            try:
                task_id = self._submit(build_payload(chunk, self.mode))
                job = self._poll_task(task_id)
            except DetectionError:
                raise
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    raise DetectionError("UPSTREAM_BUSY", "上游任务队列已满")
                if status == 503:
                    raise DetectionError("UPSTREAM_NO_CONFIG", "上游真实模型配置缺失")
                raise DetectionError("UPSTREAM_UNREACHABLE", f"上游返回 {status}")
            except httpx.HTTPError as exc:
                raise DetectionError("UPSTREAM_UNREACHABLE", f"网络错误: {exc}")
            results = {
                item.get("id"): item
                for item in (job.get("results") or [])
                if isinstance(item, dict)
            }
            errors = {
                item.get("id"): item
                for item in (job.get("errors") or [])
                if isinstance(item, dict)
            }
            for event, _knowledge in chunk:
                if event.event_id in errors:
                    outcomes[event.event_id] = DetectionError(
                        "UPSTREAM_DETECTION_FAILED",
                        str(errors[event.event_id].get("message") or "上游检测执行失败"),
                    )
                    continue
                item = results.get(event.event_id)
                if item is None:
                    outcomes[event.event_id] = DetectionError(
                        "UPSTREAM_DETECTION_FAILED",
                        f"上游任务 {job.get('status')}，但未找到本条结果",
                    )
                    continue
                try:
                    record = to_record(item, self.detector_name)
                except DetectionError as exc:
                    outcomes[event.event_id] = exc
                    continue
                record.event_id = event.event_id
                record.latency_ms = int((time.monotonic() - started) * 1000)
                outcomes[event.event_id] = record
        return outcomes

    def _submit(self, payload: dict) -> str:
        response = self.client.post("/api/checks", json=payload)
        response.raise_for_status()
        body = response.json()
        task_id = body.get("task_id")
        if not task_id:
            raise DetectionError("UPSTREAM_UNREACHABLE", "上游未返回 task_id")
        return task_id

    def _poll_task(self, task_id: str) -> dict:
        """轮询到终态返回任务体；超时抛 UPSTREAM_TIMEOUT。"""
        for _ in range(POLL_MAX_ATTEMPTS):
            response = self.client.get(f"/api/checks/{task_id}")
            response.raise_for_status()
            job = response.json()
            if job.get("status") in TERMINAL_STATES:
                return job
            time.sleep(POLL_INTERVAL_SECONDS)
        raise DetectionError("UPSTREAM_TIMEOUT", "轮询超时，上游未给出结果")