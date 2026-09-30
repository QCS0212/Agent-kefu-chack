"""商城知识源适配器测试：接口契约、失败回退、与工作循环的接线。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("LOG_CONFIGURE", "0")
os.environ["MONITOR_API_KEYS"] = ""
os.environ["MONITOR_KNOWLEDGE_SOURCE"] = "file"
os.environ["MONITOR_API_TOKENS"] = ""
os.environ["MONITOR_PANEL_TOKEN"] = ""

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor.config import Settings  # noqa: E402
from monitor.knowledge import FileKnowledgeSource  # noqa: E402
from monitor.models import DetectionRecord, IngestEvent  # noqa: E402
from monitor.shop_knowledge import ShopKnowledgeSource  # noqa: E402
from monitor.sampling import SamplingPolicy  # noqa: E402
from monitor.store import Store  # noqa: E402
from monitor.rules import RuleEngine  # noqa: E402
from monitor.alerts import AlertRouter  # noqa: E402
from monitor.metrics import Metrics  # noqa: E402
from monitor.worker import MonitorWorker, build_knowledge_source  # noqa: E402


def make_event(**overrides) -> IngestEvent:
    params = {
        "session_id": "sess-shop-1",
        "user_question": "我的票能退吗？",
        "system_reply": "可以退，我帮您申请。",
    }
    params.update(overrides)
    return IngestEvent(**params)


class FakeUpstream:
    """记录 worker 收到的知识文本，用于验证知识源是否生效。"""

    def __init__(self):
        self.knowledge: list[str] = []

    def detect_many(self, pairs):
        outcomes = {}
        for event, knowledge in pairs:
            self.knowledge.append(knowledge)
            outcomes[event.event_id] = DetectionRecord(
                event_id=event.event_id,
                verdict="no_hallucination",
                types=[],
                severity=None,
                reason="依据支持",
                claims=[],
                detector="llm",
            )
        return outcomes

    def probe(self):
        return {"status": "ok"}

    def close(self):
        pass


class ShopKnowledgeTests(unittest.TestCase):
    def test_parses_api_response_wrapper(self):
        calls = {}

        def fake_post(url, payload, token, timeout):
            calls.update({"url": url, "payload": payload, "token": token, "timeout": timeout})
            return {
                "code": 200,
                "msg": "ok",
                "data": {
                    "knowledge": "【退改政策】该节目为条件退\n【订单执行记录】订单 1001 已退单",
                    "version": "2026-09-30T20:00:00+08:00",
                    "matched_by": ["program:1001", "order:1001"],
                    "source": "zhixuanpiao",
                },
            }

        source = ShopKnowledgeSource(
            url="http://shop.invalid/api/knowledge", token="shop-token", timeout=3, post=fake_post
        )
        text = source.retrieve(category="after_sale", sku="1001", store_id="S1")
        self.assertIn("条件退", text)
        self.assertEqual(calls["payload"], {"category": "after_sale", "sku": "1001", "store_id": "S1"})
        self.assertEqual(calls["token"], "shop-token")
        self.assertEqual(calls["timeout"], 3)
        snippet = source.last_snippet()
        self.assertEqual(snippet.source, "zhixuanpiao")
        self.assertEqual(snippet.version, "2026-09-30T20:00:00+08:00")
        self.assertEqual(snippet.matched_by, ["program:1001", "order:1001"])

    def test_plain_body_without_wrapper(self):
        source = ShopKnowledgeSource(
            url="http://shop.invalid/api/knowledge",
            post=lambda *_: {"knowledge": "【政策】不支持退", "matched_by": "program:9"},
        )
        text = source.retrieve(category="after_sale", sku=None, store_id=None)
        self.assertIn("不支持退", text)
        self.assertEqual(source.last_snippet().matched_by, ["program:9"])

    def test_failure_falls_back_to_file_source(self):
        fallback = FileKnowledgeSource(ROOT / "knowledge")
        source = ShopKnowledgeSource(
            url="http://shop.invalid/api/knowledge",
            fallback=fallback,
            post=lambda *_: (_ for _ in ()).throw(OSError("connection refused")),
        )
        text = source.retrieve(category="after_sale", sku=None, store_id=None)
        self.assertIn("商城知识接口不可用", text)
        self.assertIn("【全局政策】", text)  # 本地 knowledge/policy.json 的内容
        self.assertEqual(source.last_snippet().source, "shop_fallback")

    def test_failure_without_fallback_marks_unavailable(self):
        source = ShopKnowledgeSource(
            url="http://shop.invalid/api/knowledge",
            post=lambda *_: (_ for _ in ()).throw(ValueError("bad json")),
        )
        text = source.retrieve(category="payment", sku=None, store_id=None)
        self.assertIn("知识库未命中", text)
        self.assertIn("不可核验", text)
        self.assertEqual(source.last_snippet().source, "shop_unavailable")

    def test_empty_knowledge_treated_as_failure(self):
        source = ShopKnowledgeSource(url="http://x/y", post=lambda *_: {"data": {"knowledge": "  "}})
        text = source.retrieve(category="after_sale", sku=None, store_id=None)
        self.assertIn("知识库未命中", text)

    def test_build_knowledge_source_switches_by_config(self):
        self.assertIsInstance(build_knowledge_source(Settings()), FileKnowledgeSource)
        shop = build_knowledge_source(
            Settings(knowledge_source="shop", shop_knowledge_url="http://shop.invalid/k")
        )
        self.assertIsInstance(shop, ShopKnowledgeSource)
        # 配了 shop 但没给 URL 时退回本地知识库，避免静默失去依据
        self.assertIsInstance(
            build_knowledge_source(Settings(knowledge_source="shop", shop_knowledge_url="")),
            FileKnowledgeSource,
        )

    def test_worker_uses_shop_knowledge(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            store = Store(tmp.name)
            settings = Settings(db_path=tmp.name, knowledge_dir=str(ROOT / "knowledge"))
            source = ShopKnowledgeSource(
                url="http://shop.invalid/k",
                post=lambda *_: {
                    "data": {"knowledge": "【退改政策】该节目不支持退票", "matched_by": ["program:1001"]}
                },
            )
            upstream = FakeUpstream()
            worker = MonitorWorker(
                settings=settings,
                store=store,
                upstream=upstream,
                knowledge_source=source,
                rules=RuleEngine([]),
                sampling=SamplingPolicy(rate=1.0),
                alerts=AlertRouter(store, settings),
                metrics=Metrics(),
            )
            event_id, _ = store.ingest(
                make_event(category="after_sale", system_reply="这个票可以退。"), sampled=True
            )
            worker.process_pending()
            self.assertTrue(upstream.knowledge)
            self.assertIn("不支持退票", upstream.knowledge[0])
            doc = store.get(event_id)
            self.assertEqual(doc.detection.detector, "llm")
            self.assertEqual(doc.detection.knowledge_version, "shop")
            store.close()
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()