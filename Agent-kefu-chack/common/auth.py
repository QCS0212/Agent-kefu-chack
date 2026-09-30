"""接入鉴权、限流与每日配额（批量检测服务与监测平台共用）。

企业接入形态：调用方持有 API Key，Key 绑定租户、可用范围（scope）、每分钟限流与每日配额。

Key 配置格式（环境变量）。因 scopes 内部用逗号，完整规格之间用分号分隔：

    <token>|<tenant>|<scopes>|<daily_quota>|<rate_per_minute>

例如：

    API_KEYS=sk-cs-9f2a|customer-service|detect,read|2000|120;sk-qa-1c33|qa|detect,read,admin|0|600

- scopes：detect / read / admin / ingest，`*` 表示全部
- daily_quota：0 表示不限；rate_per_minute：0 表示不限
- 兼容旧格式 `API_KEYS=t1,t2`（纯令牌、逗号分隔）：等价于 tenant=default、全部 scope、不限额度

未配置任何 Key 时视为本机开发模式，由各服务决定是否只允许回环地址。
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

ALL_SCOPES = frozenset({"detect", "read", "admin", "ingest"})
ADMIN = "admin"


class AuthError(Exception):
    """鉴权/限流/配额失败。status 用于 HTTP 响应，code 供日志与客户端判断。"""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


@dataclass(frozen=True)
class Principal:
    """一次调用的身份：Key 标识、租户、可用范围与额度。"""

    key_id: str
    tenant: str
    scopes: frozenset[str] = ALL_SCOPES
    daily_quota: int = 0
    rate_per_minute: int = 0
    anonymous: bool = False

    def has(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes or ADMIN in self.scopes

    def masked(self) -> dict:
        return {
            "key_id": self.key_id,
            "tenant": self.tenant,
            "scopes": sorted(self.scopes),
            "daily_quota": self.daily_quota,
            "rate_per_minute": self.rate_per_minute,
            "anonymous": self.anonymous,
        }


ANONYMOUS = Principal(key_id="anonymous", tenant="default", scopes=ALL_SCOPES, anonymous=True)


def _parse_scopes(raw: str) -> frozenset[str]:
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items or "*" in items:
        return ALL_SCOPES
    return frozenset(items)


def _parse_int(raw: str, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def parse_key_specs(raw: str) -> dict[str, Principal]:
    """解析 API Key 配置，返回 token -> Principal。支持新旧两种格式。"""
    keys: dict[str, Principal] = {}
    raw = (raw or "").strip()
    if not raw:
        return keys
    # 含 | 的完整规格用分号（或换行）分隔，因为 scopes 内部使用逗号；否则按逗号分隔纯令牌
    chunks = re.split(r"[;\n]+", raw) if "|" in raw else raw.split(",")
    for chunk in chunks:
        spec = chunk.strip()
        if not spec:
            continue
        if "|" in spec:
            parts = [item.strip() for item in spec.split("|")]
            token = parts[0]
            tenant = parts[1] if len(parts) > 1 and parts[1] else "default"
            scopes = _parse_scopes(parts[2]) if len(parts) > 2 else ALL_SCOPES
            quota = _parse_int(parts[3]) if len(parts) > 3 else 0
            rate = _parse_int(parts[4]) if len(parts) > 4 else 0
        else:
            token, tenant, scopes, quota, rate = spec, "default", ALL_SCOPES, 0, 0
        if token:
            keys[token] = Principal(
                key_id=f"{token[:6]}***",
                tenant=tenant,
                scopes=scopes,
                daily_quota=quota,
                rate_per_minute=rate,
            )
    return keys


def extract_token(authorization: str | None, x_token: str | None) -> str | None:
    """支持 Authorization: Bearer 与 X-Api-Key / X-Monitor-Token 两种携带方式。"""
    if x_token and x_token.strip():
        return x_token.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return None


class Authenticator:
    """常量时间比较的 Key 校验，并检查 scope。"""

    def __init__(self, keys: dict[str, Principal], scope: str, require: bool, enforce: bool = True):
        self.keys = keys
        self.scope = scope
        self.require = require
        # enforce=False 仅用于本机开放模式（本地读接口）：不带凭据时按匿名处理，带凭据仍严格校验
        self.enforce = enforce

    @property
    def configured(self) -> bool:
        return bool(self.keys)

    @property
    def strict(self) -> bool:
        """要求鉴权但没有可用 Key：属于配置错误，必须拒绝而不是放行。"""
        return self.require and not self.keys

    def authenticate(self, authorization: str | None, x_token: str | None) -> Principal:
        if self.strict:
            raise AuthError(503, "AUTH_NOT_CONFIGURED", "已要求鉴权但未配置任何 API Key")
        token = extract_token(authorization, x_token)
        if not token:
            if self.enforce and self.keys:
                raise AuthError(
                    401,
                    "MISSING_TOKEN",
                    "缺少 API Key",
                    {"WWW-Authenticate": "Bearer"},
                )
            return ANONYMOUS
        for candidate, principal in self.keys.items():
            if hmac.compare_digest(token, candidate):
                if not principal.has(self.scope):
                    raise AuthError(403, "SCOPE_DENIED", f"该 API Key 没有 {self.scope} 权限")
                return principal
        raise AuthError(401, "INVALID_TOKEN", "API Key 无效")


class RateLimiter:
    """按 Key 的滑动窗口限流。limit<=0 表示不限。"""

    def __init__(self, default_limit: int = 0):
        self.default_limit = max(0, default_limit)
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, principal: Principal) -> None:
        limit = principal.rate_per_minute or self.default_limit
        if limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(principal.key_id, deque())
            cutoff = now - 60.0
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= limit:
                raise AuthError(
                    429,
                    "RATE_LIMITED",
                    f"超过每分钟 {limit} 次调用限制",
                    {"Retry-After": "60"},
                )
            window.append(now)


class QuotaTracker:
    """按 Key 的每日配额，JSON 落盘，进程重启后仍然连续。"""

    def __init__(self, path: str | Path, default_quota: int = 0):
        self.path = Path(path)
        self.default_quota = max(0, default_quota)
        self._lock = threading.Lock()
        self._state = self._load()

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load(self) -> dict:
        today = self._today()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("date") == today:
                    return {"date": today, "counts": {str(k): int(v) for k, v in (data.get("counts") or {}).items()}}
            except (OSError, ValueError, TypeError):
                pass
        return {"date": today, "counts": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def usage(self, principal: Principal) -> int:
        with self._lock:
            return int(self._state["counts"].get(principal.key_id, 0))

    def limit_of(self, principal: Principal) -> int:
        return principal.daily_quota or self.default_quota

    def reserve(self, principal: Principal, cost: int = 1) -> None:
        """预占配额；超出时抛 429。cost<=0 或额度为 0（不限）时直接放行。"""
        limit = self.limit_of(principal)
        if limit <= 0 or cost <= 0:
            return
        with self._lock:
            if self._state["date"] != self._today():
                self._state = {"date": self._today(), "counts": {}}
            used = int(self._state["counts"].get(principal.key_id, 0))
            if used + cost > limit:
                raise AuthError(
                    429,
                    "QUOTA_EXCEEDED",
                    f"今日配额 {limit} 已用尽（已用 {used}）",
                    {"Retry-After": "3600"},
                )
            self._state["counts"][principal.key_id] = used + cost
            self._save()

    def snapshot(self, principal: Principal) -> dict:
        limit = self.limit_of(principal)
        used = self.usage(principal)
        return {
            "date": self._today(),
            "used": used,
            "limit": limit or None,
            "remaining": (limit - used) if limit else None,
        }