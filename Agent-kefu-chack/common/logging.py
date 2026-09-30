"""结构化日志与请求 ID。

JSON 日志便于企业日志系统（ELK / Loki / CloudWatch）直接采集；
请求 ID 会写入响应头 X-Request-ID，并注入每条日志。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid4().hex


def set_request_id(value: str | None = None) -> str:
    request_id = (value or "").strip() or new_request_id()
    _request_id.set(request_id)
    return request_id


def get_request_id() -> str:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON。"""

    def __init__(self, service: str = "service"):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "request_id": get_request_id(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", service: str = "service", json_logs: bool = True) -> None:
    """初始化根日志。json_logs=False 时退化为易读文本，便于本地开发。

    LOG_CONFIGURE=0 时跳过（测试或由外部平台统一配置日志时使用）。
    """
    import os

    if (os.environ.get("LOG_CONFIGURE") or "1").strip().lower() in ("0", "false", "no", "off"):
        # 交由调用方自行配置；加一个空 handler，避免 logging 的 lastResort 把日志打到 stderr
        if not logging.getLogger().handlers:
            logging.getLogger().addHandler(logging.NullHandler())
        return
    try:  # Windows 控制台默认 cp936，统一成 UTF-8 避免中文日志乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 非文本流
        pass
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def log_extra(**fields: Any) -> dict:
    """构造 logging 的 extra 字段。"""
    return {"extra_fields": fields}