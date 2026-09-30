"""抽样策略。

全量 LLM 检测成本不可控，按类目风险分级抽样。用会话哈希而非随机数，
保证同一会话重复接入时结论一致，幂等层才谈得上“同一事件不重复检测”。
"""
from __future__ import annotations



from hashlib import sha256

from .config import load_settings
from .models import IngestEvent


class SamplingPolicy:
    def __init__(self, rate: float = 1.0, always_sample: tuple[str, ...] = ()):
        if not 0.0 <= rate <= 1.0:
            raise ValueError("抽样率必须在 0~1 之间")
        self.rate = rate
        self.always_sample = {c.lower() for c in always_sample}

    def should_sample(self, event: IngestEvent) -> bool:
        if (event.category or "").lower() in self.always_sample:
            return True
        if self.rate >= 1.0:
            return True
        if self.rate <= 0.0:
            return False
        digest = sha256(f"{event.session_id}|{event.store_id or ''}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF < self.rate

    def reason(self, event: IngestEvent) -> str:
        if (event.category or "").lower() in self.always_sample:
            return "high_risk_category"
        if self.should_sample(event):
            return "sampled"
        return "skipped"


def build_policy_from_settings() -> SamplingPolicy:
    settings = load_settings()
    return SamplingPolicy(rate=settings.sample_rate, always_sample=settings.always_sample)
