"""监测平台单元测试。用标准库 unittest，需在项目根目录以 venv python 运行。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("LOG_CONFIGURE", "0")
# 测试必须与开发者本地 .env 隔离，否则本地配置会改变断言结果
os.environ["MONITOR_API_KEYS"] = ""
os.environ["MONITOR_KNOWLEDGE_SOURCE"] = "file"
os.environ["MONITOR_API_TOKENS"] = ""
os.environ["MONITOR_PANEL_TOKEN"] = ""

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor.config import Settings  # noqa: E402
from monitor.alerts import AlertRouter  # noqa: E402
from monitor.api import create_app  # noqa: E402
from monitor.metrics import Metrics  # noqa: E402
from monitor.knowledge import FileKnowledgeSource, redact  # noqa: E402
from monitor.models import DetectionError, IngestEvent  # noqa: E402
from monitor.models import DetectionRecord  # noqa: E402
from monitor.worker import MonitorWorker  # noqa: E402
from monitor.rules import RuleEngine  # noqa: E402
from monitor.sampling import SamplingPolicy  # noqa: E402
from monitor.store import Store  # noqa: E402
from monitor.upstream import (  # noqa: E402
    UpstreamClient,
    fallback_record,
    to_record,
)


def make_event(**overrides) -> IngestEvent:
    params = {
        "session_id": "sess-1",
        "user_question": "我的订单什么时候到？",
        "system_reply": "您的订单已经发货，预计明天到达。",
    }
    params.update(overrides)
    return IngestEvent(**params)


class SamplingTests(unittest.TestCase):
    def test_always_sample_category(self):
        policy = SamplingPolicy(rate=0.0, always_sample=("after_sale",))
        event = make_event(category="after_sale")
        self.assertTrue(policy.should_sample(event))
        self.assertEqual(policy.reason(event), "high_risk_category")

    def test_rate_zero_skips(self):
        policy = SamplingPolicy(rate=0.0)
        self.assertFalse(policy.should_sample(make_event()))
        self.assertEqual(policy.reason(make_event()), "skipped")

    def test_rate_one_samples_all(self):
        policy = SamplingPolicy(rate=1.0)
        self.assertTrue(policy.should_sample(make_event()))

    def test_hash_is_deterministic(self):
        policy = SamplingPolicy(rate=0.5)
        event = make_event(session_id="stable-session")
        first = policy.should_sample(event)
        for _ in range(20):
            self.assertEqual(policy.should_sample(event), first)

    def test_invalid_rate(self):
        with self.assertRaises(ValueError):
            SamplingPolicy(rate=1.5)


class RuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RuleEngine(knowledge_names=["王经理", "北京仓"])

    def test_execution_claim_without_record_needs_review(self):
        # 没有执行记录时不能直接判幻觉：记为待核验，交给人工核对工单系统
        event = make_event(system_reply="好的，我已经帮您完成了退款。")
        record = self.engine.check(event, "【全局政策】退款需订单号")
        self.assertIsNotNone(record)
        self.assertEqual(record.verdict, "not_verifiable")
        self.assertIn("capability_overreach", record.types)
        self.assertIsNone(record.severity)
        self.assertTrue(record.needs_review)
        self.assertEqual(record.detector, "rule")

    def test_unrelated_denial_is_not_capability_overreach(self):
        # 知识否定的是"修改配送信息"，不能据此把"已退款"判成能力越界（回归：真实库端到端时发现的误判）
        event = make_event(system_reply="好的，我已经帮您完成了退款。")
        record = self.engine.check(event, "【配送说明】不支持修改配送电话、地址等信息。")
        self.assertIsNotNone(record)
        self.assertEqual(record.verdict, "not_verifiable")
        self.assertTrue(record.needs_review)
        self.assertNotEqual(record.severity, "high")

    def test_execution_claim_with_capability_denial_is_hallucination(self):
        # 知识依据明确说明系统不具备该能力时才判能力越界
        event = make_event(system_reply="好的，我已经帮您完成了退款。")
        record = self.engine.check(
            event, "【能力说明】系统未接入工单系统，无法查询或修改订单状态。"
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.verdict, "hallucination")
        self.assertEqual(record.severity, "high")
        self.assertIn("capability_overreach", record.types)
        self.assertEqual(record.detector, "rule")

    def test_fabricated_contact_hit(self):
        event = make_event(system_reply="我让张经理联系您。")
        record = self.engine.check(event, "【联系人】王经理")
        self.assertIsNotNone(record)
        self.assertIn("business_fabrication", record.types)

    def test_clean_reply_no_hit(self):
        event = make_event(system_reply="您的订单已发货，预计明天到达。")
        self.assertIsNone(self.engine.check(event, "【全局政策】正常条款"))

    def test_known_name_not_fabricated(self):
        event = make_event(system_reply="我让王经理联系您。")
        record = self.engine.check(event, "【联系人】王经理 北京仓")
        # 王经理在知识联系人名单中，不构成编造
        self.assertNotIn("business_fabrication", record.types if record else [])
        self.assertNotIn("business_fabrication", record.types if record else [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_ingest_idempotent(self):
        event = make_event()
        first_id, created = self.store.ingest(event, sampled=True)
        second_id, created_again = self.store.ingest(event, sampled=True)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first_id, second_id)

    def test_claim_and_detect(self):
        event = make_event()
        event_id, _ = self.store.ingest(event, sampled=True)
        ids = self.store.claim(10)
        self.assertEqual(ids, [event_id])
        self.assertEqual(self.store.get(event_id).status, "running")
        record = fallback_record(
            event_id, DetectionError("UPSTREAM_UNREACHABLE", "网络错误")
        )
        self.store.save_detection(record)
        doc = self.store.get(event_id)
        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.detection.verdict, "not_verifiable")
        self.assertEqual(doc.detection.detector, "fallback")

    def test_skipped_not_claimed(self):
        event_id, _ = self.store.ingest(make_event(), sampled=False)
        self.assertEqual(self.store.claim(10), [])
        self.assertEqual(self.store.get(event_id).status, "skipped")

    def test_reset_running_on_restart(self):
        event_id, _ = self.store.ingest(make_event(), sampled=True)
        self.store.claim(1)
        self.assertEqual(self.store.get(event_id).status, "running")
        self.store.reset_running()
        self.assertEqual(self.store.get(event_id).status, "pending")

    def test_stats_aggregation(self):
        event_id, _ = self.store.ingest(make_event(), sampled=True)
        self.store.claim(1)
        record = fallback_record(
            event_id, DetectionError("UPSTREAM_TIMEOUT", "超时")
        )
        record.event_id = event_id
        self.store.save_detection(record)
        stats = self.store.stats()
        self.assertEqual(stats["events"]["total"], 1)
        self.assertEqual(stats["detections"]["total"], 1)
        self.assertEqual(stats["detections"]["by_verdict"]["not_verifiable"], 1)


class UpstreamTests(unittest.TestCase):
    def _settings(self, **overrides):
        params = {
            "upstream_url": "http://upstream.invalid",
            "upstream_mode": "mock",
        }
        params.update(overrides)
        return Settings(**params)

    def test_to_record_valid(self):
        result = {
            "id": "e1",
            "verdict": "hallucination",
            "types": ["policy_error"],
            "severity": "high",
            "reason": "与退款政策冲突",
            "claims": [
                {
                    "text": "已退款",
                    "relation": "contradicted",
                    "reply_quote": "已退款",
                    "knowledge_quote": "退款需订单号",
                    "reason": "知识库要求订单号",
                }
            ],
        }
        record = to_record(result, "mock")
        self.assertEqual(record.verdict, "hallucination")
        self.assertEqual(record.types, ["policy_error"])

    def test_to_record_rejects_bad_verdict(self):
        with self.assertRaises(DetectionError):
            to_record({"verdict": "maybe", "reason": "x", "claims": []}, "mock")

    def test_to_record_rejects_clean_contradiction(self):
        result = {
            "verdict": "no_hallucination",
            "types": ["policy_error"],
            "reason": "正常",
            "claims": [],
        }
        with self.assertRaises(DetectionError):
            to_record(result, "mock")

    def test_fallback_record_never_fakes(self):
        event = make_event(event_id="e1")
        record = fallback_record(event.event_id, DetectionError("UPSTREAM_BUSY", "队列满"))
        self.assertEqual(record.verdict, "not_verifiable")
        self.assertEqual(record.detector, "fallback")
        self.assertEqual(record.event_id, event.event_id)

    def test_detect_fallback_on_network_error(self):
        def handler(request):
            raise httpx.ConnectError("unreachable")

        transport = httpx.MockTransport(handler)
        client = UpstreamClient(self._settings(), transport=transport)
        try:
            with self.assertRaises(DetectionError):
                client.detect(make_event(), "知识")
        finally:
            client.close()


class FakeUpstream:
    """鸭子类型的上游客户端：worker 只用 detect_many/probe/close。"""

    def __init__(self, record=None, error=None):
        self.record = record
        self.error = error
        self.calls = 0

    def detect(self, event, knowledge):
        self.calls += 1
        if self.error:
            raise self.error
        return self.record

    def detect_many(self, pairs):
        self.calls += 1
        if self.error:
            raise self.error
        outcomes = {}
        for event, _knowledge in pairs:
            if self.record is None:
                raise self.error or DetectionError("UPSTREAM_DETECTION_FAILED", "测试未提供结果")
            outcomes[event.event_id] = self.record.model_copy(
                update={"event_id": event.event_id}
            )
        return outcomes

    def probe(self):
        return {"status": "ok"}

    def close(self):
        pass


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.settings = Settings(
            db_path=self.tmp.name,
            knowledge_dir=str(ROOT / "knowledge"),
            rule_fast_check=True,
        )

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _worker(self, upstream, **overrides):
        settings = Settings(
            db_path=self.tmp.name,
            knowledge_dir=str(ROOT / "knowledge"),
            rule_fast_check=True,
            **overrides,
        )
        return MonitorWorker(
            settings=settings,
            store=self.store,
            upstream=upstream,
            knowledge_source=FileKnowledgeSource(Path(settings.knowledge_dir)),
            rules=RuleEngine(["王经理", "北京仓"]),
            sampling=SamplingPolicy(rate=1.0),
            alerts=AlertRouter(self.store, settings),
            metrics=Metrics(),
        )

    def test_capability_denial_short_circuits_upstream(self):
        event_id, _ = self.store.ingest(
            make_event(
                system_reply="好的，我已经帮您完成了退款。",
                knowledge_snippet="【能力说明】系统未接入工单系统，无法查询或修改订单状态。",
            ),
            sampled=True,
        )
        upstream = FakeUpstream()
        worker = self._worker(upstream)
        self.assertEqual(worker.process_pending(), 1)
        doc = self.store.get(event_id)
        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.detection.verdict, "hallucination")
        self.assertEqual(doc.detection.detector, "rule")
        self.assertEqual(upstream.calls, 0)  # 规则快检命中，不消耗模型调用

    def test_execution_claim_without_record_goes_to_review(self):
        event_id, _ = self.store.ingest(
            make_event(system_reply="好的，我已经帮您完成了退款。"), sampled=True
        )
        upstream = FakeUpstream()
        worker = self._worker(upstream)
        self.assertEqual(worker.process_pending(), 1)
        doc = self.store.get(event_id)
        self.assertEqual(doc.detection.verdict, "not_verifiable")
        self.assertEqual(doc.detection.detector, "rule")
        self.assertTrue(doc.detection.needs_review)
        self.assertEqual(doc.review_status, "pending")
        self.assertEqual(upstream.calls, 0)

    def test_upstream_failure_degrades_not_verifiable(self):
        event_id, _ = self.store.ingest(
            make_event(system_reply="您的订单已发货，预计明天到达。"), sampled=True
        )
        upstream = FakeUpstream(error=DetectionError("UPSTREAM_UNREACHABLE", "网络错误"))
        # 关闭重试时立即降级；开启重试的路径见 ReliabilityTests
        worker = self._worker(upstream, retry_max_attempts=0)
        worker.process_pending()
        doc = self.store.get(event_id)
        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.detection.verdict, "not_verifiable")
        self.assertEqual(doc.detection.detector, "fallback")
        self.assertTrue(doc.detection.needs_review)

    def test_provided_knowledge_wins(self):
        event_id, _ = self.store.ingest(
            make_event(system_reply="正常回复。", knowledge_snippet="外部系统提供的依据"),
            sampled=True,
        )
        upstream = FakeUpstream(
            record=DetectionRecord(
                event_id=event_id,
                verdict="no_hallucination",
                types=[],
                severity=None,
                reason="依据支持该说法",
                claims=[],
                detector="llm",
            )
        )
        self._worker(upstream).process_pending()
        self.assertEqual(upstream.calls, 1)
        doc = self.store.get(event_id)
        self.assertEqual(doc.detection.verdict, "no_hallucination")
        self.assertEqual(doc.detection.knowledge_version, "provided")
        self.assertEqual(doc.detection.detector, "llm")

    def test_batch_forwards_all_events_in_one_upstream_call(self):
        ids = []
        for index in range(4):
            event_id, _ = self.store.ingest(
                make_event(
                    session_id=f"s-{index}",
                    user_question=f"第 {index} 个问题",
                    system_reply=f"第 {index} 条正常回复。",
                ),
                sampled=True,
            )
            ids.append(event_id)
        upstream = FakeUpstream(
            record=DetectionRecord(
                event_id="",
                verdict="no_hallucination",
                types=[],
                severity=None,
                reason="依据支持该说法",
                claims=[],
                detector="llm",
            )
        )
        worker = self._worker(upstream)
        self.assertEqual(worker.process_pending(), 4)
        self.assertEqual(upstream.calls, 1)  # 4 条事件只调用一次上游
        for event_id in ids:
            self.assertEqual(self.store.get(event_id).detection.verdict, "no_hallucination")


class RedactTests(unittest.TestCase):
    def test_redact_phone_and_order(self):
        text = "我手机 13812345678，订单号 AB12345678 已退款。"
        out = redact(text)
        self.assertNotIn("13812345678", out)
        self.assertNotIn("AB12345678", out)
        self.assertIn("[手机号]", out)
        self.assertIn("[订单号]", out)

    def test_redact_keeps_normal_text(self):
        text = "退款需在签收后 7 天内发起。"
        self.assertEqual(redact(text), text)

    def test_redact_extended_patterns(self):
        text = (
            "邮箱 a.b+c@example.com，身份证 110105199003072316，"
            "卡号 6222021234567890123，座机 010-12345678。"
        )
        out = redact(text)
        for secret, token in [
            ("a.b+c@example.com", "[邮箱]"),
            ("110105199003072316", "[身份证号]"),
            ("6222021234567890123", "[银行卡号]"),
            ("010-12345678", "[座机]"),
        ]:
            self.assertNotIn(secret, out)
            self.assertIn(token, out)


class IngestApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.settings = Settings(
            db_path=self.tmp.name,
            api_tokens=("test-token-1",),
            require_auth=True,
            rate_limit_per_minute=0,
            knowledge_dir=str(ROOT / "knowledge"),
        )
        self.app = create_app(self.settings, worker=False)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.state.store.close()
        os.unlink(self.tmp.name)

    def test_no_token_rejected(self):
        response = self.client.post("/api/ingest/events", json={"session_id": "s1"})
        self.assertEqual(response.status_code, 401)

    def test_bad_token_rejected(self):
        response = self.client.post(
            "/api/ingest/events",
            json={"session_id": "s1"},
            headers={"X-Monitor-Token": "wrong"},
        )
        self.assertEqual(response.status_code, 401)

    def test_valid_event_accepted(self):
        response = self.client.post(
            "/api/ingest/events",
            json={
                "session_id": "s1",
                "user_question": "退款多久到账？",
                "system_reply": "1-3 个工作日原路退回。",
            },
            headers={"Authorization": "Bearer test-token-1"},
        )
        self.assertEqual(response.status_code, 202)
        ack = response.json()
        self.assertEqual(ack["status"], "sampled")
        self.assertFalse(ack["duplicate"])
        self.assertIn("/api/monitor/events/", ack["status_url"])

    def test_duplicate_is_idempotent(self):
        payload = {
            "session_id": "s2",
            "user_question": "退款多久到账？",
            "system_reply": "1-3 个工作日原路退回。",
        }
        headers = {"Authorization": "Bearer test-token-1"}
        first = self.client.post("/api/ingest/events", json=payload, headers=headers)
        second = self.client.post("/api/ingest/events", json=payload, headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["event_id"], second.json()["event_id"])
        self.assertTrue(second.json()["duplicate"])

    def test_batch_with_rejections(self):
        response = self.client.post(
            "/api/ingest/events",
            json={
                "events": [
                    {
                        "session_id": "s3",
                        "user_question": "问题",
                        "system_reply": "回复",
                    },
                    {"session_id": "", "user_question": "x", "system_reply": "y"},
                ]
            },
            headers={"Authorization": "Bearer test-token-1"},
        )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(len(body["accepted"]), 1)
        self.assertEqual(len(body["rejected"]), 1)

    def test_read_endpoints_need_panel_token_when_configured(self):
        response = self.client.get("/api/monitor/stats")
        # 未配置 MONITOR_PANEL_TOKEN 时读接口不鉴权
        self.assertEqual(response.status_code, 200)

    def test_event_detail_after_ingest(self):
        ack = self.client.post(
            "/api/ingest/events",
            json={
                "session_id": "s4",
                "user_question": "问题？",
                "system_reply": "回复。",
            },
            headers={"Authorization": "Bearer test-token-1"},
        ).json()
        detail = self.client.get(f"/api/monitor/events/{ack['event_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["session_id"], "s4")

    def test_health_and_metrics(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("monitor_events_total", metrics.text)


class ReliabilityTests(unittest.TestCase):
    """上游不稳定时的重试、退避与降级语义。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.settings = Settings(
            db_path=self.tmp.name,
            knowledge_dir=str(ROOT / "knowledge"),
            retry_max_attempts=2,
            retry_backoff_seconds=0,
        )

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _worker(self, upstream):
        return MonitorWorker(
            settings=self.settings,
            store=self.store,
            upstream=upstream,
            knowledge_source=FileKnowledgeSource(Path(self.settings.knowledge_dir)),
            rules=RuleEngine([]),
            sampling=SamplingPolicy(rate=1.0),
            alerts=AlertRouter(self.store, self.settings),
            metrics=Metrics(),
        )

    def test_transient_failure_is_retried_then_recovered(self):
        event_id, _ = self.store.ingest(
            make_event(system_reply="您的订单已发货，预计明天到达。"), sampled=True
        )

        class Flaky:
            def __init__(self):
                self.calls = 0

            def detect_many(self, pairs):
                self.calls += 1
                if self.calls == 1:
                    raise DetectionError("UPSTREAM_UNREACHABLE", "网络错误")
                return {
                    event.event_id: DetectionRecord(
                        event_id=event.event_id,
                        verdict="no_hallucination",
                        types=[],
                        severity=None,
                        reason="依据支持该说法",
                        claims=[],
                        detector="llm",
                    )
                    for event, _knowledge in pairs
                }

            def probe(self):
                return {"status": "ok"}

            def close(self):
                pass

        upstream = Flaky()
        worker = self._worker(upstream)
        self.assertEqual(worker.process_pending(), 1)
        doc = self.store.get(event_id)
        self.assertEqual(doc.status, "pending")  # 排队重试，不落库结论
        self.assertIsNone(doc.detection)
        self.assertEqual(doc.attempts, 1)
        worker.process_pending()
        doc = self.store.get(event_id)
        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.detection.verdict, "no_hallucination")
        self.assertEqual(upstream.calls, 2)

    def test_retry_exhausted_marks_unverifiable(self):
        event_id, _ = self.store.ingest(
            make_event(system_reply="您的订单已发货，预计明天到达。"), sampled=True
        )
        upstream = FakeUpstream(error=DetectionError("UPSTREAM_UNREACHABLE", "网络错误"))
        worker = self._worker(upstream)
        for _ in range(6):
            worker.process_pending()
            if self.store.get(event_id).status == "done":
                break
        doc = self.store.get(event_id)
        self.assertEqual(doc.status, "done")
        self.assertEqual(doc.detection.verdict, "not_verifiable")
        self.assertEqual(doc.detection.detector, "fallback")
        self.assertTrue(doc.detection.needs_review)
        self.assertEqual(doc.attempts, self.settings.retry_max_attempts)


