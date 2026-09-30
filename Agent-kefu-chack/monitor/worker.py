"""后台工作循环。

领取待处理事件 -> 装配知识 -> 规则快检 -> 上游批量检测 -> 结果落库 -> 告警路由。
上游失败先按指数退避重试，重试耗尽才降级为 not_verifiable；评估层不伪造任何结论。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .alerts import AlertRouter
from .config import Settings
from .knowledge import FileKnowledgeSource
from .metrics import Metrics
from .models import DetectionError, DetectionRecord, EventDocument, IngestEvent
from .mysql_knowledge import MysqlKnowledgeSource, ShopDbConfig
from .rules import RuleEngine, attach_event_id
from .sampling import SamplingPolicy
from .shop_knowledge import ShopKnowledgeSource
from .store import Store
from .upstream import UpstreamClient, fallback_record

META_LAST_PURGE = "last_purge_at"
PURGE_INTERVAL_MINUTES = 30

logger = logging.getLogger("monitor.worker")


def _event_from_doc(doc: EventDocument) -> IngestEvent:
    """从存储视图还原检测所需的事件载荷。"""
    return IngestEvent(
        event_id=doc.event_id,
        session_id=doc.session_id,
        user_question=doc.user_question,
        system_reply=doc.system_reply,
        category=doc.category,
        sku=doc.sku,
        store_id=doc.store_id,
        knowledge_snippet=doc.knowledge_snippet,
    )


class MonitorWorker:
    """单进程事件消费者。claim 原子抢占，多 worker 线程安全。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        upstream: UpstreamClient,
        knowledge_source: FileKnowledgeSource,
        rules: RuleEngine,
        sampling: SamplingPolicy,
        alerts: AlertRouter,
        metrics: Metrics,
    ):
        self.settings = settings
        self.store = store
        self.upstream = upstream
        self.knowledge_source = knowledge_source
        self.rules = rules
        self.sampling = sampling
        self.alerts = alerts
        self.metrics = metrics

    # ------------------------------------------------------------------ 单条路径

    def process_event(self, event_id: str) -> DetectionRecord:
        """处理单条事件（人工重跑或调试用）。重试已排队时抛 RETRY_SCHEDULED。"""
        doc = self.store.get(event_id)
        if doc is None:
            raise DetectionError("EVENT_MISSING", f"事件不存在: {event_id}")

        event = _event_from_doc(doc)
        knowledge, knowledge_version, matched_by = self._assemble_knowledge(event)

        record = self._rule_check(event, knowledge, event_id, knowledge_version)
        if record is not None:
            self.store.save_detection(record, knowledge_matched=matched_by)
            self._route(event_id, record)
            return record

        try:
            record = self.upstream.detect(event, knowledge)
        except DetectionError as exc:
            record = self._handle_upstream_failure(event_id, exc)
            if record is None:
                raise DetectionError("RETRY_SCHEDULED", f"{exc.code}: 已安排重试") from None
            return record
        self.metrics.inc("monitor_upstream_calls_total", result="ok")
        record.knowledge_version = record.knowledge_version or knowledge_version
        self.store.save_detection(record, knowledge_matched=matched_by)
        self._route(event_id, record)
        return record

    # ------------------------------------------------------------------ 批处理路径

    def process_pending(self, batch: int | None = None) -> int:
        """处理一批待处理事件：规则快检短路，其余合并成一次上游批量请求。"""
        ids = self.store.claim(batch or self.settings.worker_batch)
        if ids:
            try:
                self._process_batch(ids)
            except Exception:
                logger.exception("批处理异常，未完成事件标记为失败")
                for event_id in ids:
                    doc = self.store.get(event_id)
                    if doc is not None and doc.status == "running":
                        self.store.mark_failed(event_id, "WORKER_ERROR")
        try:
            self.alerts.flush_digest()
        except Exception:
            logger.exception("日报发送失败")
        try:
            self.alerts.retry_failed()
        except Exception:
            logger.exception("告警重投失败")
        try:
            self._maintain()
        except Exception:
            logger.exception("保留期清理失败")
        return len(ids)

    def _process_batch(self, ids: list[str]) -> None:
        pending: list[tuple[IngestEvent, str, str, list[str]]] = []
        for event_id in ids:
            doc = self.store.get(event_id)
            if doc is None:
                continue
            event = _event_from_doc(doc)
            knowledge, version, matched = self._assemble_knowledge(event)
            record = self._rule_check(event, knowledge, event_id, version)
            if record is not None:
                self.store.save_detection(record, knowledge_matched=matched)
                self._route(event_id, record)
                continue
            pending.append((event, knowledge, version, matched))
        if pending:
            self._detect_batch(pending)

    def _detect_batch(self, calls: list[tuple[IngestEvent, str, str, list[str]]]) -> None:
        """一次批量转发：单批最多 20 条，配合上游 2 并发任务显著提高吞吐。"""
        pairs = [(event, knowledge) for event, knowledge, _version, _matched in calls]
        try:
            outcomes = self.upstream.detect_many(pairs)
        except DetectionError as exc:
            logger.warning("上游批次失败（%s），按事件重试：%s", exc.code, exc.message)
            for event, _knowledge, _version, _matched in calls:
                self._handle_upstream_failure(event.event_id, exc)
            return
        for event, _knowledge, version, matched in calls:
            outcome = outcomes.get(event.event_id)
            if isinstance(outcome, DetectionError):
                self._handle_upstream_failure(event.event_id, outcome)
                continue
            if outcome is None:
                self._handle_upstream_failure(
                    event.event_id,
                    DetectionError("UPSTREAM_DETECTION_FAILED", "上游未返回该事件的结果"),
                )
                continue
            self.metrics.inc("monitor_upstream_calls_total", result="ok")
            record = outcome
            record.knowledge_version = record.knowledge_version or version
            self.store.save_detection(record, knowledge_matched=matched)
            self._route(event.event_id, record)

    def _handle_upstream_failure(self, event_id: str, error: DetectionError) -> DetectionRecord | None:
        """上游失败：先指数退避重试，重试耗尽才落库为不可核验。

        返回 None 表示已排队重试；返回 DetectionRecord 表示已放弃并落库。
        """
        attempts = self.store.attempts_of(event_id)
        self.metrics.inc("monitor_fallback_total", code=error.code)
        if attempts < self.settings.retry_max_attempts:
            delay = self.settings.retry_backoff_seconds * (2 ** attempts)
            self.store.requeue(event_id, delay_seconds=delay, error=f"{error.code}: {error.message}")
            self.metrics.inc("monitor_retry_total", code=error.code)
            logger.warning(
                "上游检测失败 %s（第 %s 次尝试），%s 秒后重试：%s",
                event_id,
                attempts + 1,
                delay,
                error.message,
            )
            return None
        logger.error("上游检测重试耗尽 %s，标记为不可核验：%s", event_id, error.message)
        self.metrics.inc("monitor_unresolved_total", code=error.code)
        record = fallback_record(event_id, error)
        self.store.save_detection(record)
        self._route(event_id, record)
        return record

    # ------------------------------------------------------------------ 内部工具

    def _assemble_knowledge(self, event: IngestEvent) -> tuple[str, str, list[str]]:
        """外系统自带的依据优先；否则按类目/SKU/门店从知识源装配。"""
        snippet = (event.knowledge_snippet or "").strip()
        if snippet:
            return snippet, "provided", ["provided"]
        text = self.knowledge_source.retrieve(
            category=event.category, sku=event.sku, store_id=event.store_id
        )
        last = self.knowledge_source.last_snippet()
        return text, (last.version if last else "unknown"), (last.matched_by if last else [])

    def _rule_check(
        self, event: IngestEvent, knowledge: str, event_id: str, knowledge_version: str
    ) -> DetectionRecord | None:
        if not self.settings.rule_fast_check:
            return None
        record = attach_event_id(self.rules.check(event, knowledge), event_id)
        if record is None:
            return None
        record.knowledge_version = knowledge_version
        self.metrics.inc("monitor_upstream_calls_total", result="rule_short_circuit")
        logger.info("规则快检命中 %s: %s %s", event_id, record.verdict, record.types)
        return record

    def _route(self, event_id: str, record: DetectionRecord) -> None:
        doc = self.store.get(event_id)
        if doc is None:
            return
        try:
            self.alerts.route(doc, record)
        except Exception:
            logger.exception("告警路由失败: %s", event_id)

    def _maintain(self) -> None:
        """按保留期清理历史会话；默认 30 天，0 表示永久保留。"""
        days = self.settings.retention_days
        if days <= 0:
            return
        last = self.store.get_meta(META_LAST_PURGE)
        moment = datetime.now(timezone.utc)
        if last:
            try:
                if moment - datetime.fromisoformat(last) < timedelta(minutes=PURGE_INTERVAL_MINUTES):
                    return
            except ValueError:
                pass
        result = self.store.purge(days)
        self.store.set_meta(META_LAST_PURGE, moment.isoformat())
        if any(result.values()):
            logger.info("保留期清理（%s 天）：%s", days, result)

    # ------------------------------------------------------------------ 循环

    def run_loop(self, stop_event: threading.Event) -> None:
        logger.info(
            "监测工作循环已启动：间隔 %ss，批量 %s，重试上限 %s 次",
            self.settings.worker_interval_seconds,
            self.settings.worker_batch,
            self.settings.retry_max_attempts,
        )
        while not stop_event.is_set():
            try:
                processed = self.process_pending()
            except Exception:
                logger.exception("工作循环异常")
                processed = 0
            if processed == 0:
                stop_event.wait(self.settings.worker_interval_seconds)
        logger.info("监测工作循环已停止")

    def close(self) -> None:
        self.upstream.close()
        self.alerts.close()


