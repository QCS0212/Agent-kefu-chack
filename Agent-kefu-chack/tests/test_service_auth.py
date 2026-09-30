"""企业接入层测试：API Key、scope、限流、配额、幂等、租户隔离、任务管理。

全部离线，用 TestClient 直接调用构建好的应用，不依赖网络与真实模型。
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("LOG_CONFIGURE", "0")
os.environ["API_KEYS"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.providers import MockDetector  # noqa: E402
from app.service import ApiSettings, IdempotencyStore  # noqa: E402
from common.auth import Authenticator, AuthError, QuotaTracker, RateLimiter, parse_key_specs  # noqa: E402

ITEM = {
    "id": "case-1",
    "user_question": "支持退货吗？",
    "system_reply": "支持七天退货。",
    "knowledge_base": "支持七天退货。",
}
KEY_A = "sk-tenant-a-0001"
KEY_B = "sk-tenant-b-0002"
KEY_ADMIN = "sk-admin-0003"
KEYS = (
    f"{KEY_A}|tenant-a|detect,read|0|0;"
    f"{KEY_B}|tenant-b|detect,read|0|0;"
    f"{KEY_ADMIN}|ops|detect,read,admin|0|0"
)


def settings(**overrides) -> ApiSettings:
    base = dict(bind_host="127.0.0.1", keys_raw=KEYS, rate_per_minute=0, log_json=False)
    base.update(overrides)
    return ApiSettings(**base)


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class AuthUnitTests(unittest.TestCase):
    def test_parse_specs_and_scopes(self):
        keys = parse_key_specs("tok-a|tenant-a|detect,read|100|60;plain")
        self.assertEqual(keys["tok-a"].tenant, "tenant-a")
        self.assertTrue(keys["tok-a"].has("detect"))
        self.assertFalse(keys["tok-a"].has("admin"))
        self.assertEqual(keys["tok-a"].daily_quota, 100)
        self.assertEqual(keys["plain"].tenant, "default")
        self.assertTrue(keys["plain"].has("admin"))

    def test_authenticator_scope_and_errors(self):
        keys = parse_key_specs("tok-a|tenant-a|detect")
        authn = Authenticator(keys, scope="detect", require=True)
        self.assertEqual(authn.authenticate("Bearer tok-a", None).tenant, "tenant-a")
        self.assertEqual(authn.authenticate(None, "tok-a").tenant, "tenant-a")
        with self.assertRaises(AuthError) as ctx:
            authn.authenticate(None, None)
        self.assertEqual(ctx.exception.code, "MISSING_TOKEN")
        with self.assertRaises(AuthError) as ctx:
            authn.authenticate("Bearer nope", None)
        self.assertEqual(ctx.exception.code, "INVALID_TOKEN")
        with self.assertRaises(AuthError) as ctx:
            Authenticator(keys, scope="admin", require=True).authenticate("Bearer tok-a", None)
        self.assertEqual(ctx.exception.code, "SCOPE_DENIED")

    def test_authenticator_strict_when_required_without_keys(self):
        authn = Authenticator({}, scope="ingest", require=True)
        self.assertTrue(authn.strict)
        with self.assertRaises(AuthError) as ctx:
            authn.authenticate(None, None)
        self.assertEqual(ctx.exception.status, 503)

    def test_open_mode_allows_anonymous_but_validates_presented_token(self):
        authn = Authenticator(parse_key_specs("tok-a|t"), scope="read", require=False, enforce=False)
        self.assertTrue(authn.authenticate(None, None).anonymous)
        with self.assertRaises(AuthError):
            authn.authenticate("Bearer wrong", None)

    def test_rate_limiter(self):
        limiter = RateLimiter(2)
        principal = parse_key_specs("tok|t")["tok"]
        limiter.check(principal)
        limiter.check(principal)
        with self.assertRaises(AuthError) as ctx:
            limiter.check(principal)
        self.assertEqual(ctx.exception.code, "RATE_LIMITED")

    def test_quota_tracker_persists_and_rejects(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "quota.json"
            tracker = QuotaTracker(path, default_quota=2)
            principal = parse_key_specs("tok|t")["tok"]
            tracker.reserve(principal)
            tracker.reserve(principal)
            with self.assertRaises(AuthError) as ctx:
                tracker.reserve(principal)
            self.assertEqual(ctx.exception.code, "QUOTA_EXCEEDED")
            self.assertEqual(QuotaTracker(path, default_quota=2).usage(principal), 2)

    def test_idempotency_store_ttl_and_fingerprint(self):
        with TemporaryDirectory() as folder:
            store = IdempotencyStore(Path(folder) / "idem.json", ttl_days=7)
            self.assertIsNone(store.find("t1", "k1"))
            store.save("t1", "k1", "task-1", "fp-1")
            self.assertEqual(store.find("t1", "k1")["fingerprint"], "fp-1")
            self.assertIsNone(store.find("t2", "k1"))


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._stack = contextlib.ExitStack()
        self.client = self._stack.enter_context(
            TestClient(create_app(self._tmp.name, settings=settings()))
        )

    def tearDown(self):
        self._stack.close()
        self._tmp.cleanup()

    def client_for(self, suffix: str, factory=None, **overrides):
        """为单个用例构建独立应用（同一临时目录下换子目录，避免进程锁冲突）。"""
        app = create_app(
            f"{self._tmp.name}-{suffix}",
            factory=factory or MockDetector and __import__("app.providers", fromlist=["get_detector"]).get_detector,
            settings=settings(**overrides),
        )
        return self._stack.enter_context(TestClient(app))

    def wait(self, task_id, headers):
        for _ in range(200):
            job = self.client.get(f"/api/checks/{task_id}", headers=headers).json()
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(0.01)
        self.fail("任务未完成")

    def test_missing_and_invalid_key(self):
        self.assertEqual(self.client.post("/api/checks", json={"items": [ITEM]}).status_code, 401)
        self.assertEqual(
            self.client.post("/api/checks", json={"items": [ITEM]}, headers=auth("bad")).status_code, 401
        )

    def test_scope_denied_for_write_without_detect(self):
        client = self.client_for("scope", keys_raw="sk-ro|t|read|0|0")
        self.assertEqual(
            client.post("/api/checks", json={"items": [ITEM]}, headers=auth("sk-ro")).status_code,
            403,
        )

    def test_valid_key_runs_and_returns_request_id(self):
        response = self.client.post("/api/checks", json={"items": [ITEM]}, headers=auth(KEY_A))
        self.assertEqual(response.status_code, 202)
        self.assertIn("X-Request-ID", response.headers)
        job = self.wait(response.json()["task_id"], auth(KEY_A))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["tenant"], "tenant-a")

    def test_tenant_isolation_on_read(self):
        task_id = self.client.post(
            "/api/checks", json={"items": [ITEM]}, headers=auth(KEY_A)
        ).json()["task_id"]
        self.wait(task_id, auth(KEY_A))
        self.assertEqual(self.client.get(f"/api/checks/{task_id}", headers=auth(KEY_B)).status_code, 404)
        self.assertEqual(self.client.get(f"/api/checks/{task_id}", headers=auth(KEY_ADMIN)).status_code, 200)
        listed_a = self.client.get("/api/checks", headers=auth(KEY_A)).json()
        self.assertEqual({row["tenant"] for row in listed_a["items"]}, {"tenant-a"})
        listed_admin = self.client.get("/api/checks", headers=auth(KEY_ADMIN)).json()
        self.assertGreaterEqual(listed_admin["total"], 1)

    def test_idempotency_replay_and_conflict(self):
        headers = {**auth(KEY_A), "Idempotency-Key": "order-42"}
        first = self.client.post("/api/checks", json={"items": [ITEM]}, headers=headers)
        second = self.client.post("/api/checks", json={"items": [ITEM]}, headers=headers)
        self.assertEqual(first.json()["task_id"], second.json()["task_id"])
        self.assertTrue(second.json().get("idempotent_replay"))
        conflict = self.client.post(
            "/api/checks",
            json={"items": [{**ITEM, "system_reply": "支持三十天退货。"}]},
            headers=headers,
        )
        self.assertEqual(conflict.status_code, 409)

    def test_quota_exceeded(self):
        client = self.client_for("quota", keys_raw="sk-q|t|detect|1|0")
        self.assertEqual(
            client.post("/api/checks", json={"items": [ITEM]}, headers=auth("sk-q")).status_code, 202
        )
        self.assertEqual(
            client.post("/api/checks", json={"items": [ITEM]}, headers=auth("sk-q")).status_code, 429
        )

    def test_rate_limit(self):
        client = self.client_for("rate", rate_per_minute=2)
        codes = [
            client.post("/api/checks", json={"items": [ITEM]}, headers=auth(KEY_A)).status_code
            for _ in range(3)
        ]
        self.assertEqual(codes, [202, 202, 429])

    def test_batch_over_limit_rejected(self):
        client = self.client_for("batch-limit", max_batch=2)
        items = [{**ITEM, "id": f"case-{index}"} for index in range(3)]
        response = client.post("/api/checks", json={"items": items}, headers=auth(KEY_A))
        self.assertEqual(response.status_code, 422)
        self.assertIn("单批最多", response.json()["detail"])

    def test_admin_only_delete_and_cancel(self):
        task_id = self.client.post(
            "/api/checks", json={"items": [ITEM]}, headers=auth(KEY_A)
        ).json()["task_id"]
        self.wait(task_id, auth(KEY_A))
        self.assertEqual(self.client.delete(f"/api/checks/{task_id}", headers=auth(KEY_A)).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/checks/{task_id}", headers=auth(KEY_ADMIN)).status_code, 200)
        self.assertEqual(self.client.get(f"/api/checks/{task_id}", headers=auth(KEY_ADMIN)).status_code, 404)

    def test_cancel_running_job(self):
        gate = threading.Event()

        class Blocking(MockDetector):
            def detect(self, row):
                gate.wait(5)
                return super().detect(row)

        client = self.client_for("cancel", factory=lambda mode: Blocking())
        task_id = client.post(
            "/api/checks", json={"items": [ITEM]}, headers=auth(KEY_A)
        ).json()["task_id"]
        self.assertEqual(
            client.post(f"/api/checks/{task_id}/cancel", headers=auth(KEY_A)).status_code, 200
        )
        gate.set()
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            status = client.get(f"/api/checks/{task_id}", headers=auth(KEY_A)).json()["status"]
            if status in ("cancelled", "completed"):
                break
            time.sleep(0.02)
        self.assertEqual(status, "cancelled")

    def test_ops_endpoints_open(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/readyz").status_code, 200)
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("api_requests_total", metrics.text)
        self.assertIn("api_tasks", metrics.text)

    def test_fail_fast_when_exposed_without_keys(self):
        with self.assertRaises(RuntimeError):
            create_app(self._tmp.name + "-bad", settings=settings(bind_host="0.0.0.0", keys_raw=""))


if __name__ == "__main__":
    unittest.main()