class StatsAndReviewTests(unittest.TestCase):
    """指标口径与人工复核闭环。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.settings = Settings(db_path=self.tmp.name, knowledge_dir=str(ROOT / "knowledge"))

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _detect(self, verdict, detector="llm", severity=None, needs_review=False):
        event_id, _ = self.store.ingest(
            make_event(
                user_question=f"{verdict}-{detector}-{severity} 的问题",
                system_reply=f"{verdict}-{detector}-{severity} 的回复",
            ),
            sampled=True,
        )
        self.store.save_detection(
            DetectionRecord(
                event_id=event_id,
                verdict=verdict,
                types=["policy_error"] if verdict == "hallucination" else [],
                severity=severity,
                reason="测试结论",
                claims=[],
                detector=detector,
                needs_review=needs_review,
            )
        )
        return event_id

    def test_rate_excludes_not_verifiable(self):
        self._detect("hallucination", severity="high")
        self._detect("no_hallucination")
        self._detect("not_verifiable", detector="fallback", needs_review=True)
        stats = self.store.stats()
        self.assertEqual(stats["detections"]["total"], 3)
        self.assertEqual(stats["detections"]["decisive"], 2)
        self.assertEqual(stats["detections"]["degraded"], 1)
        self.assertEqual(stats["detections"]["fallback"], 1)
        self.assertEqual(stats["detections"]["needs_review"], 1)
        # 上游不可用不再让幻觉率看起来更好，覆盖率单独暴露
        self.assertEqual(stats["hallucination_rate"], 0.5)
        self.assertEqual(stats["coverage"], round(2 / 3, 4))
        self.assertEqual(stats["degraded_rate"], round(1 / 3, 4))

    def test_review_updates_precision_after_review(self):
        first = self._detect("hallucination", severity="high", needs_review=True)
        second = self._detect("hallucination", severity="medium", needs_review=True)
        self.assertEqual(self.store.stats()["review"]["pending"], 2)
        self.assertTrue(self.store.set_review(first, "confirmed", "人工确认"))
        self.assertTrue(self.store.set_review(second, "rejected", "知识库过旧"))
        stats = self.store.stats()
        self.assertEqual(stats["review"]["confirmed"], 1)
        self.assertEqual(stats["review"]["rejected"], 1)
        self.assertEqual(stats["review"]["precision_after_review"], 0.5)

    def test_review_and_delete_endpoints(self):
        event_id = self._detect("hallucination", severity="high", needs_review=True)
        app = create_app(self.settings, worker=False)
        client = TestClient(app)
        try:
            response = client.post(
                f"/api/monitor/events/{event_id}/review",
                json={"status": "confirmed", "note": "已人工确认"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["review_status"], "confirmed")
            self.assertEqual(
                client.get(f"/api/monitor/events/{event_id}").json()["review_note"], "已人工确认"
            )
            self.assertEqual(
                client.post(
                    f"/api/monitor/events/{event_id}/review", json={"status": "maybe"}
                ).status_code,
                422,
            )
            self.assertIn('monitor_review_total', client.get("/metrics").text)
            self.assertEqual(client.delete(f"/api/monitor/events/{event_id}").status_code, 200)
            self.assertEqual(client.get(f"/api/monitor/events/{event_id}").status_code, 404)
        finally:
            app.state.store.close()


class RetentionTests(unittest.TestCase):
    """保留期清理与个人信息删除。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_purge_removes_expired_events(self):
        event_id, _ = self.store.ingest(make_event(), sampled=True)
        self.store.conn.execute("UPDATE events SET created_at=?", ("2000-01-01T00:00:00+00:00",))
        self.store.conn.commit()
        result = self.store.purge(30)
        self.assertEqual(result["events"], 1)
        self.assertIsNone(self.store.get(event_id))

    def test_purge_disabled_when_retention_zero(self):
        self.store.ingest(make_event(), sampled=True)
        self.assertEqual(self.store.purge(0)["events"], 0)
        self.assertEqual(self.store.stats()["events"]["total"], 1)

    def test_delete_event_removes_detection(self):
        event_id, _ = self.store.ingest(make_event(), sampled=True)
        self.store.save_detection(
            DetectionRecord(
                event_id=event_id,
                verdict="hallucination",
                types=["policy_error"],
                severity="high",
                reason="测试结论",
                claims=[],
                detector="llm",
            )
        )
        self.assertIsNotNone(self.store.detection_for(event_id))
        self.assertTrue(self.store.delete_event(event_id))
        self.assertIsNone(self.store.get(event_id))
        self.assertIsNone(self.store.detection_for(event_id))
        self.assertFalse(self.store.delete_event(event_id))


