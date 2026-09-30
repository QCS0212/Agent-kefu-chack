"""监测平台 API（企业接入版）。

对外：POST /api/ingest/events 事件接入（需 ingest 权限）。
对内：/api/monitor/* 只读统计（按租户过滤）、复核、删除、告警重放；/metrics、/health、/readyz。

安全默认：默认只绑 127.0.0.1；接入接口必须鉴权（未配置令牌返回 503）；绑到非本机地址时读接口强制要求面板令牌。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.auth import ALL_SCOPES, AuthError, Principal, QuotaTracker, RateLimiter, Authenticator
from common.logging import REQUEST_ID_HEADER, configure_logging, set_request_id

from . import __version__
from .config import Settings, load_settings
from .metrics import Metrics
from .models import EventDocument, IngestAck, IngestEvent
from .sampling import SamplingPolicy
from .store import Store
from .worker import MonitorWorker, build_worker

logger = logging.getLogger("monitor.api")


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
            thread.join(timeout=20)
            worker.close()
    else:
        yield
    app.state.store.close()


def create_app(settings: Settings | None = None, worker: bool = True) -> FastAPI:
    """构建监测平台应用。worker=False 用于测试，不启动后台检测线程。"""
    settings = settings or load_settings()
    configure_logging(settings.log_level, service="monitor", json_logs=settings.log_json)
    store = Store(settings.db_path)
    sampling = SamplingPolicy(rate=settings.sample_rate, always_sample=settings.always_sample)
    rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    quota = QuotaTracker(store.path.parent / "monitor_quota.json", settings.daily_quota)
    monitor_worker = build_worker(settings, store) if worker else None

    ingest_auth = Authenticator(settings.api_keys, scope="ingest", require=settings.require_auth)
    read_keys = dict(settings.api_keys)
    if settings.panel_token:
        read_keys[settings.panel_token] = Principal(
            key_id="panel***", tenant="*", scopes=ALL_SCOPES
        )
    # 企业形态（非本机绑定 / 配置了面板令牌 / 显式开启）强制读接口鉴权；本机默认开放便于联调
    enforce_read = settings.require_read_auth or settings.read_auth_strict or bool(settings.panel_token)
    read_auth = Authenticator(read_keys, scope="read", require=False, enforce=enforce_read)
    admin_auth = Authenticator(read_keys, scope="admin", require=False, enforce=enforce_read)

    app = FastAPI(
        title="客服幻觉监测平台",
        version=__version__,
        lifespan=_lifespan,
        description=(
            "接入外部系统会话事件，持续监测客服回复幻觉。"
            "支持 API Key（租户/scope/配额）、人工复核闭环、批量上游检测与告警重试。"
        ),
    )
    app.state.settings = settings
    app.state.store = store
    app.state.sampling = sampling
    app.state.rate_limiter = rate_limiter
    app.state.quota = quota
    app.state.ingest_auth = ingest_auth
    app.state.read_auth = read_auth
    app.state.admin_auth = admin_auth
    app.state.worker = monitor_worker
    app.state.stop_event = threading.Event()
    app.state.worker_thread = None

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Monitor-Token", "X-Request-ID"],
        )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        request_id = set_request_id(request.headers.get(REQUEST_ID_HEADER))
        started = time.monotonic()
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        principal = getattr(request.state, "principal", None)
        logger.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "tenant": principal.tenant if principal else None,
                    "key_id": principal.key_id if principal else None,
                }
            },
        )
        return response

    # ---------------------------------------------------------------- 鉴权依赖

    def _credentials(authorization: str | None, x_monitor_token: str | None):
        return authorization, x_monitor_token

    def ingest_dependency(
        request: Request,
        authorization: str | None = Header(default=None),
        x_monitor_token: str | None = Header(default=None, alias="X-Monitor-Token"),
    ) -> Principal:
        try:
            principal = ingest_auth.authenticate(authorization, x_monitor_token)
            rate_limiter.check(principal)
        except AuthError as exc:
            raise HTTPException(exc.status, f"{exc.code}：{exc.message}", headers=exc.headers) from None
        request.state.principal = principal
        return principal

    def read_dependency(
        request: Request,
        authorization: str | None = Header(default=None),
        x_monitor_token: str | None = Header(default=None, alias="X-Monitor-Token"),
    ) -> Principal:
        if settings.read_auth_strict:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "MONITOR_NOT_CONFIGURED：监听地址不是本机时必须配置 MONITOR_PANEL_TOKEN",
            )
        try:
            principal = read_auth.authenticate(authorization, x_monitor_token)
        except AuthError as exc:
            raise HTTPException(exc.status, f"{exc.code}：{exc.message}", headers=exc.headers) from None
        request.state.principal = principal
        return principal

    def admin_dependency(
        request: Request,
        authorization: str | None = Header(default=None),
        x_monitor_token: str | None = Header(default=None, alias="X-Monitor-Token"),
    ) -> Principal:
        if settings.read_auth_strict:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "MONITOR_NOT_CONFIGURED：监听地址不是本机时必须配置 MONITOR_PANEL_TOKEN",
            )
        try:
            principal = admin_auth.authenticate(authorization, x_monitor_token)
        except AuthError as exc:
            raise HTTPException(exc.status, f"{exc.code}：{exc.message}", headers=exc.headers) from None
        request.state.principal = principal
        return principal

    def tenant_scope(principal: Principal) -> str | None:
        """管理员可看全部租户；其他身份只看自己的租户。"""
        if principal.has("admin") or principal.tenant == "*":
            return None
        return principal.tenant

    # ---------------------------------------------------------------- 元信息

    @app.get("/", tags=["meta"])
    async def root():
        return {
            "service": "客服幻觉监测平台",
            "version": __version__,
            "ingest": "POST /api/ingest/events",
            "read": "GET /api/monitor/*",
            "review": "POST /api/monitor/events/{event_id}/review",
            "delete": "DELETE /api/monitor/events/{event_id}",
            "alerts": "GET /api/monitor/alerts",
            "metrics": "GET /metrics",
            "health": "GET /health",
            "ready": "GET /readyz",
        }

    @app.get("/healthz", tags=["ops"], summary="存活检查")
    async def healthz():
        """轻量存活探针：只证明进程可用，不探测上游。"""
        return {"status": "ok", "version": __version__}

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

    @app.get("/readyz", tags=["ops"], summary="就绪检查")
    async def readyz():
        worker = app.state.worker
        checks: dict[str, Any] = {"store": "ok", "upstream": "unknown", "ingest_auth": "ok"}
        ready = True
        try:
            store.stats()
        except Exception as exc:  # pragma: no cover - 存储异常
            checks["store"] = f"error: {exc}"
            ready = False
        if ingest_auth.strict:
            checks["ingest_auth"] = "MONITOR_API_TOKENS 未配置，接入接口将返回 503"
            ready = False
        if worker is not None:
            probe = worker.upstream.probe()
            checks["upstream"] = probe.get("status", "unknown")
            if probe.get("status") == "unreachable":
                ready = False
        status_code = 200 if ready else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ready" if ready else "degraded", "checks": checks},
        )

    # ---------------------------------------------------------------- 接入

    @app.post(
        "/api/ingest/events",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["ingest"],
        summary="外部系统接入事件",
    )
    async def ingest_events(
        request: Request,
        payload: Any = Body(...),
        principal: Principal = Depends(ingest_dependency),
    ):
        """接入一条或多条客服会话事件，异步检测；同一事件重复接入幂等返回。"""
        items, is_batch = _normalize_events(payload)
        limit = settings.max_batch_items
        if limit and len(items) > limit:
            raise HTTPException(
                413,
                f"单次最多接入 {limit} 条事件，当前 {len(items)} 条；请分批提交",
            )
        try:
            quota.reserve(principal, len(items))
        except AuthError as exc:
            raise HTTPException(exc.status, f"{exc.code}：{exc.message}", headers=exc.headers) from None
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
            event_id, created = store.ingest(event, sampled, tenant=principal.tenant)
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

    # ---------------------------------------------------------------- 只读

    @app.get("/api/monitor/stats", tags=["read"], summary="整体统计")
    async def get_stats(
        days: int | None = None, principal: Principal = Depends(read_dependency)
    ):
        return store.stats(
            max(1, min(days, 365)) if days else None, tenant=tenant_scope(principal)
        )

    @app.get("/api/monitor/timeseries", tags=["read"], summary="趋势时间序列")
    async def get_timeseries(days: int = 7, principal: Principal = Depends(read_dependency)):
        return store.timeseries(days, tenant=tenant_scope(principal))

    @app.get("/api/monitor/knowledge-gaps", tags=["read"], summary="知识缺口榜")
    async def get_knowledge_gaps(
        limit: int = 10, principal: Principal = Depends(read_dependency)
    ):
        return store.knowledge_gaps(max(1, min(limit, 200)), tenant=tenant_scope(principal))

    @app.get("/api/monitor/events", tags=["read"], summary="事件流")
    async def list_events(
        limit: int = 50,
        offset: int = 0,
        status_filter: str | None = None,
        verdict: str | None = None,
        category: str | None = None,
        needs_review: bool | None = None,
        review_status: str | None = None,
        principal: Principal = Depends(read_dependency),
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
            tenant=tenant_scope(principal),
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
    async def get_event(event_id: str, principal: Principal = Depends(read_dependency)):
        doc = store.get(event_id)
        scope = tenant_scope(principal)
        if doc is None or (scope and doc.tenant != scope):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND：事件不存在")
        return doc

    @app.get("/api/monitor/config", tags=["read"], summary="脱敏后的运行配置")
    async def get_config(principal: Principal = Depends(read_dependency)):
        payload = settings.masked()
        payload["quota"] = quota.snapshot(principal)
        return payload

    @app.get("/api/monitor/alerts", tags=["read"], summary="告警投递记录")
    async def list_alerts(
        status_filter: str | None = None,
        limit: int = 50,
        principal: Principal = Depends(read_dependency),
    ):
        return {
            "limit": max(1, min(limit, 200)),
            "items": store.list_alerts(status_filter, limit),
        }

    # ---------------------------------------------------------------- 复核与删除

    @app.post("/api/monitor/events/{event_id}/review", tags=["review"], summary="提交人工复核结论")
    async def review_event(
        event_id: str,
        body: ReviewRequest,
        principal: Principal = Depends(admin_dependency),
    ):
        """确认是幻觉（confirmed）或标记误报（rejected），记录复核人用于审计。"""
        doc = store.get(event_id)
        scope = tenant_scope(principal)
        if doc is None or (scope and doc.tenant != scope):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND：事件不存在")
        store.set_review(event_id, body.status, body.note, reviewer=principal.key_id)
        updated = store.get(event_id)
        logger.info(
            "人工复核",
            extra={
                "extra_fields": {
                    "event_id": event_id,
                    "status": body.status,
                    "reviewer": principal.key_id,
                    "tenant": principal.tenant,
                }
            },
        )
        return {
            "event_id": event_id,
            "review_status": updated.review_status if updated else body.status,
            "review_note": updated.review_note if updated else body.note,
            "reviewer": updated.reviewer if updated else principal.key_id,
            "reviewed_at": updated.reviewed_at if updated else None,
        }

    @app.delete(
        "/api/monitor/events/{event_id}",
        tags=["review"],
        summary="删除事件（个人信息删除请求）",
    )
    async def delete_event(event_id: str, principal: Principal = Depends(admin_dependency)):
        doc = store.get(event_id)
        scope = tenant_scope(principal)
        if doc is None or (scope and doc.tenant != scope):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND：事件不存在")
        store.delete_event(event_id)
        logger.warning(
            "事件已删除",
            extra={"extra_fields": {"event_id": event_id, "key_id": principal.key_id}},
        )
        return {"event_id": event_id, "deleted": True}

    @app.post("/api/monitor/alerts/{alert_id}/replay", tags=["review"], summary="重放失败告警")
    async def replay_alert(alert_id: str, principal: Principal = Depends(admin_dependency)):
        if not store.replay_alert(alert_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "ALERT_NOT_FOUND：告警不存在或无需重放")
        return {"alert_id": alert_id, "status": "queued"}

    # ---------------------------------------------------------------- 指标

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标")
    async def get_metrics():
        worker = app.state.worker
        metrics = worker.metrics if worker is not None else Metrics()
        return PlainTextResponse(
            metrics.render(store),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app