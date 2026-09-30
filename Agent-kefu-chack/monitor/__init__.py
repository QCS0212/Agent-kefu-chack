"""客服幻觉监测平台。

在既有批量检测服务之上增加：外部系统事件接入、知识自动装配、抽样、
幂等存储、规则快检、告警路由与 Prometheus 指标。
"""
__version__ = "1.0.0"

__all__ = ["__version__"]

from monitor.config import Settings  # noqa: E402,F401
from monitor.models import IngestEvent  # noqa: E402,F401