class IngestLimitTests(unittest.TestCase):
    """接入上限、读接口上限与读接口安全默认。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.settings = Settings(
            db_path=self.tmp.name,
            api_tokens=("test-token-1",),
            require_auth=True,
            rate_limit_per_minute=0,
            knowledge_dir=str(ROOT / "knowledge"),
            max_batch_items=2,
        )
        self.app = create_app(self.settings, worker=False)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.state.store.close()
        os.unlink(self.tmp.name)

    def _events(self, count):
        return [
            {"session_id": f"s-{index}", "user_question": "问题", "system_reply": "回复"}
            for index in range(count)
        ]

    def test_over_limit_batch_rejected(self):
        headers = {"Authorization": "Bearer test-token-1"}
        over = self.client.post(
            "/api/ingest/events", json={"events": self._events(3)}, headers=headers
        )
        self.assertEqual(over.status_code, 413)
        ok = self.client.post(
            "/api/ingest/events", json={"events": self._events(2)}, headers=headers
        )
        self.assertEqual(ok.status_code, 202)

    def test_read_limit_is_clamped(self):
        response = self.client.get("/api/monitor/events?limit=100000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["limit"], 200)

    def test_read_endpoint_fails_closed_when_exposed_without_token(self):
        settings = Settings(
            db_path=self.tmp.name,
            api_tokens=("test-token-1",),
            require_auth=True,
            bind_host="0.0.0.0",
            panel_token="",
        )
        app = create_app(settings, worker=False)
        client = TestClient(app)
        try:
            self.assertEqual(client.get("/api/monitor/stats").status_code, 503)
            self.assertEqual(client.get("/metrics").status_code, 200)
        finally:
            app.state.store.close()




class TenantAndAuditTests(unittest.TestCase):
    """企业形态：多租户隔离、复核审计、告警重投与就绪检查。"""

    KEY_A = "sk-tenant-a-aaa"
    KEY_B = "sk-tenant-b-bbb"
    KEY_ADMIN = "sk-ops-ccc"

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.settings = Settings(
            db_path=self.tmp.name,
            knowledge_dir=str(ROOT / "knowledge"),
            api_keys_raw=(
                f"{self.KEY_A}|tenant-a|ingest,read|0|0;"
                f"{self.KEY_B}|tenant-b|ingest,read|0|0;"
                f"{self.KEY_ADMIN}|ops|ingest,read,admin|0|0"
            ),
            require_auth=True,
            require_read_auth=True,
            rate_limit_per_minute=0,
            max_batch_items=200,
        )
        self.app = create_app(self.settings, worker=False)
        self.client = TestClient(self.app)
        self.store = self.app.state.store

    def tearDown(self):
        self.app.state.store.close()
        os.unlink(self.tmp.name)

    def _ingest(self, key: str, session: str = "s-1"):
        response = self.client.post(
            "/api/ingest/events",
            json={
                "session_id": session,
                "user_question": "退款多久到账？",
                "system_reply": "1-3 个工作日原路退回。",
            },
            headers={"X-Monitor-Token": key},
        )
        self.assertEqual(response.status_code, 202)
        return response.json()["event_id"]

    def test_ingest_requires_tenant_key(self):
        self.assertEqual(
            self.client.post(
                "/api/ingest/events",
                json={"session_id": "s", "user_question": "q", "system_reply": "r"},
            ).status_code,
            401,
        )

    def test_events_are_isolated_per_tenant(self):
        event_id = self._ingest(self.KEY_A)
        own = self.client.get(
            "/api/monitor/events", headers={"X-Monitor-Token": self.KEY_A}
        ).json()
        other = self.client.get(
            "/api/monitor/events", headers={"X-Monitor-Token": self.KEY_B}
        ).json()
        admin = self.client.get("/api/monitor/stats", headers={"X-Monitor-Token": self.KEY_ADMIN})
        self.assertEqual(own["total"], 1)
        self.assertEqual(other["total"], 0)
        self.assertEqual(admin.status_code, 200)
        denied = self.client.get(
            f"/api/monitor/events/{event_id}", headers={"X-Monitor-Token": self.KEY_B}
        )
        self.assertEqual(denied.status_code, 404)
        detail = self.client.get(
            f"/api/monitor/events/{event_id}", headers={"X-Monitor-Token": self.KEY_A}
        )
        self.assertEqual(detail.json()["tenant"], "tenant-a")

    def test_review_records_audit_fields(self):
        event_id = self._ingest(self.KEY_A)
        self.store.save_detection(
            DetectionRecord(
                event_id=event_id,
                verdict="hallucination",
                types=["policy_error"],
                severity="high",
                reason="测试结论",
                claims=[],
                detector="llm",
                needs_review=True,
            )
        )
        response = self.client.post(
            f"/api/monitor/events/{event_id}/review",
            json={"status": "rejected", "note": "知识库过旧"},
            headers={"X-Monitor-Token": self.KEY_A},
        )
        # tenant-a 的 Key 没有 admin 权限，写操作必须被拒绝
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            f"/api/monitor/events/{event_id}/review",
            json={"status": "rejected", "note": "知识库过旧"},
            headers={"X-Monitor-Token": self.KEY_ADMIN},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reviewer"], "sk-ops***")
        doc = self.store.get(event_id)
        self.assertEqual(doc.review_status, "rejected")
        self.assertEqual(doc.review_note, "知识库过旧")
        self.assertIsNotNone(doc.reviewed_at)

    def test_readyz_reports_missing_ingest_key(self):
        healthy = self.client.get("/readyz")
        self.assertEqual(healthy.status_code, 200)
        broken_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        broken_tmp.close()
        try:
            app = create_app(Settings(db_path=broken_tmp.name, require_auth=True), worker=False)
            client = TestClient(app)
            try:
                response = client.get("/readyz")
                self.assertEqual(response.status_code, 503)
                self.assertIn("ingest_auth", response.json()["checks"])
            finally:
                app.state.store.close()
        finally:
            os.unlink(broken_tmp.name)

    def test_webhook_failure_is_retried_and_can_be_replayed(self):
        event_id = self._ingest(self.KEY_A)
        doc = self.store.get(event_id)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("webhook down")
            return httpx.Response(200, text="ok")

        settings = Settings(
            db_path=self.tmp.name,
            alert_webhook_url="https://hook.invalid/notify",
            alert_min_severity="high",
            alert_dedup_minutes=0,
        )
        router = AlertRouter(self.store, settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            record = DetectionRecord(
                event_id=event_id,
                verdict="hallucination",
                types=["capability_overreach"],
                severity="high",
                reason="能力越界",
                claims=[],
                detector="rule",
            )
            status = router.route(doc, record)
            self.assertEqual(status, "failed")
            scheduled = self.store.list_alerts()[0]
            self.assertEqual(scheduled["status"], "failed")
            self.assertGreaterEqual(scheduled["attempts"], 1)  # 失败已安排退避重投
            self.assertIsNotNone(scheduled["next_attempt_at"])
            # 把退避时间提前，验证重投成功后状态流转为 sent
            self.store.conn.execute("UPDATE alerts SET next_attempt_at=NULL")
            self.store.conn.commit()
            self.assertEqual(router.retry_failed(), 1)
            alerts = self.store.list_alerts()
            self.assertEqual(alerts[0]["status"], "sent")
            self.assertGreaterEqual(alerts[0]["attempts"], 1)
        finally:
            router.close()

    def test_alert_replay_endpoint(self):
        event_id = self._ingest(self.KEY_A)
        self.store.record_alert(
            "alert-1", event_id, "high", "webhook", "fp-1", "failed", {"k": "v"}
        )
        response = self.client.post(
            "/api/monitor/alerts/alert-1/replay", headers={"X-Monitor-Token": self.KEY_ADMIN}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.list_alerts()[0]["status"], "queued")
        listing = self.client.get(
            "/api/monitor/alerts", headers={"X-Monitor-Token": self.KEY_ADMIN}
        )
        self.assertEqual(listing.status_code, 200)



class UpstreamCredentialTests(unittest.TestCase):
    """监测平台转发到批量检测服务时，必须带上上游 API Key。"""

    def test_authorization_header_is_sent(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            if request.url.path.endswith("/api/checks"):
                return httpx.Response(202, json={"task_id": "t-1"})
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "results": [
                        {
                            "id": "e-1",
                            "verdict": "no_hallucination",
                            "types": [],
                            "severity": None,
                            "reason": "依据支持",
                            "claims": [],
                        }
                    ],
                    "errors": [],
                },
            )

        settings = Settings(
            upstream_url="http://upstream.invalid",
            upstream_mode="mock",
            upstream_api_key="sk-upstream-0001",
        )
        client = UpstreamClient(settings, transport=httpx.MockTransport(handler))
        try:
            event = make_event(event_id="e-1")
            record = client.detect(event, "知识依据")
        finally:
            client.close()
        self.assertEqual(record.verdict, "no_hallucination")
        self.assertTrue(seen and all(h.get("authorization") == "Bearer sk-upstream-0001" for h in seen))

if __name__ == "__main__":
    unittest.main()