def build_knowledge_source(settings: Settings):
    """按配置选择知识源：商城只读接口 / 只读库优先，失败按配置回退本地 knowledge/ 目录。"""
    file_source = FileKnowledgeSource(Path(settings.knowledge_dir))
    if settings.knowledge_source == "mysql":
        return MysqlKnowledgeSource(
            config=ShopDbConfig(
                host=settings.shop_db_host,
                port=settings.shop_db_port,
                user=settings.shop_db_user,
                password=settings.shop_db_password,
                program_db=settings.shop_db_program,
                order_db=settings.shop_db_order,
                pay_db=settings.shop_db_pay,
                db_suffixes=settings.shop_db_suffixes,
                timeout=settings.shop_timeout,
            ),
            fallback=file_source if settings.shop_fallback_file else None,
        )
    if settings.knowledge_source == "shop" and settings.shop_knowledge_url:
        return ShopKnowledgeSource(
            url=settings.shop_knowledge_url,
            token=settings.shop_token,
            timeout=settings.shop_timeout,
            fallback=file_source if settings.shop_fallback_file else None,
        )
    return file_source


def build_worker(settings: Settings, store: Store) -> MonitorWorker:
    knowledge_source = build_knowledge_source(settings)
    # 联系人名单始终来自本地 knowledge/contacts.json：商城/库知识源不提供该名单
    names = getattr(knowledge_source, "names", None)
    if names is None:
        names = FileKnowledgeSource(Path(settings.knowledge_dir)).names
    return MonitorWorker(
        settings=settings,
        store=store,
        upstream=UpstreamClient(settings),
        knowledge_source=knowledge_source,
        rules=RuleEngine(names),
        sampling=SamplingPolicy(
            rate=settings.sample_rate, always_sample=settings.always_sample
        ),
        alerts=AlertRouter(store, settings),
        metrics=Metrics(),
    )