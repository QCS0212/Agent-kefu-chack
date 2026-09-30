"""MySQL 商城知识源测试：订单/退款执行记录、节目政策、只读约束与降级。"""
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

from monitor.alerts import AlertRouter  # noqa: E402
from monitor.config import Settings  # noqa: E402
from monitor.knowledge import FileKnowledgeSource  # noqa: E402
from monitor.metrics import Metrics  # noqa: E402
from monitor.models import DetectionRecord, IngestEvent  # noqa: E402
from monitor.mysql_knowledge import MysqlKnowledgeSource, ShopDbConfig  # noqa: E402
from monitor.rules import RuleEngine  # noqa: E402
from monitor.sampling import SamplingPolicy  # noqa: E402
from monitor.store import Store  # noqa: E402
from monitor.worker import MonitorWorker, build_knowledge_source  # noqa: E402

ORDER = {
    "order_number": 202609300001,
    "program_id": 1001,
    "program_title": "音乐剧《测试》",
    "program_place": "上海大剧院",
    "program_show_time": "2026-10-01 19:30:00",
    "order_price": 488,
    "order_status": 4,
    "pay_order_time": "2026-09-20 10:00:00",
    "cancel_order_time": "2026-09-29 10:12:00",
    "edit_time": "2026-09-29 10:12:00",
}
PAY = {
    "out_order_no": "202609300001",
    "pay_amount": 488,
    "pay_bill_status": 2,
    "pay_time": "2026-09-20 10:00:05",
    "edit_time": "2026-09-20 10:00:05",
}
REFUND = {
    "out_order_no": "202609300001",
    "refund_amount": 488,
    "refund_status": 2,
    "refund_time": "2026-09-30 09:05:00",
    "reason": "用户申请退票",
    "edit_time": "2026-09-30 09:05:00",
}
PROGRAM = {
    "id": 1001,
    "title": "音乐剧《测试》",
    "actor": "某剧团",
    "place": "上海大剧院",
    "permit_refund": 1,
    "refund_ticket_rule": "开演前 24 小时可退，收取票面 10% 手续费",
    "entry_rule": "凭购票身份证入场",
    "delivery_instruction": "电子票，不提供纸质票",
    "child_purchase": "1.2 米以下儿童谢绝入场",
    "invoice_specification": "演出结束后 30 天内可申请电子发票",
    "real_ticket_purchase_rule": "实名购票，一证一票",
    "per_order_limit_purchase_count": 6,
    "per_account_limit_purchase_count": 6,
    "important_notice": "迟到观众需等待幕间入场",
    "kind_reminder": "演出票一经售出不退不换（条件退除外）",
    "edit_time": "2026-09-28 12:00:00",
}


class FakeCursor:
    """按表名+主键匹配返回预置数据，并记录所有执行过的 SQL。"""

    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple[str, tuple]] = []
        self._result = None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self._result = None
        for marker, key, row in self.rows:
            if f"`{marker}" in sql and str(key) == str(params[0]):
                self._result = row
                return
                # 命中即返回

    def fetchone(self):
        return self._result

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def close(self):
        pass


def fake_connect_factory(rows):
    def _connect(_config):
        return FakeConnection(rows)

    return _connect


class MysqlKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.config = ShopDbConfig(host="127.0.0.1", user="kefu_ro", password="secret")
        self.db_rows = [
            ("d_order_0", "202609300001", ORDER),
            ("d_pay_bill_0", "202609300001", PAY),
            ("d_refund_bill_0", "202609300001", REFUND),
            ("d_program_0", "1001", PROGRAM),
        ]

    def _source(self, rows=None, fallback=None):
        return MysqlKnowledgeSource(
            config=self.config,
            fallback=fallback,
            connect=fake_connect_factory(rows if rows is not None else self.db_rows),
        )

    def test_order_lookup_returns_execution_and_policy(self):
        source = self._source()
        text = source.retrieve(category="after_sale", sku="202609300001", store_id=None)
        self.assertIn("【订单执行记录】", text)
        self.assertIn("订单状态：已退单", text)
        self.assertIn("【支付账单】", text)
        self.assertIn("支付状态：已支付", text)
        self.assertIn("【退款账单】", text)
        self.assertIn("退款状态：已退款", text)
        self.assertIn("退款到账时间：2026-09-30 09:05:00", text)
        self.assertIn("【节目信息】", text)
        self.assertIn("退票政策：条件退票", text)
        self.assertIn("开演前 24 小时可退", text)
        snippet = source.last_snippet()
        self.assertEqual(snippet.source, "zhixuanpiao-mysql")
        self.assertEqual(snippet.version, "2026-09-30 09:05:00")
        self.assertIn("order:202609300001", snippet.matched_by)
        self.assertIn("refund:202609300001", snippet.matched_by)
        self.assertIn("program:1001", snippet.matched_by)

    def test_program_lookup_by_id(self):
        source = self._source(rows=[("d_program_0", "1001", PROGRAM)])
        text = source.retrieve(category="product", sku="1001", store_id=None)
        self.assertIn("发票说明：演出结束后 30 天内可申请电子发票", text)
        self.assertIn("儿童购票：1.2 米以下儿童谢绝入场", text)
        self.assertIn("每个账号最多购买：6 张", text)
        self.assertNotIn("【退款账单】", text)

    def test_store_id_is_used_when_sku_empty(self):
        source = self._source(rows=[("d_program_0", "1001", PROGRAM)])
        text = source.retrieve(category="product", sku=None, store_id="1001")
        self.assertIn("音乐剧《测试》", text)

    def test_only_select_statements_are_issued(self):
        source = self._source()
        source.retrieve(category="after_sale", sku="202609300001", store_id=None)
        connection = source.connect(self.config)
        for sql, _params in connection.cursor_obj.executed:
            self.assertTrue(sql.strip().upper().startswith("SELECT"), sql)

    def test_missing_key_is_unavailable(self):
        source = self._source()
        text = source.retrieve(category="after_sale", sku=None, store_id=None)
        self.assertIn("知识库未命中", text)
        self.assertEqual(source.last_snippet().source, "mysql_unavailable")

    def test_no_match_falls_back_to_file(self):
        source = self._source(rows=[], fallback=FileKnowledgeSource(ROOT / "knowledge"))
        text = source.retrieve(category="after_sale", sku="999999999", store_id=None)
        self.assertIn("商城库不可用", text)
        self.assertIn("【全局政策】", text)
        self.assertEqual(source.last_snippet().source, "mysql_fallback")

    def test_no_match_without_fallback_is_unverifiable(self):
        source = self._source(rows=[])
        text = source.retrieve(category="after_sale", sku="999999999", store_id=None)
        self.assertIn("不可核验", text)

    def test_connection_error_falls_back(self):
        def boom(_config):
            raise OSError("connection refused")

        source = MysqlKnowledgeSource(
            config=self.config, fallback=FileKnowledgeSource(ROOT / "knowledge"), connect=boom
        )
        text = source.retrieve(category="after_sale", sku="202609300001", store_id=None)
        self.assertIn("商城库不可用", text)

    def test_build_knowledge_source_supports_mysql(self):
        source = build_knowledge_source(
            Settings(knowledge_source="mysql", shop_db_user="kefu_ro", shop_db_password="x")
        )
        self.assertIsInstance(source, MysqlKnowledgeSource)

    def test_build_worker_accepts_non_file_knowledge_source(self):
        """回归：切到 mysql/shop 知识源时 build_worker 不能再依赖 FileKnowledgeSource.names。"""
        from monitor.worker import build_worker

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        store = Store(tmp.name)
        try:
            for source in ("mysql", "shop"):
                overwrite = {"knowledge_source": source, "shop_db_user": "kefu_ro"}
                if source == "shop":
                    overwrite["shop_knowledge_url"] = "http://shop.invalid/k"
                worker = build_worker(Settings(db_path=tmp.name, **overwrite), store)
                self.assertIsNotNone(worker)
                worker.upstream.close()
        finally:
            store.close()
            os.unlink(tmp.name)

    def test_worker_uses_mysql_knowledge(self):
        class FakeUpstream:
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

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        store = Store(tmp.name)
        try:
            # 关闭规则快检，确保该回复走到上游模型路径（本用例验证知识内容）
            settings = Settings(
                db_path=tmp.name, knowledge_dir=str(ROOT / "knowledge"), rule_fast_check=False
            )
            upstream = FakeUpstream()
            worker = MonitorWorker(
                settings=settings,
                store=store,
                upstream=upstream,
                knowledge_source=self._source(),
                rules=RuleEngine([]),
                sampling=SamplingPolicy(rate=1.0),
                alerts=AlertRouter(store, settings),
                metrics=Metrics(),
            )
            event_id, _ = store.ingest(
                IngestEvent(
                    session_id="s-mysql-1",
                    user_question="我这个订单退款到账了吗？",
                    system_reply="已经帮您退款了，1-3 个工作日到账。",
                    category="after_sale",
                    sku="202609300001",
                ),
                sampled=True,
            )
            worker.process_pending()
            self.assertTrue(upstream.knowledge)
            self.assertIn("退款状态：已退款", upstream.knowledge[0])
            self.assertEqual(store.get(event_id).detection.knowledge_version, "2026-09-30 09:05:00")
        finally:
            store.close()
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()