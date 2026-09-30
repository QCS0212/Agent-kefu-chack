"""批量检测服务的 HTTP 接口（企业接入版）。

能力：API Key 鉴权（scope：detect/read/admin）、按 Key 限流与每日配额、Idempotency-Key 幂等、
租户隔离、任务列表/取消/删除、健康检查与 Prometheus 指标、结构化访问日志。

启动：
    API_BIND_HOST=0.0.0.0 API_KEYS='sk-cs|customer-service|detect,read|2000|120' \
    python -B -m uvicorn app.api:app --host 0.0.0.0 --port 8000 --workers 1
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

try:
    import fcntl  # Unix
except ImportError:  # Windows
    import msvcrt

    fcntl = None

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.jobs import JobStore
from app.providers import get_detector
from app.service import (
    IdempotencyStore,
    ServiceMetrics,
    load_api_settings,
)
from common.auth import AuthError, Principal, QuotaTracker, RateLimiter
from common.logging import REQUEST_ID_HEADER, configure_logging

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.3.0"
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

logger = logging.getLogger("app.api")


def _route_of(path: str) -> str:
    """把路径里的任务 ID 归一化，避免指标标签基数爆炸。"""
    return UUID_RE.sub("{task_id}", path)


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=100)
    user_question: str = Field(min_length=1, max_length=20000)
    system_reply: str = Field(min_length=1, max_length=20000)
    knowledge_base: str = Field(min_length=1, max_length=20000)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["mock", "llm"] = "mock"
    items: list[Reply] = Field(min_length=1, max_length=200)

    @field_validator("items")
    @classmethod
    def unique_ids(cls, items):
        if len({item.id for item in items}) != len(items):
            raise ValueError("案例 ID 不得重复")
        return items


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=100)
    is_hallucination: StrictBool


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labels: list[Label] = Field(min_length=1, max_length=200)


def create_app(output_root=None, factory=get_detector, settings=None):
    settings = settings or load_api_settings()
    settings.validate()
    configure_logging(settings.log_level, service="kefu-check", json_logs=settings.log_json)
    directory = Path(output_root or ROOT / "outputs/api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        directory.mkdir(parents=True, exist_ok=True)
        lockfile = (directory / ".service.lock").open("a")
        try:
            if fcntl is not None:
                fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            lockfile.close()
            raise RuntimeError("同一任务目录只能运行一个服务进程；请使用 --workers 1")
        try:
            app.state.store = JobStore(
                directory, factory, max_running=settings.max_running, max_queued=settings.max_queued
            )
            logger.info(
                "服务启动",
                extra={"extra_fields": {"config": settings.masked(), "output": str(directory)}},
            )
            yield
        finally:
            if getattr(app.state, "store", None) is not None:
                app.state.store.close()
                app.state.store = None
            lockfile.close()
            logger.info("服务停止")

    app = FastAPI(
        title="客服回复幻觉检测 API",
        version=VERSION,
        lifespan=lifespan,
        description=(
            "企业接入版批量检测接口：API Key 鉴权、限流配额、幂等提交、租户隔离。"
            "默认 mock；mode=llm 调用服务端配置的真实模型。单批上限由 API_MAX_BATCH 控制。"
        ),
        servers=[{"url": f"http://{settings.bind_host}:{settings.port}"}],
    )

    # 鉴权、限流、配额与指标在构建期初始化：不依赖 lifespan，便于测试与嵌入式调用
    app.state.settings = settings
    app.state.store = None
    app.state.metrics = ServiceMetrics()
    app.state.idempotency = IdempotencyStore(
        directory / "idempotency.json", settings.idempotency_ttl_days
    )
    app.state.rate_limiter = RateLimiter(settings.rate_per_minute)
    app.state.quota = QuotaTracker(directory / "quota.json", settings.daily_quota)
    app.state.authenticators = {
        scope: settings.authenticator_for(scope) for scope in ("detect", "read", "admin")
    }

    # ---------------------------------------------------------------- 依赖与异常

    def client_host(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def require(scope: str):
        def dependency(
            request: Request,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
        ) -> Principal:
            principal = None
            try:
                principal = app.state.authenticators[scope].authenticate(authorization, x_api_key)
                app.state.rate_limiter.check(principal)
            except AuthError as exc:
                app.state.metrics.inc(
                    "api_auth_failures_total", code=exc.code, path=_route_of(request.url.path)
                )
                logger.warning(
                    "请求被拒绝",
                    extra={
                        "extra_fields": {
                            "code": exc.code,
                            "path": request.url.path,
                            "scope": scope,
                            "client": client_host(request),
                        }
                    },
                )
                raise HTTPException(exc.status, exc.code, headers=exc.headers) from None
            request.state.principal = principal
            return principal

        return dependency

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def guard_and_log(request: Request, call_next):
        from common.logging import set_request_id

        started = __import__("time").monotonic()
        request_id = set_request_id(request.headers.get(REQUEST_ID_HEADER))
        origin = request.headers.get("origin")
        if origin not in (None, *settings.allowed_origins):
            return JSONResponse(status_code=403, content={"detail": "不接受该站点的浏览器请求"})
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        duration_ms = round((__import__("time").monotonic() - started) * 1000, 2)
        route = _route_of(request.url.path)
        app.state.metrics.inc("api_requests_total", route=route, status=response.status_code)
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
                    "client": client_host(request),
                }
            },
        )
        return response

    # ---------------------------------------------------------------- 运维接口

    @app.get("/healthz", tags=["ops"], summary="存活检查")
    def healthz():
        return {"status": "ok", "version": VERSION}

    @app.get("/health", tags=["ops"], summary="健康检查（兼容旧路径）")
    def health():
        return {
            "status": "ok",
            "version": VERSION,
            "max_batch_size": settings.max_batch,
            "max_running_tasks": settings.max_running,
            "max_queued_tasks": settings.max_queued,
            "auth_required": settings.auth_required,
        }

    @app.get("/readyz", tags=["ops"], summary="就绪检查")
    def readyz():
        checks: dict[str, object] = {"store": "ok", "llm_config": "not_required"}
        ready = True
        try:
            app.state.store.status_counts()
        except Exception as exc:  # pragma: no cover - 存储异常
            checks["store"] = f"error: {exc}"
            ready = False
        if any(job.get("mode") == "llm" for job in app.state.store.jobs.values()):
            from app.llm import config as llm_config

            try:
                llm_config()
            except ValueError as exc:
                checks["llm_config"] = str(exc)
        status_code = 200 if ready else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ready" if ready else "degraded", "checks": checks},
        )

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标")
    def metrics():
        return PlainTextResponse(
            app.state.metrics.render(app.state.store),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/service/config", tags=["ops"], summary="脱敏后的服务配置")
    def service_config(principal: Principal = Depends(require("admin"))):
        payload = settings.masked()
        payload["quota"] = app.state.quota.snapshot(principal)
        return payload

    # ---------------------------------------------------------------- 检测接口

    @app.post("/api/checks", status_code=202, tags=["detect"], summary="提交单条或批量检测")
    def submit(
        request: Request,
        body: CheckRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal: Principal = Depends(require("detect")),
    ):
        if len(body.items) > settings.max_batch:
            raise HTTPException(
                422, f"单批最多 {settings.max_batch} 条，当前 {len(body.items)} 条"
            )
        items = [item.model_dump() for item in body.items]
        fingerprint = sha256(
            (body.mode + "|" + __import__("json").dumps(items, ensure_ascii=False, sort_keys=True)).encode()
        ).hexdigest()
        if idempotency_key:
            entry = app.state.idempotency.find(principal.tenant, idempotency_key)
            if entry:
                if entry.get("fingerprint") != fingerprint:
                    raise HTTPException(
                        409, "IDEMPOTENCY_CONFLICT: 同一 Idempotency-Key 提交了不同内容"
                    )
                try:
                    existing = app.state.store.get(entry["task_id"])
                except KeyError:
                    existing = None
                if existing:
                    return {
                        "task_id": existing["task_id"],
                        "status": existing["status"],
                        "status_url": f"/api/checks/{existing['task_id']}",
                        "report_url": f"/api/checks/{existing['task_id']}/report",
                        "idempotent_replay": True,
                    }
        try:
            app.state.quota.reserve(principal, len(items))
        except AuthError as exc:
            app.state.metrics.inc("api_auth_failures_total", code=exc.code, path="/api/checks")
            raise HTTPException(exc.status, exc.code, headers=exc.headers) from None
        try:
            task_id = app.state.store.submit(
                body.mode, items, tenant=principal.tenant, idempotency_key=idempotency_key
            )
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from None
        except ValueError:
            raise HTTPException(503, "真实模型配置缺失或无效，请检查服务端 .env")
        app.state.metrics.inc("api_items_submitted_total")
        if idempotency_key:
            app.state.idempotency.save(principal.tenant, idempotency_key, task_id, fingerprint)
        logger.info(
            "任务已受理",
            extra={
                "extra_fields": {
                    "task_id": task_id,
                    "tenant": principal.tenant,
                    "mode": body.mode,
                    "items": len(items),
                }
            },
        )
        return {
            "task_id": task_id,
            "status": "accepted",
            "status_url": f"/api/checks/{task_id}",
            "report_url": f"/api/checks/{task_id}/report",
        }

    @app.get("/api/checks", tags=["detect"], summary="任务列表")
    def list_tasks(
        status_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
        principal: Principal = Depends(require("read")),
    ):
        tenant = None if principal.has("admin") else principal.tenant
        return app.state.store.list_jobs(
            tenant=tenant,
            status=status_filter,
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )

    def _job_for(task_id: UUID, principal: Principal) -> dict:
        try:
            job = app.state.store.get(str(task_id))
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        if not principal.has("admin") and job.get("tenant") != principal.tenant:
            raise HTTPException(404, "任务不存在")
        return job

    @app.get("/api/checks/{task_id}", tags=["detect"], summary="查询任务进度及逐条结果")
    def result(task_id: UUID, principal: Principal = Depends(require("read"))):
        job = _job_for(task_id, principal)
        job.pop("items", None)
        job["progress"] = {
            "processed": job["completed"] + job["failed"],
            "total": job["total"],
            "succeeded": job["completed"],
            "failed": job["failed"],
        }
        return job

    @app.post("/api/checks/{task_id}/evaluate", tags=["detect"], summary="提交人工标签进行独立评估")
    def score(
        task_id: UUID,
        body: EvaluationRequest,
        principal: Principal = Depends(require("read")),
    ):
        _job_for(task_id, principal)
        try:
            return app.state.store.score(str(task_id), [item.model_dump() for item in body.labels])
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/checks/{task_id}/report", tags=["detect"], summary="打开 HTML 图表报告")
    def report(task_id: UUID, principal: Principal = Depends(require("read"))):
        job = _job_for(task_id, principal)
        if not job["report_ready"]:
            raise HTTPException(409, "报告尚未生成；请先查询任务状态")
        return FileResponse(
            directory / str(task_id) / "report.html",
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/checks/{task_id}/cancel", tags=["detect"], summary="取消任务")
    def cancel(task_id: UUID, principal: Principal = Depends(require("read"))):
        _job_for(task_id, principal)
        try:
            cancelled = app.state.store.cancel(str(task_id))
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        if not cancelled:
            raise HTTPException(409, "任务已结束，无法取消")
        return {"task_id": str(task_id), "cancel_requested": True}

    @app.delete("/api/checks/{task_id}", tags=["admin"], summary="删除任务及其产物")
    def delete(task_id: UUID, principal: Principal = Depends(require("admin"))):
        _job_for(task_id, principal)
        app.state.store.delete(str(task_id))
        logger.warning(
            "任务已删除",
            extra={"extra_fields": {"task_id": str(task_id), "key_id": principal.key_id}},
        )
        return {"task_id": str(task_id), "deleted": True}

    # CORS：浏览器（如智选票 vue3 面板）跨域调用本服务时需要。
    # 放在最后注册，保证预检请求先被 CORS 中间件处理。
    if settings.allowed_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Api-Key",
                "X-Request-ID",
                "Idempotency-Key",
            ],
            expose_headers=["X-Request-ID"],
        )

    return app


app = create_app()