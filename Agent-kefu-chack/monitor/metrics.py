"""Prometheus 文本格式指标。

不依赖 prometheus_client：快照指标在请求 /metrics 时从 store 实时聚合，
接入量与上游调用为内存计数器，由接入接口与工作循环递增。
"""
from __future__ import annotations

import threading

from .store import Store

HELP = {
    "monitor_events_total": "接入事件数（按状态）",
    "monitor_sampled_total": "进入抽样检测的事件数",
    "monitor_detections_total": "检测结果数（按结论）",
    "monitor_hallucination_rate": "幻觉率（0~1，分母只含已给出明确结论的事件）",
    "monitor_coverage": "明确结论覆盖率（明确结论数 / 已检测数）",
    "monitor_degraded_rate": "降级率（不可核验数 / 已检测数）",
    "monitor_fallback_total": "上游失败降级为不可核验的累计条数（来自存储）",
    "monitor_retry_total": "失败重试调度计数",
    "monitor_unresolved_total": "重试耗尽仍未完成的计数",
    "monitor_review_total": "人工复核状态计数",
    "monitor_severity_total": "幻觉严重程度分布",
    "monitor_type_total": "幻觉类型分布",
    "monitor_detection_latency_ms": "检测耗时（毫秒，平均值）",
    "monitor_alerts_total": "告警数（按状态）",
    "monitor_ingest_requests_total": "接入请求计数（按结果）",
    "monitor_upstream_calls_total": "上游调用计数（按结果）",
}


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _line(name: str, labels: dict | None, value) -> str:
    if labels:
        pairs = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        return f"{name}{{{pairs}}} {value}"
    return f"{name} {value}"


class Metrics:
    """内存计数器：接入与上游调用的维度统计。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, float]] = {}

    def inc(self, name: str, **labels) -> None:
        key = _line(name, labels, 0).rsplit(" ", 1)[0]
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0.0) + 1.0

    def counters(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {name: dict(bucket) for name, bucket in self._counters.items()}

    def render(self, store: Store) -> str:
        stats = store.stats()
        lines: list[str] = []
        for name, help_text in HELP.items():
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            if name == "monitor_events_total":
                by_status = stats["events"]["by_status"]
                if not by_status:
                    by_status = {"pending": 0}
                for status, value in sorted(by_status.items()):
                    lines.append(_line(name, {"status": status}, value))
            elif name == "monitor_sampled_total":
                lines.append(_line(name, None, stats["events"]["sampled"]))
            elif name == "monitor_detections_total":
                by_verdict = stats["detections"]["by_verdict"] or {"not_verifiable": 0}
                for verdict, value in sorted(by_verdict.items()):
                    lines.append(_line(name, {"verdict": verdict}, value))
            elif name == "monitor_hallucination_rate":
                lines.append(_line(name, None, stats["hallucination_rate"] or 0))
            elif name == "monitor_coverage":
                lines.append(_line(name, None, stats["coverage"] or 0))
            elif name == "monitor_degraded_rate":
                lines.append(_line(name, None, stats["degraded_rate"] or 0))
            elif name == "monitor_review_total":
                for status, value in sorted((stats["review"] or {"pending": 0}).items()):
                    lines.append(_line(name, {"status": status}, value))
            elif name == "monitor_fallback_total":
                # 来自存储，重启后仍然连续可用
                lines.append(_line(name, None, stats["detections"]["fallback"]))
            elif name in ("monitor_retry_total", "monitor_unresolved_total"):
                counters = self.counters().get(name, {})
                if counters:
                    for line_key, value in sorted(counters.items()):
                        lines.append(f"{line_key} {value:g}")
                else:
                    lines.append(_line(name, {"code": "none"}, 0))
            elif name == "monitor_severity_total":
                for severity, value in sorted(stats["detections"]["by_severity"].items()):
                    lines.append(_line(name, {"severity": severity}, value))
            elif name == "monitor_type_total":
                for category, value in stats["detections"]["by_type"].items():
                    lines.append(_line(name, {"type": category}, value))
            elif name == "monitor_detection_latency_ms":
                lines.append(_line(name, {"quantile": "avg"}, stats["detections"]["latency_avg_ms"]))
                lines.append(_line(name, {"quantile": "p50"}, stats["detections"]["latency_p50_ms"]))
                lines.append(_line(name, {"quantile": "p95"}, stats["detections"]["latency_p95_ms"]))
            elif name == "monitor_alerts_total":
                by_status = stats["alerts"] or {"stored": 0}
                for status, value in sorted(by_status.items()):
                    lines.append(_line(name, {"status": status}, value))
            elif name in ("monitor_ingest_requests_total", "monitor_upstream_calls_total"):
                counters = self.counters().get(name, {})
                if counters:
                    for line_key, value in sorted(counters.items()):
                        lines.append(f"{line_key} {value:g}")
                else:
                    lines.append(_line(name, {"result": "none"}, 0))
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"
