"""监测平台 API。

对外系统：POST /api/ingest/events 事件接入（核心交付）。
对内运维：/api/monitor/* 只读统计、/metrics（Prometheus）、/health。

安全默认：默认只绑 127.0.0.1；接入接口要求 Bearer/X-Monitor-Token，
未配置令牌时拒绝接入而不是裸奔；读接口可用 MONITOR_PANEL_TOKEN 可选保护。
"""
from __future__ import annotations

import hmac
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .config import Settings, load_settings
from .metrics import Metrics
from .models import EventDocument, IngestAck, IngestEvent
from .sampling import SamplingPolicy
from .store import Store
from .worker import MonitorWorker, build_worker


def _extract_token(authorization: str | None, x_monitor_token: str | None) -> str | None:
    """支持 Authorization: Bearer 与 X-Monitor-Token 两种方式。"""
    if x_monitor_token and x_monitor_token.strip():
        return x_monitor_token.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return None


class RateLimiter:
    """按调用方的滑动窗口限流，保护接入接口不被打爆。"""

    def __init__(self, per_minute: int):
        self.per_minute = max(0, per_minute)
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            cutoff = now - 60.0
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= self.per_minute:
                return False
            window.append(now)
            return True


def _normalize_events(payload: Any) -> tuple[list[Any], bool]:
    """接受单条、数组或 {events: [...]} 三种形态，返回事件列表与是否批量。"""
    if isinstance(payload, dict) and "events" in payload:
        items = payload["events"]
        if not isinstance(items, list):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "events 字段必须是数组")
        return items, True
    if isinstance(payload, list):
        return payload, True
    if isinstance(payload, dict):
        return [payload], False
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "请求体必须是事件对象、事件数组或 {events: [...]}",
    )


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        "{}: {}".format(".".join(str(p) for p in err["loc"]) or "root", err["msg"])
        for err in exc.errors()
    )


