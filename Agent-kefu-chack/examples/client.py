"""企业接入示例：提交检测、轮询结果、提交标签评估、接入监测事件。

用法（项目根目录）：
    API_BASE=http://127.0.0.1:8000 API_KEY=sk-cs-9f2a \
    MONITOR_BASE=http://127.0.0.1:8010 MONITOR_KEY=sk-cc-a1b2 \
    .venv/bin/python -B examples/client.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

API_BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")
MONITOR_BASE = os.environ.get("MONITOR_BASE", "http://127.0.0.1:8010").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
MONITOR_KEY = os.environ.get("MONITOR_KEY", "")

SAMPLE = {
    "id": "case-0001",
    "user_question": "可以货到付款吗？",
    "system_reply": "目前支持货到付款，也可以微信支付。",
    "knowledge_base": "支付方式：微信支付、支付宝、银行卡。不支持货到付款。",
}


def call(url: str, payload: dict | None = None, key: str = "", idempotency_key: str | None = None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def run_detection() -> None:
    print("== 批量检测 API ==", API_BASE)
    idem = str(uuid4())
    status, body = call(f"{API_BASE}/api/checks", {"mode": "mock", "items": [SAMPLE]}, API_KEY, idem)
    print("提交:", status, body)
    if status != 202:
        return
    task_id = body["task_id"]
    # 幂等重放：同样的 Idempotency-Key 会返回同一个任务
    _, replay = call(f"{API_BASE}/api/checks", {"mode": "mock", "items": [SAMPLE]}, API_KEY, idem)
    print("幂等重放:", replay.get("idempotent_replay"), replay.get("task_id") == task_id)
    for _ in range(60):
        _, job = call(f"{API_BASE}/api/checks/{task_id}", key=API_KEY)
        if job.get("status") not in ("queued", "running"):
            break
        time.sleep(1)
    print("结果:", json.dumps(job.get("results"), ensure_ascii=False)[:300])
    _, evaluation = call(
        f"{API_BASE}/api/checks/{task_id}/evaluate",
        {"labels": [{"id": SAMPLE["id"], "is_hallucination": True}]},
        API_KEY,
    )
    print("评估:", json.dumps(evaluation.get("metrics"), ensure_ascii=False))
    print("报告:", f"{API_BASE}/api/checks/{task_id}/report")


def run_monitor() -> None:
    print("== 监测平台 ==", MONITOR_BASE)
    payload = {
        "session_id": str(uuid4()),
        "user_question": SAMPLE["user_question"],
        "system_reply": SAMPLE["system_reply"],
        "category": "payment",
        "knowledge_snippet": "支付方式：微信支付、支付宝、银行卡。不支持货到付款。",
    }
    status, body = call(f"{MONITOR_BASE}/api/ingest/events", payload, MONITOR_KEY)
    print("接入:", status, body)
    if status != 202:
        return
    event_id = body["event_id"]
    for _ in range(30):
        _, event = call(f"{MONITOR_BASE}/api/monitor/events/{event_id}", key=MONITOR_KEY)
        if event.get("status") == "done":
            break
        time.sleep(1)
    print("事件:", json.dumps({
        "status": event.get("status"),
        "verdict": (event.get("detection") or {}).get("verdict"),
        "detector": (event.get("detection") or {}).get("detector"),
        "needs_review": (event.get("detection") or {}).get("needs_review"),
    }, ensure_ascii=False))
    status, review = call(
        f"{MONITOR_BASE}/api/monitor/events/{event_id}/review",
        {"status": "confirmed", "note": "示例复核"},
        MONITOR_KEY,
    )
    print("复核:", status, review)
    _, stats = call(f"{MONITOR_BASE}/api/monitor/stats", key=MONITOR_KEY)
    print("统计:", json.dumps({
        "hallucination_rate": stats.get("hallucination_rate"),
        "coverage": stats.get("coverage"),
        "degraded_rate": stats.get("degraded_rate"),
        "review": stats.get("review"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    if not API_KEY and not MONITOR_KEY:
        print("提示：未设置 API_KEY / MONITOR_KEY，本机默认模式下可直接调用", file=sys.stderr)
    run_detection()
    run_monitor()