"""批量检测服务的企业接入层：运行配置、鉴权装配、幂等与指标。"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from common.auth import Authenticator, Principal, QuotaTracker, RateLimiter, parse_key_specs

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173")


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
_ENV_FILE_VALUES: dict[str, str] = {}


def _load_env_file(path: Path) -> dict[str, str]:
    """读取仓库根目录 .env 作为兜底（环境变量优先），便于本地直接起服务。"""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values


def _env(name: str, default: str = "") -> str:
    if name in os.environ:
        return (os.environ[name] or "").strip()
    if not _ENV_FILE_VALUES:
        _ENV_FILE_VALUES.update(_load_env_file(ENV_FILE))
    value = _ENV_FILE_VALUES.get(name)
    return (value if value is not None else default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ApiSettings:
    """来自环境变量（前缀 API_）的运行配置。"""

    bind_host: str = "127.0.0.1"
    port: int = 8000
    keys_raw: str = ""
    max_batch: int = 20
    max_running: int = 2
    max_queued: int = 8
    rate_per_minute: int = 120
    daily_quota: int = 0
    idempotency_ttl_days: int = 7
    log_level: str = "INFO"
    log_json: bool = True
    allowed_origins: tuple[str, ...] = DEFAULT_ORIGINS

    @property
    def loopback_only(self) -> bool:
        return self.bind_host in LOOPBACK_HOSTS

    @property
    def auth_required(self) -> bool:
        """绑到非本机地址时必须鉴权；本机默认匿名可用，便于本地联调。"""
        return not self.loopback_only

    @property
    def keys(self) -> dict[str, Principal]:
        return parse_key_specs(self.keys_raw)

    def authenticator_for(self, scope: str) -> Authenticator:
        return Authenticator(self.keys, scope=scope, require=self.auth_required)

    def validate(self) -> None:
        """启动前校验：暴露到网络却没有任何 Key 属于配置错误，直接拒绝启动。"""
        if not self.loopback_only and not self.keys:
            raise RuntimeError(
                f"API_BIND_HOST={self.bind_host} 绑定了非本机地址，但未配置任何 API_KEY。"
                "请配置 API_KEYS（并建议配置 API_DAILY_QUOTA / API_RATE_LIMIT），或改回 127.0.0.1。"
            )
        if self.keys and self.authenticator_for("detect").strict:
            raise RuntimeError("已配置 API_KEYS 但解析为空，请检查格式：token|tenant|scopes|quota|rate")

    def masked(self) -> dict:
        return {
            "bind_host": self.bind_host,
            "port": self.port,
            "loopback_only": self.loopback_only,
            "auth_required": self.auth_required,
            "key_count": len(self.keys),
            "tenants": sorted({p.tenant for p in self.keys.values()}),
            "max_batch": self.max_batch,
            "max_running": self.max_running,
            "max_queued": self.max_queued,
            "rate_per_minute": self.rate_per_minute,
            "daily_quota": self.daily_quota,
            "idempotency_ttl_days": self.idempotency_ttl_days,
            "log_level": self.log_level,
            "log_json": self.log_json,
        }


def load_api_settings() -> ApiSettings:
    return ApiSettings(
        bind_host=_env("API_BIND_HOST", "127.0.0.1"),
        port=_env_int("API_PORT", 8000),
        keys_raw=_env("API_KEYS") or _env("API_KEY"),
        max_batch=max(1, _env_int("API_MAX_BATCH", 20)),
        max_running=max(1, _env_int("API_MAX_RUNNING", 2)),
        max_queued=max(1, _env_int("API_MAX_QUEUED", 8)),
        rate_per_minute=max(0, _env_int("API_RATE_LIMIT", 120)),
        daily_quota=max(0, _env_int("API_DAILY_QUOTA", 0)),
        idempotency_ttl_days=max(0, _env_int("API_IDEMPOTENCY_TTL_DAYS", 7)),
        log_level=_env("LOG_LEVEL", "INFO"),
        log_json=_env_bool("LOG_JSON", True),
        allowed_origins=tuple(
            origin.strip()
            for origin in (_env("API_CORS_ORIGINS") or ",".join(DEFAULT_ORIGINS)).split(",")
            if origin.strip()
        ),
    )


class IdempotencyStore:
    """Idempotency-Key -> task_id 的落盘映射，按租户隔离，避免重复提交重复计费。"""

    def __init__(self, path: str | Path, ttl_days: int = 7):
        self.path = Path(path)
        self.ttl = timedelta(days=ttl_days) if ttl_days else None
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def _key(self, tenant: str, key: str) -> str:
        return f"{tenant}::{key}"

    def find(self, tenant: str, key: str) -> dict | None:
        """返回未过期的幂等记录；命中过期记录时顺手清理。"""
        with self._lock:
            entry = self._state.get(self._key(tenant, key))
            if not entry:
                return None
            created = entry.get("created_at")
            if self.ttl and created:
                try:
                    if datetime.now(timezone.utc) - datetime.fromisoformat(created) > self.ttl:
                        self._state.pop(self._key(tenant, key), None)
                        self._save()
                        return None
                except ValueError:
                    pass
            return dict(entry)

    def save(self, tenant: str, key: str, task_id: str, fingerprint: str | None = None) -> None:
        with self._lock:
            self._state[self._key(tenant, key)] = {
                "task_id": task_id,
                "fingerprint": fingerprint,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save()

    def get(self, tenant: str, key: str) -> str | None:
        entry = self.find(tenant, key)
        return entry.get("task_id") if entry else None

    def put(self, tenant: str, key: str, task_id: str) -> None:
        self.save(tenant, key, task_id)


class ServiceMetrics:
    """Prometheus 文本指标：请求、鉴权失败、限流、配额与任务状态。"""

    HELP = {
        "api_requests_total": "HTTP 请求数（按路由与状态码）",
        "api_auth_failures_total": "鉴权/限流/配额拒绝数（按结果）",
        "api_items_submitted_total": "提交检测的样本条数",
        "api_items_failed_total": "检测技术失败条数",
        "api_tasks": "任务数（按状态）",
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, float]] = {}

    def inc(self, name: str, **labels) -> None:
        label_text = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        key = f"{name}{{{label_text}}}" if label_text else name
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0.0) + 1.0

    def render(self, store) -> str:
        lines: list[str] = []
        counters = {name: dict(bucket) for name, bucket in self._counters.items()}
        for name, help_text in self.HELP.items():
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            if name == "api_tasks":
                for status, count in sorted(store.status_counts().items()):
                    lines.append(f'{name}{{status="{status}"}} {count}')
                continue
            bucket = counters.get(name) or {}
            if not bucket:
                lines.append(f"{name} 0")
                continue
            for key, value in sorted(bucket.items()):
                lines.append(f"{key} {value:g}")
            lines.append("")
        return "\n".join(line for line in lines if line != "").rstrip("\n") + "\n"


class RequestLogMiddleware:
    """记录结构化访问日志，并回写 X-Request-ID。"""

    def __init__(self, app, logger, service: str = "kefu-check"):
        self.app = app
        self.logger = logger
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from common.logging import REQUEST_ID_HEADER, set_request_id

        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        request_id = set_request_id(headers.get(REQUEST_ID_HEADER.lower()))
        started = time.monotonic()
        status_code = {"value": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code["value"] = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            self.logger.info(
                "request",
                extra={
                    "extra_fields": {
                        "service": self.service,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status_code["value"],
                        "duration_ms": duration_ms,
                    }
                },
            )