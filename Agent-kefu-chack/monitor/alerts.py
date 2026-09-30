"""告警路由。

high 立即推送 webhook；medium 聚合为日报；low 只入仓。带去重窗口抑制与夜间静默。
告警投递失败不影响检测结果落盘，告警表留痕便于审计。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, time as dt_time
from uuid import uuid4

import httpx

from .config import Settings
from .models import DetectionRecord, EventDocument
from .store import Store

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}
CHANNEL_WEBHOOK = "webhook"
CHANNEL_NONE = "none"
STATUS_SENT = "sent"
STATUS_QUEUED = "queued"
STATUS_STORED = "stored"
STATUS_SUPPRESSED = "suppressed"
STATUS_FAILED = "failed"
META_LAST_DIGEST = "last_digest_date"


def in_quiet_hours(window: str | None, moment: datetime | None = None) -> bool:
    """解析 "22:00-08:00" 这类跨午夜窗口，用本地时间判断。"""
    if not window:
        return False
    try:
        start_s, end_s = window.split("-", 1)
        start = dt_time.fromisoformat(start_s.strip())
        end = dt_time.fromisoformat(end_s.strip())
    except ValueError:
        return False
    moment = moment or datetime.now()
    current = moment.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def alert_fingerprint(event: EventDocument, record: DetectionRecord) -> str:
    """同一会话同一类幻觉在去重窗口内只告警一次。"""
    raw = f"{event.session_id}|{event.sku or ''}|{event.category or ''}|{','.join(sorted(record.types))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AlertRouter:
    def __init__(self, store: Store, settings: Settings, client: httpx.Client | None = None):
        self.store = store
        self.settings = settings
        self.client = client or httpx.Client(timeout=10.0)

    def close(self) -> None:
        self.client.close()

    @property
    def webhook_configured(self) -> bool:
        return bool(self.settings.alert_webhook_url)

    def route(self, event: EventDocument, record: DetectionRecord) -> str | None:
        """只对幻觉结论告警；返回告警状态供指标统计。"""
        if record.verdict != "hallucination":
            return None
        severity = record.severity or "medium"
        floor = self.settings.alert_min_severity
        if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(floor, 0):
            return None
        fingerprint = alert_fingerprint(event, record)
        channel = CHANNEL_WEBHOOK if self.webhook_configured else CHANNEL_NONE
        if severity == "high":
            return self._immediate(event, record, severity, fingerprint, channel)
        if severity == "medium":
            return self._queue(event, record, severity, fingerprint, channel)
        return self._store(event, record, severity, fingerprint, channel)

    def _immediate(self, event, record, severity, fingerprint, channel) -> str:
        if self.store.alert_seen_within(fingerprint, severity, self.settings.alert_dedup_minutes):
            self.store.record_alert(
                uuid4().hex, event.event_id, severity, channel, fingerprint, STATUS_SUPPRESSED
            )
            return STATUS_SUPPRESSED
        if not self.webhook_configured:
            return self._store(event, record, severity, fingerprint, channel)
        payload = self._payload(event, record, severity)
        ok, response = self._send(payload)
        status = STATUS_SENT if ok else STATUS_FAILED
        self.store.record_alert(
            uuid4().hex, event.event_id, severity, channel, fingerprint, status, payload
        )
        return status

    def _queue(self, event, record, severity, fingerprint, channel) -> str:
        status = STATUS_QUEUED if self.webhook_configured else STATUS_STORED
        self.store.record_alert(
            uuid4().hex, event.event_id, severity, channel, fingerprint, status,
            self._payload(event, record, severity) if self.webhook_configured else None,
        )
        return status

    def _store(self, event, record, severity, fingerprint, channel) -> str:
        self.store.record_alert(
            uuid4().hex, event.event_id, severity, channel, fingerprint, STATUS_STORED
        )
        return STATUS_STORED

    def _payload(self, event, record, severity) -> dict:
        return {
            "event_id": event.event_id,
            "session_id": event.session_id,
            "category": event.category,
            "sku": event.sku,
            "severity": severity,
            "verdict": record.verdict,
            "types": record.types,
            "reason": record.reason,
            "reply_excerpt": event.system_reply[:200],
            "claims": [c.model_dump(mode="json") for c in record.claims[:3]],
        }

    def _send(self, payload: dict) -> tuple[bool, str]:
        body = json.dumps(payload, ensure_ascii=False)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        secret = self.settings.alert_secret
        if secret:
            signature = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
            headers["X-Monitor-Signature"] = f"sha256={signature}"
        try:
            response = self.client.post(self.settings.alert_webhook_url, content=body.encode("utf-8"), headers=headers)
            ok = response.status_code < 400
            return ok, f"{response.status_code} {response.text[:200]}"
        except httpx.HTTPError as exc:
            return False, str(exc)

    def flush_digest(self, today: str | None = None) -> int:
        """发送一次日报：聚合所有 queued 告警。夜间静默时段顺延。"""
        today = today or datetime.now().date().isoformat()
        if self.store.get_meta(META_LAST_DIGEST) == today:
            return 0
        if in_quiet_hours(self.settings.quiet_hours):
            return 0
        rows = self.store.claim_digest()
        if not rows:
            self.store.set_meta(META_LAST_DIGEST, today)
            return 0
        summary = {
            "kind": "daily_digest",
            "date": today,
            "total": len(rows),
            "by_severity": {},
            "items": [],
        }
        for row in rows:
            severity = row["severity"]
            summary["by_severity"][severity] = summary["by_severity"].get(severity, 0) + 1
            try:
                summary["items"].append(json.loads(row["payload"] or "{}"))
            except json.JSONDecodeError:
                continue
        ok, response = self._send(summary)
        status = STATUS_SENT if ok else STATUS_FAILED
        for row in rows:
            self.store.mark_alert(row["alert_id"], status, response)
        if ok:
            self.store.set_meta(META_LAST_DIGEST, today)
        return len(rows)