class ReviewRequest(BaseModel):
    """人工复核结论：confirmed=确认是幻觉，rejected=误报。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["confirmed", "rejected"]
    note: str | None = Field(default=None, max_length=500)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    worker: MonitorWorker | None = app.state.worker
    stop_event = app.state.stop_event
    if worker is not None:
        thread = threading.Thread(
            target=worker.run_loop, args=(stop_event,), daemon=True, name="monitor-worker"
        )
        thread.start()
        app.state.worker_thread = thread
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=10)
            worker.close()
    else:
        yield
    app.state.store.close()


def create_app(settings: Settings | None = None, worker: bool = True) -> FastAPI:
    """构建监测平台应用。worker=False 用于测试，不启动后台检测线程。"""
    settings = settings or load_settings()
    store = Store(settings.db_path)
    sampling = SamplingPolicy(rate=settings.sample_rate, always_sample=settings.always_sample)
    rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    monitor_worker = build_worker(settings, store) if worker else None

    app = FastAPI(
        title="客服幻觉监测平台",
        version=__version__,
        lifespan=_lifespan,
        description="接入外部系统会话事件，持续监测客服回复幻觉。",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.sampling = sampling
    app.state.rate_limiter = rate_limiter
    app.state.worker = monitor_worker
    app.state.stop_event = threading.Event()
    app.state.worker_thread = None

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Monitor-Token"],
        )

    def ingest_auth(
        authorization: str | None = Header(default=None),
        x_monitor_token: str | None = Header(default=None, alias="X-Monitor-Token"),
    ) -> str:
        """接入鉴权：常量时间比较；要求鉴权但未配置令牌时拒绝服务。"""
        if settings.auth_strict:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "MONITOR_NOT_CONFIGURED：已要求鉴权但未配置 MONITOR_API_TOKENS",
            )
        if not settings.auth_enabled:
            return "anonymous"
        token = _extract_token(authorization, x_monitor_token)
        if not token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "MISSING_TOKEN：缺少接入令牌",
                headers={"WWW-Authenticate": "Bearer"},
            )
        for candidate in settings.api_tokens:
            if hmac.compare_digest(token, candidate):
                return f"{token[:6]}***"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN：接入令牌无效")

    def panel_auth(
        authorization: str | None = Header(default=None),
        x_monitor_token: str | None = Header(default=None, alias="X-Monitor-Token"),
    ) -> str:
        """读接口保护：未配面板令牌时仅允许本机绑定，非本机绑定必须配置令牌。"""
        if settings.read_auth_strict:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "MONITOR_NOT_CONFIGURED：监听地址不是本机时必须配置 MONITOR_PANEL_TOKEN",
            )
        if not settings.panel_token:
            return "anonymous"
        token = _extract_token(authorization, x_monitor_token)
        if not token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "MISSING_TOKEN：缺少面板令牌",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if hmac.compare_digest(token, settings.panel_token):
            return "panel***"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_TOKEN：面板令牌无效")

    @app.get("/", tags=["meta"])
    async def root():
        return {
            "service": "客服幻觉监测平台",
            "version": __version__,
            "ingest": "POST /api/ingest/events",
            "read": "GET /api/monitor/*",
            "review": "POST /api/monitor/events/{event_id}/review",
            "delete": "DELETE /api/monitor/events/{event_id}",
            "metrics": "GET /metrics",
            "health": "GET /health",
        }

    @app.post(
        "/api/ingest/events",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["ingest"],
        summary="外部系统接入事件",
    )
    async def ingest_events(
        request: Request,
        payload: Any = Body(...),
        caller: str = Depends(ingest_auth),
    ):
        """接入一条或多条客服会话事件，异步检测；同一事件重复接入幂等返回。"""
        if not rate_limiter.allow(caller):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "RATE_LIMITED：接入频率超出限制",
                headers={"Retry-After": "60"},
            )
        items, is_batch = _normalize_events(payload)
        limit = settings.max_batch_items
        if limit and len(items) > limit:
            raise HTTPException(
                413,
                f"单次最多接入 {limit} 条事件，当前 {len(items)} 条；请分批提交",
            )
        accepted: list[IngestAck] = []
        rejected: list[dict] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                detail = f"第 {index} 项不是 JSON 对象"
                if not is_batch:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)
                rejected.append({"index": index, "error": detail})
                continue
            try:
                event = IngestEvent(**item)
            except ValidationError as exc:
                detail = _format_validation_error(exc)
                if not is_batch:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)
                rejected.append({"index": index, "error": detail})
                continue
            sampled = sampling.should_sample(event)
            event_id, created = store.ingest(event, sampled)
            if created:
                event_status = "sampled" if sampled else "skipped"
            else:
                existing = store.get(event_id)
                event_status = existing.status if existing else "stored"
            accepted.append(
                IngestAck(
                    event_id=event_id,
                    status=event_status,
                    sampled=sampled,
                    duplicate=not created,
                    status_url=str(request.url_for("get_event", event_id=event_id)),
                )
            )
        if not is_batch:
            return accepted[0] if accepted else None
        return {"accepted": accepted, "rejected": rejected}

    @app.get("/api/monitor/stats", tags=["read"], summary="整体统计")
    async def get_stats(days: int | None = None, _caller: str = Depends(panel_auth)):
        return store.stats(max(1, min(days, 365)) if days else None)

    @app.get("/api/monitor/timeseries", tags=["read"], summary="趋势时间序列")
    async def get_timeseries(days: int = 7, _caller: str = Depends(panel_auth)):
        return store.timeseries(days)

    @app.get("/api/monitor/knowledge-gaps", tags=["read"], summary="知识缺口榜")
    async def get_knowledge_gaps(limit: int = 10, _caller: str = Depends(panel_auth)):
        return store.knowledge_gaps(max(1, min(limit, 200)))

    @app.get("/api/monitor/events", tags=["read"], summary="事件流")
    async def list_events(
        limit: int = 50,
        offset: int = 0,
        status_filter: str | None = None,
        verdict: str | None = None,
        category: str | None = None,
        needs_review: bool | None = None,
        review_status: str | None = None,
        _caller: str = Depends(panel_auth),
    ):
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        result = store.list_events(
            limit=limit,
            offset=offset,
            status=status_filter,
            verdict=verdict,
            category=category,
            needs_review=needs_review,
            review_status=review_status,
        )
        return {
            "total": result["total"],
            "limit": limit,
            "offset": offset,
            "items": [item.model_dump(mode="json") for item in result["items"]],
        }

    @app.get(
        "/api/monitor/events/{event_id}",
        tags=["read"],
        name="get_event",
        response_model=EventDocument,
        summary="事件详情与检测结果",
    )
    async def get_event(event_id: str, _caller: str = Depends(panel_auth)):
        doc = store.get(event_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND：事件不存在")
        return doc

    @app.get("/api/monitor/config", tags=["read"], summary="脱敏后的运行配置")
    async def get_config(_caller: str = Depends(panel_auth)):
        return settings.masked()

    @app.post(
        "/api/monitor/events/{event_id}/review",
        tags=["review"],
        summary="提交人工复核结论",
    )
    async def review_event(
        event_id: str,
        body: ReviewRequest,
        _caller: str = Depends(panel_auth),
    ):
        """确认是幻觉（confirmed）或标记误报（rejected），用于统计复核后精确率。"""
        if not store.set_review(event_id, body.status, body.note):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND：事件不存在")
        doc = store.get(event_id)
        return {
            "event_id": event_id,
            "review_status": doc.review_status if doc else body.status,
            "review_note": doc.review_note if doc else body.note,
            "reviewed_at": doc.reviewed_at if doc else None,
        }

    @app.delete(
        "/api/monitor/events/{event_id}",
        tags=["review"],
        summary="删除事件（个人信息删除请求）",
    )
    async def delete_event(event_id: str, _caller: str = Depends(panel_auth)):
        if not store.delete_event(event_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND：事件不存在")
        return {"event_id": event_id, "deleted": True}

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标")
    async def get_metrics():
        worker = app.state.worker
        metrics = worker.metrics if worker is not None else Metrics()
        return PlainTextResponse(
            metrics.render(store),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/health", tags=["ops"], summary="健康检查")
    async def get_health():
        worker = app.state.worker
        stats = store.stats()
        upstream = worker.upstream.probe() if worker is not None else {"status": "disabled"}
        return {
            "status": "ok",
            "version": __version__,
            "worker": "running" if worker is not None else "disabled",
            "queue_pending": stats["events"]["by_status"].get("pending", 0),
            "upstream": upstream,
        }

    return app
