from typing import Protocol

class Detector(Protocol):
    def detect(self, row: dict) -> dict: ...

class MockDetector:
    """Pipeline fixture, deliberately does not infer labels or access ground truth."""
    def detect(self, row):
        return {
            "id": row["id"],
            "verdict": "not_verifiable",
            "types": [],
            "severity": None,
            "claims": [],
            "reason": "MOCK：仅演示处理流程，尚未进行模型事实核验。",
        }

def get_detector(mode):
    if mode == "mock":
        return MockDetector()
    if mode == "llm":
        from app.llm import LLMDetector
        return LLMDetector()
    raise ValueError("未知检测模式")
