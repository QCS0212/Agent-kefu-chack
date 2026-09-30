"""环境变量驱动的配置。

优先读取 os.environ，其次读取项目根目录的 .env。不引入第三方配置库。
所有开关默认面向“本机安全”，要让外部系统调用必须显式配置令牌与监听地址。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_ENV_VALUES = _parse_env_file(ENV_FILE)


def _env(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name].strip()
    return _ENV_VALUES.get(name, default).strip()


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # 服务监听。默认仍只绑本机；要让外部系统调用需显式改为 0.0.0.0 并配置令牌
    bind_host: str = field(default_factory=lambda: _env("MONITOR_BIND_HOST", "127.0.0.1"))
    bind_port: int = field(default_factory=lambda: _env_int("MONITOR_BIND_PORT", 8010))

    # 既有批量检测服务（FastAPI，127.0.0.1:8000）。监测平台把装配好的知识随单条任务转发过去
    upstream_url: str = field(
        default_factory=lambda: _env("MONITOR_UPSTREAM_URL", "http://127.0.0.1:8000").rstrip("/")
    )
    upstream_mode: str = field(default_factory=lambda: _env("MONITOR_UPSTREAM_MODE", "mock"))

    # 外部系统接入鉴权。未配置令牌时拒绝所有接入请求，避免裸奔上线
    api_tokens: tuple[str, ...] = field(default_factory=lambda: tuple(_env_list("MONITOR_API_TOKENS")))
    require_auth: bool = field(default_factory=lambda: _env("MONITOR_REQUIRE_AUTH", "1") == "1")
    rate_limit_per_minute: int = field(default_factory=lambda: _env_int("MONITOR_RATE_LIMIT", 120))

    # 持久化与知识
    db_path: str = field(default_factory=lambda: _env("MONITOR_DB", str(ROOT / "data" / "monitor.db")))
    knowledge_dir: str = field(
        default_factory=lambda: _env("MONITOR_KNOWLEDGE_DIR", str(ROOT / "knowledge"))
    )

    # 抽样：高风险类目全量，其余按概率。哈希抽样保证同一会话结论稳定
    sample_rate: float = field(default_factory=lambda: _env_float("MONITOR_SAMPLE_RATE", "1.0"))
    always_sample: tuple[str, ...] = field(
        default_factory=lambda: tuple(_env_list("MONITOR_ALWAYS_SAMPLE", "after_sale,payment,product"))
    )

    # 规则快检：结构性矛盾（虚假执行、地址泄露）直接出结论，不调用模型
    rule_fast_check: bool = field(default_factory=lambda: _env("MONITOR_RULE_FAST_CHECK", "1") == "1")

    # 告警
    alert_webhook_url: str = field(default_factory=lambda: _env("MONITOR_ALERT_WEBHOOK_URL"))
    alert_secret: str = field(default_factory=lambda: _env("MONITOR_ALERT_SECRET"))
    alert_min_severity: str = field(default_factory=lambda: _env("MONITOR_ALERT_MIN_SEVERITY", "medium"))
    alert_dedup_minutes: int = field(default_factory=lambda: _env_int("MONITOR_ALERT_DEDUP_MINUTES", 30))
    quiet_hours: str = field(default_factory=lambda: _env("MONITOR_QUIET_HOURS"))  # 如 "22:00-08:00"

    # 工作循环
    worker_interval_seconds: int = field(default_factory=lambda: _env_int("MONITOR_WORKER_INTERVAL", 5))
    worker_batch: int = field(default_factory=lambda: _env_int("MONITOR_WORKER_BATCH", 20))

    # 接入上限：单次请求最多接收的事件数，0=不限（默认 200，防止一条请求压垮存储）
    max_batch_items: int = field(default_factory=lambda: _env_int("MONITOR_MAX_BATCH_ITEMS", 200))

    # 上游失败重试：最大尝试次数与指数退避基数（秒）
    retry_max_attempts: int = field(default_factory=lambda: _env_int("MONITOR_RETRY_MAX_ATTEMPTS", 3))
    retry_backoff_seconds: int = field(default_factory=lambda: _env_int("MONITOR_RETRY_BACKOFF_SECONDS", 30))

    # 会话数据保留天数，0=永久保留；超期数据由工作循环定期清理
    retention_days: int = field(default_factory=lambda: _env_int("MONITOR_RETENTION_DAYS", 30))

    # 前端面板与只读接口的可选保护；留空时读接口不鉴权（默认只绑本机）
    panel_token: str = field(default_factory=lambda: _env("MONITOR_PANEL_TOKEN"))

    # 前端面板联调用，留空则只允许本机 vite 代理访问
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(_env_list("MONITOR_CORS_ORIGINS")))

    @property
    def auth_strict(self) -> bool:
        """要求鉴权但未配置令牌：视为服务端配置错误，拒绝接入而不是放行。"""
        return self.require_auth and not self.api_tokens

    @property
    def auth_enabled(self) -> bool:
        return self.require_auth and bool(self.api_tokens)

    @property
    def loopback_only(self) -> bool:
        return self.bind_host in ("127.0.0.1", "localhost", "::1")

    @property
    def read_auth_strict(self) -> bool:
        """非本机绑定时读接口必须配置面板令牌，否则拒绝服务。

        读接口会返回会话原文，绑到 0.0.0.0 又不设令牌等于把客诉数据公开。
        """
        return not self.loopback_only and not self.panel_token

    def masked(self) -> dict:
        """给 /api/monitor/config 用，不暴露任何密钥。"""
        return {
            "bind_host": self.bind_host,
            "bind_port": self.bind_port,
            "loopback_only": self.loopback_only,
            "read_auth_strict": self.read_auth_strict,
            "max_batch_items": self.max_batch_items,
            "retry_max_attempts": self.retry_max_attempts,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "retention_days": self.retention_days,
            "worker_batch": self.worker_batch,
            "upstream_url": self.upstream_url,
            "upstream_mode": self.upstream_mode,
            "auth_required": self.require_auth,
            "auth_configured": bool(self.api_tokens),
            "panel_auth_configured": bool(self.panel_token),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "sample_rate": self.sample_rate,
            "always_sample": list(self.always_sample),
            "rule_fast_check": self.rule_fast_check,
            "alert_min_severity": self.alert_min_severity,
            "alert_dedup_minutes": self.alert_dedup_minutes,
            "quiet_hours": self.quiet_hours or None,
            "worker_interval_seconds": self.worker_interval_seconds,
        }


def load_settings() -> Settings:
    return Settings()
