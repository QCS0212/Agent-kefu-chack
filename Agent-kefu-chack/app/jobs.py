"""单进程任务队列与磁盘检查点。

支持：租户隔离、幂等键、任务列表/取消/删除、重启标记中断。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from app.providers import get_detector
from app.schema import validate_prediction
from app.report import write_report
from app.evaluate import evaluate

logger = logging.getLogger("app.jobs")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, root, factory=get_detector, max_running: int = 2, max_queued: int = 8):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.factory = factory
        self.max_running = max(1, max_running)
        self.max_queued = max(1, max_queued)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=self.max_running, thread_name_prefix="kefu")
        self.jobs: dict[str, dict] = {}
        for path in self.root.glob("*/task.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") in ("queued", "running"):
                job["status"] = "interrupted"
                job["error"] = "服务重启中断了任务；请重新提交。"
                job["updated_at"] = now()
            self.jobs[job["task_id"]] = job
            self.save(job)

    # ------------------------------------------------------------------ 基础读写

    def save(self, job) -> None:
        folder = self.root / job["task_id"]
        folder.mkdir(exist_ok=True)
        temp = folder / "task.json.tmp"
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(folder / "task.json")

    def get(self, task_id) -> dict:
        with self.lock:
            if task_id not in self.jobs:
                raise KeyError(task_id)
            return json.loads(json.dumps(self.jobs[task_id]))

    def status_counts(self) -> dict[str, int]:
        with self.lock:
            counts: dict[str, int] = {}
            for job in self.jobs.values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
        return counts

    def list_jobs(
        self,
        tenant: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        with self.lock:
            rows = list(self.jobs.values())
        if tenant:
            rows = [job for job in rows if job.get("tenant") == tenant]
        if status:
            rows = [job for job in rows if job.get("status") == status]
        rows.sort(key=lambda job: job.get("created_at") or "", reverse=True)
        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self.summary(job) for job in page],
        }

    @staticmethod
    def summary(job: dict) -> dict:
        return {
            "task_id": job["task_id"],
            "status": job["status"],
            "mode": job.get("mode"),
            "tenant": job.get("tenant", "default"),
            "total": job.get("total"),
            "completed": job.get("completed"),
            "failed": job.get("failed"),
            "model": job.get("model"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "report_ready": job.get("report_ready", False),
            "has_evaluation": bool(job.get("evaluation")),
            "idempotency_key": job.get("idempotency_key"),
        }

    # ------------------------------------------------------------------ 提交

    def submit(self, mode, items, tenant: str = "default", idempotency_key: str | None = None) -> str:
        with self.lock:
            active = sum(job["status"] in ("queued", "running") for job in self.jobs.values())
            if active >= self.max_queued:
                raise OverflowError("任务队列已满，请稍后再试")
            # 接受任务前先校验配置，避免收下付费任务后才发现配置错误
            detector = self.factory(mode)
            task_id = str(uuid4())
            job = {
                "task_id": task_id,
                "status": "queued",
                "mode": mode,
                "tenant": tenant,
                "idempotency_key": idempotency_key,
                "created_at": now(),
                "updated_at": now(),
                "total": len(items),
                "completed": 0,
                "failed": 0,
                "results": [],
                "errors": [],
                "evaluation": None,
                "report_ready": False,
                "items": items,
                "cancel_requested": False,
                "model": getattr(detector, "model", None),
                "input_sha256": sha256(
                    json.dumps(items, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            }
            if mode == "llm":
                from app.llm import PROMPT, PROMPT_VERSION

                job.update(
                    prompt_version=PROMPT_VERSION,
                    prompt_sha256=sha256(PROMPT.encode()).hexdigest(),
                    temperature=0,
                )
            self.jobs[task_id] = job
            self.save(job)
            self.executor.submit(self.work, task_id, detector)
            return task_id

    def cancel(self, task_id) -> bool:
        """请求取消：排队中直接置为已取消，运行中在下一条样本前停下。"""
        with self.lock:
            job = self.jobs.get(task_id)
            if job is None:
                raise KeyError(task_id)
            if job["status"] not in ("queued", "running"):
                return False
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["updated_at"] = now()
            self.save(job)
            return True

    def delete(self, task_id) -> None:
        """删除任务及其本地产物。"""
        with self.lock:
            if task_id not in self.jobs:
                raise KeyError(task_id)
            folder = (self.root / task_id).resolve()
            if self.root.resolve() not in folder.parents:
                raise ValueError("任务目录不在输出根目录内，拒绝删除")
            self.jobs.pop(task_id, None)
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)

    # ------------------------------------------------------------------ 执行

    def predictions(self, job):
        return {k: job[k] for k in ("mode", "created_at", "model", "input_sha256", "results", "errors")}

    def report(self, job):
        evaluation = job["evaluation"] or {
            "metrics": None,
            "note": "模拟结果不计算正式检出率。"
            if job["mode"] == "mock"
            else "未提交人工标签，仅展示检测结果，不计算检出率。",
        }
        folder = self.root / job["task_id"]
        temp = folder / "report.tmp.html"
        write_report(temp, self.predictions(job), evaluation, job["items"])
        temp.replace(folder / "report.html")
        job["report_ready"] = True

    def work(self, task_id, detector):
        job = self.jobs[task_id]
        try:
            with self.lock:
                if job.get("cancel_requested"):
                    job["status"] = "cancelled"
                    job["updated_at"] = now()
                    self.save(job)
                    return
                job["status"] = "running"
                job["updated_at"] = now()
                self.save(job)
            for index, row in enumerate(job["items"]):
                with self.lock:
                    if job.get("cancel_requested"):
                        job["status"] = "cancelled"
                        job["updated_at"] = now()
                        self.save(job)
                        logger.info("任务已按请求取消", extra={"extra_fields": {"task_id": task_id}})
                        return
                try:
                    result = validate_prediction(detector.detect(row), row)
                    error = None
                except Exception as exc:
                    result = None
                    error = {
                        "id": row["id"],
                        "code": "DETECTION_FAILED",
                        "message": str(exc)
                        if isinstance(exc, ValueError)
                        else "检测执行失败，请检查服务端配置或上游状态。",
                    }
                raw = getattr(detector, "last_response", None)
                if raw:
                    folder = self.root / task_id / "raw"
                    folder.mkdir(exist_ok=True)
                    (folder / f"{index + 1:03}.json").write_text(
                        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                with self.lock:
                    if error:
                        job["errors"].append(error)
                        job["failed"] += 1
                    else:
                        job["results"].append(result)
                        job["completed"] += 1
                    job["updated_at"] = now()
                    self.save(job)
            with self.lock:
                cancelled = bool(job.get("cancel_requested"))
                if cancelled:
                    # 取消：保留已完成的结果，状态标记为已取消，不再生成完整报告
                    job["status"] = "cancelled"
                    if job["completed"] and not job["failed"]:
                        self.report(job)
                    logger.info(
                        "任务已取消",
                        extra={
                            "extra_fields": {
                                "task_id": task_id,
                                "completed": job["completed"],
                                "failed": job["failed"],
                            }
                        },
                    )
                elif job["failed"]:
                    job["status"] = "partial_failed" if job["completed"] else "failed"
                else:
                    self.report(job)
                    job["status"] = "completed"
                job["updated_at"] = now()
                self.save(job)
        except Exception:
            logger.exception("任务执行失败", extra={"extra_fields": {"task_id": task_id}})
            with self.lock:
                job["status"] = "failed"
                job["error"] = "任务保存或报告生成失败，请检查服务端。"
                job["updated_at"] = now()
                self.save(job)

    def score(self, task_id, labels):
        with self.lock:
            job = self.jobs[task_id]
            if job["status"] != "completed":
                raise RuntimeError("任务尚未完整完成，不能评估")
            result = evaluate(self.predictions(job), labels)
            job["evaluation"] = result
            self.report(job)
            job["updated_at"] = now()
            self.save(job)
            path = self.root / task_id / "evaluation.json"
            path.write_text(
                json.dumps({"labels": labels, "evaluation": result}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result

    def close(self):
        self.executor.shutdown(wait=True)