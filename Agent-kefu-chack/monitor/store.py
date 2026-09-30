"""SQLite 持久化与幂等队列。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .models import Claim, DetectionRecord, EventDocument, IngestEvent, now
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED)


def dedup_key_of(event: IngestEvent) -> str:
    raw = f"{event.session_id}|{event.user_question}|{event.system_reply}"
    return sha256(raw.encode("utf-8")).hexdigest()


def days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class Store:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self.reset_running()

    def _migrate(self) -> None:
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS events("
            " event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE,"
            " category TEXT, sku TEXT, store_id TEXT, user_question TEXT NOT NULL,"
            " system_reply TEXT NOT NULL, knowledge_snippet TEXT, occurred_at TEXT,"
            " payload TEXT NOT NULL, status TEXT NOT NULL, sampled INTEGER NOT NULL DEFAULT 1,"
            " attempts INTEGER NOT NULL DEFAULT 0, error TEXT,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS idx_events_status_created ON events(status, created_at);"
            "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);"
            "CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);"
            "CREATE TABLE IF NOT EXISTS detections("
            " event_id TEXT PRIMARY KEY, verdict TEXT NOT NULL, types TEXT NOT NULL,"
            " severity TEXT, reason TEXT NOT NULL, claims TEXT NOT NULL, detector TEXT NOT NULL,"
            " knowledge_version TEXT, knowledge_matched TEXT NOT NULL,"
            " latency_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
            " FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE);"
            "CREATE INDEX IF NOT EXISTS idx_detections_verdict ON detections(verdict);"
            "CREATE INDEX IF NOT EXISTS idx_detections_severity ON detections(severity);"
            "CREATE TABLE IF NOT EXISTS alerts("
            " alert_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, severity TEXT NOT NULL,"
            " channel TEXT NOT NULL, dedup_key TEXT NOT NULL, status TEXT NOT NULL,"
            " payload TEXT, response TEXT, created_at TEXT NOT NULL, sent_at TEXT,"
            " FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE);"
            "CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);"
            "CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts(dedup_key, severity);"
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
        )
        self.conn.commit()
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """为已有数据库补齐新增列，升级时不需要重建库。"""
        wanted = {
            "events": {
                "next_attempt_at": "TEXT",
                "review_status": "TEXT NOT NULL DEFAULT 'pending'",
                "review_note": "TEXT",
                "reviewed_at": "TEXT",
            },
            "detections": {"needs_review": "INTEGER NOT NULL DEFAULT 0"},
        }
        with self._lock:
            for table, columns in wanted.items():
                existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns.items():
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def ingest(self, event: IngestEvent, sampled: bool) -> tuple[str, bool]:
        """写入一条事件，返回 (event_id, created)。dedup_key 命中时返回既有 id。"""
        key = dedup_key_of(event)
        with self._lock:
            existing = self.conn.execute(
                "SELECT event_id FROM events WHERE dedup_key=?", (key,)
            ).fetchone()
            if existing:
                return existing["event_id"], False
            event_id = uuid4().hex
            stamp = now()
            self.conn.execute(
                "INSERT INTO events(event_id, session_id, dedup_key, category, sku, store_id,"
                " user_question, system_reply, knowledge_snippet, occurred_at, payload,"
                " status, sampled, attempts, error, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?)",
                (
                    event_id,
                    event.session_id,
                    key,
                    event.category,
                    event.sku,
                    event.store_id,
                    event.user_question,
                    event.system_reply,
                    event.knowledge_snippet,
                    event.occurred_at or stamp,
                    json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                    STATUS_PENDING if sampled else STATUS_SKIPPED,
                    1 if sampled else 0,
                    stamp,
                    stamp,
                ),
            )
            self.conn.commit()
            return event_id, True

    def reset_running(self) -> None:
        """重启恢复：上次中断的检测任务回到待处理。"""
        with self._lock:
            self.conn.execute(
                "UPDATE events SET status=?, updated_at=? WHERE status=?",
                (STATUS_PENDING, now(), STATUS_RUNNING),
            )
            self.conn.commit()

    def claim(self, batch: int) -> list[str]:
        """原子抢占到期的待处理事件，避免多 worker 重复消费。"""
        if batch <= 0:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT event_id FROM events"
                " WHERE status=? AND sampled=1 AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
                " ORDER BY created_at ASC LIMIT ?",
                (STATUS_PENDING, now(), batch),
            ).fetchall()
            ids = [row["event_id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE events SET status=?, updated_at=? WHERE event_id IN ({placeholders})",
                    (STATUS_RUNNING, now(), *ids),
                )
                self.conn.commit()
            return ids

    def mark_failed(self, event_id: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE events SET status=?, error=?, attempts=attempts+1, next_attempt_at=NULL,"
                " updated_at=? WHERE event_id=?",
                (STATUS_FAILED, error[:500], now(), event_id),
            )
            self.conn.commit()

    def requeue(self, event_id: str, delay_seconds: float = 0.0, error: str | None = None) -> int:
        """放回待处理队列（可延迟），返回累计尝试次数：人工重跑与失败重试共用。"""
        when = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay_seconds))).isoformat()
        with self._lock:
            self.conn.execute(
                "UPDATE events SET status=?, error=?, attempts=attempts+1,"
                " next_attempt_at=?, updated_at=? WHERE event_id=?",
                (STATUS_PENDING, (error or "")[:500] or None, when, now(), event_id),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT attempts FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def attempts_of(self, event_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT attempts FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def save_detection(self, record: DetectionRecord, knowledge_matched: list[str] | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO detections(event_id, verdict, types, severity, reason, claims,"
                " detector, needs_review, knowledge_version, knowledge_matched, latency_ms, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(event_id) DO UPDATE SET verdict=excluded.verdict, types=excluded.types,"
                " severity=excluded.severity, reason=excluded.reason, claims=excluded.claims,"
                " detector=excluded.detector, needs_review=excluded.needs_review,"
                " knowledge_version=excluded.knowledge_version,"
                " knowledge_matched=excluded.knowledge_matched, latency_ms=excluded.latency_ms",
                (
                    record.event_id,
                    record.verdict,
                    json.dumps(record.types, ensure_ascii=False),
                    record.severity,
                    record.reason,
                    json.dumps([c.model_dump(mode="json") for c in record.claims], ensure_ascii=False),
                    record.detector,
                    1 if record.needs_review else 0,
                    record.knowledge_version,
                    json.dumps(knowledge_matched or [], ensure_ascii=False),
                    record.latency_ms,
                    record.created_at or now(),
                ),
            )
            self.conn.execute(
                "UPDATE events SET status=?, error=NULL, next_attempt_at=NULL, updated_at=?"
                " WHERE event_id=?",
                (STATUS_DONE, now(), record.event_id),
            )
            self.conn.commit()

    def _detection_from_row(self, row: sqlite3.Row) -> DetectionRecord:
        return DetectionRecord(
            event_id=row["event_id"],
            verdict=row["verdict"],
            types=json.loads(row["types"]),
            severity=row["severity"],
            reason=row["reason"],
            claims=[Claim(**item) for item in json.loads(row["claims"])],
            detector=row["detector"],
            needs_review=bool(row["needs_review"]),
            knowledge_version=row["knowledge_version"],
            latency_ms=row["latency_ms"],
            created_at=row["created_at"],
        )

    def detection_for(self, event_id: str) -> DetectionRecord | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM detections WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._detection_from_row(row) if row else None

    def get(self, event_id: str) -> EventDocument | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if not row:
                return None
            detection = self.detection_for(event_id)
        return EventDocument(
            event_id=row["event_id"],
            session_id=row["session_id"],
            category=row["category"],
            sku=row["sku"],
            store_id=row["store_id"],
            status=row["status"],
            sampled=bool(row["sampled"]),
            created_at=row["created_at"],
            detection=detection,
            error=row["error"],
            user_question=row["user_question"],
            system_reply=row["system_reply"],
            knowledge_snippet=row["knowledge_snippet"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            review_status=row["review_status"] or "pending",
            review_note=row["review_note"],
            reviewed_at=row["reviewed_at"],
        )

    def list_events(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        verdict: str | None = None,
        category: str | None = None,
        needs_review: bool | None = None,
        review_status: str | None = None,
    ) -> dict:
        where = []
        params: list = []
        if status:
            where.append("e.status=?")
            params.append(status)
        if verdict:
            where.append("d.verdict=?")
            params.append(verdict)
        if category:
            where.append("e.category=?")
            params.append(category)
        if needs_review is not None:
            where.append("COALESCE(d.needs_review, 0)=?")
            params.append(1 if needs_review else 0)
        if review_status:
            where.append("e.review_status=?")
            params.append(review_status)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM events e LEFT JOIN detections d ON d.event_id=e.event_id {clause}",
                params,
            ).fetchone()["n"]
            rows = self.conn.execute(
                f"SELECT e.event_id FROM events e LEFT JOIN detections d ON d.event_id=e.event_id"
                f" {clause} ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        items = [self.get(row["event_id"]) for row in rows]
        return {"total": total, "items": items}

    def stats(self, days: int | None = None) -> dict:
        scope = ""
        sampled_scope = "WHERE e.sampled=1"
        params: list = []
        if days and days > 0:
            scope = "WHERE e.created_at>=?"
            sampled_scope = "WHERE e.created_at>=? AND e.sampled=1"
            params = [days_ago(days)]
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM events e {scope}", params
            ).fetchone()["n"]
            sampled = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM events e {sampled_scope}", params
            ).fetchone()["n"]
            status_rows = self.conn.execute(
                f"SELECT e.status AS k, COUNT(*) AS n FROM events e {scope} GROUP BY e.status", params
            ).fetchall()
            verdict_rows = self.conn.execute(
                f"SELECT d.verdict AS k, COUNT(*) AS n FROM detections d"
                f" JOIN events e ON e.event_id=d.event_id {scope} GROUP BY d.verdict",
                params,
            ).fetchall()
            severity_rows = self.conn.execute(
                f"SELECT d.severity AS k, COUNT(*) AS n FROM detections d"
                f" JOIN events e ON e.event_id=d.event_id {scope} GROUP BY d.severity",
                params,
            ).fetchall()
            detection_rows = self.conn.execute(
                f"SELECT d.types AS types, d.latency_ms AS latency_ms, d.detector AS detector,"
                f" d.needs_review AS needs_review, d.knowledge_matched AS knowledge_matched"
                f" FROM detections d"
                f" JOIN events e ON e.event_id=d.event_id {scope}",
                params,
            ).fetchall()
            alert_rows = self.conn.execute(
                f"SELECT a.status AS k, COUNT(*) AS n FROM alerts a"
                f" JOIN events e ON e.event_id=a.event_id {scope} GROUP BY a.status",
                params,
            ).fetchall()
            review_rows = self.conn.execute(
                f"SELECT e.review_status AS k, COUNT(*) AS n FROM detections d"
                f" JOIN events e ON e.event_id=d.event_id {scope} GROUP BY e.review_status",
                params,
            ).fetchall()
        by_type: dict[str, int] = {}
        latencies: list[int] = []
        knowledge_hits = 0
        fallback_count = 0
        needs_review_count = 0
        for row in detection_rows:
            for item in json.loads(row["types"] or "[]"):
                by_type[item] = by_type.get(item, 0) + 1
            latencies.append(int(row["latency_ms"] or 0))
            if json.loads(row["knowledge_matched"] or "[]"):
                knowledge_hits += 1
            if row["detector"] == "fallback":
                fallback_count += 1
            if row["needs_review"]:
                needs_review_count += 1
        latencies.sort()
        verdicts = {row["k"]: row["n"] for row in verdict_rows}
        detected = sum(verdicts.values())
        hallucination = verdicts.get("hallucination", 0)
        degraded = verdicts.get("not_verifiable", 0)
        decisive = detected - degraded
        review = {row["k"] or "pending": row["n"] for row in review_rows}
        confirmed = review.get("confirmed", 0)
        rejected = review.get("rejected", 0)
        index95 = min(len(latencies) - 1, int(len(latencies) * 0.95)) if latencies else 0
        return {
            "window_days": days,
            "events": {
                "total": total,
                "sampled": sampled,
                "by_status": {row["k"]: row["n"] for row in status_rows},
            },
            "detections": {
                "total": detected,
                "decisive": decisive,
                "degraded": degraded,
                "fallback": fallback_count,
                "needs_review": needs_review_count,
                "by_verdict": verdicts,
                "by_severity": {row["k"] or "unrated": row["n"] for row in severity_rows},
                "by_type": dict(sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)),
                "knowledge_matched": knowledge_hits,
                "knowledge_missing": len(detection_rows) - knowledge_hits,
                "latency_avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
                "latency_p50_ms": latencies[len(latencies) // 2] if latencies else 0,
                "latency_p95_ms": latencies[index95] if latencies else 0,
            },
            "review": {
                "pending": review.get("pending", 0),
                "confirmed": confirmed,
                "rejected": rejected,
                "precision_after_review": round(confirmed / (confirmed + rejected), 4)
                if (confirmed + rejected)
                else None,
            },
            "hallucination_rate": round(hallucination / decisive, 4) if decisive else None,
            "coverage": round(decisive / detected, 4) if detected else None,
            "degraded_rate": round(degraded / detected, 4) if detected else None,
            "alerts": {row["k"]: row["n"] for row in alert_rows},
        }

    def timeseries(self, days: int = 7) -> list[dict]:
        days = max(1, min(days, 90))
        with self._lock:
            rows = self.conn.execute(
                "SELECT date(e.created_at) AS day, d.verdict AS verdict, COUNT(*) AS n"
                " FROM events e LEFT JOIN detections d ON d.event_id=e.event_id"
                " WHERE e.created_at>=? GROUP BY day, d.verdict",
                (days_ago(days - 1),),
            ).fetchall()
        buckets: dict[str, dict] = {}
        for offset in range(days):
            day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - offset)).date().isoformat()
            buckets[day] = {
                "bucket": day,
                "total": 0,
                "hallucination": 0,
                "no_hallucination": 0,
                "not_verifiable": 0,
            }
        for row in rows:
            bucket = buckets.get(row["day"])
            if not bucket:
                continue
            bucket["total"] += row["n"]
            if row["verdict"]:
                bucket[row["verdict"]] = bucket.get(row["verdict"], 0) + row["n"]
        return list(buckets.values())

    def knowledge_gaps(self, limit: int = 10) -> list[dict]:
        """知识缺口：检出结论非“正常”，且装配知识时一条都没命中。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT e.category AS category, COUNT(*) AS hits, MAX(e.created_at) AS last_seen"
                " FROM detections d JOIN events e ON e.event_id=d.event_id"
                " WHERE d.knowledge_matched='[]' AND d.verdict!='no_hallucination'"
                " GROUP BY e.category ORDER BY hits DESC LIMIT ?",
                (limit,),
            ).fetchall()
            gaps = []
            for row in rows:
                example = self.conn.execute(
                    "SELECT e.session_id, e.system_reply FROM detections d"
                    " JOIN events e ON e.event_id=d.event_id"
                    " WHERE d.knowledge_matched='[]' AND e.category IS ?"
                    " ORDER BY d.created_at DESC LIMIT 1",
                    (row["category"],),
                ).fetchone()
                gaps.append(
                    {
                        "category": row["category"] or "unknown",
                        "hits": row["hits"],
                        "last_seen": row["last_seen"],
                        "example_session": example["session_id"] if example else None,
                        "example_reply": example["system_reply"] if example else None,
                    }
                )
        return gaps

    def alert_seen_within(self, dedup_key: str, severity: str, minutes: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM alerts"
                " WHERE dedup_key=? AND severity=? AND created_at>=?",
                (dedup_key, severity, days_ago(minutes / 60 / 24)),
            ).fetchone()
        return row["n"] > 0

    def record_alert(
        self,
        alert_id: str,
        event_id: str,
        severity: str,
        channel: str,
        dedup_key: str,
        status: str,
        payload: dict | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO alerts(alert_id, event_id, severity, channel, dedup_key, status,"
                " payload, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    alert_id,
                    event_id,
                    severity,
                    channel,
                    dedup_key,
                    status,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    now(),
                ),
            )
            self.conn.commit()

    def claim_digest(self) -> list[sqlite3.Row]:
        """取出待聚合发送的告警并标记为 sending，事务内完成避免重复投递。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT alert_id, event_id, severity, payload FROM alerts"
                " WHERE status='queued' ORDER BY created_at"
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE alerts SET status='sending' WHERE alert_id=?", (row["alert_id"],)
                )
            if rows:
                self.conn.commit()
        return rows

    def mark_alert(self, alert_id: str, status: str, response: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE alerts SET status=?, response=?, sent_at=? WHERE alert_id=?",
                (status, (response or "")[:1000], now(), alert_id),
            )
            self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def set_review(self, event_id: str, status: str, note: str | None = None) -> bool:
        """记录人工复核结论：confirmed（确认是幻觉）/ rejected（误报）。"""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE events SET review_status=?, review_note=?, reviewed_at=?, updated_at=?"
                " WHERE event_id=?",
                (status, note, now(), now(), event_id),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    def delete_event(self, event_id: str) -> bool:
        """删除一条会话及其检测与告警，用于个人信息删除请求。"""
        with self._lock:
            cursor = self.conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))
            self.conn.commit()
        return cursor.rowcount > 0

    def purge(self, retention_days: int) -> dict:
        """清理超过保留期的会话、检测结果与告警；retention_days<=0 表示永久保留。"""
        if retention_days <= 0:
            return {"events": 0, "detections": 0, "alerts": 0}
        cutoff = days_ago(retention_days)
        with self._lock:
            stale = "event_id IN (SELECT event_id FROM events WHERE created_at<?)"
            alerts = self.conn.execute(
                f"DELETE FROM alerts WHERE {stale}", (cutoff,)
            ).rowcount
            detections = self.conn.execute(
                f"DELETE FROM detections WHERE {stale}", (cutoff,)
            ).rowcount
            events = self.conn.execute("DELETE FROM events WHERE created_at<?", (cutoff,)).rowcount
            self.conn.commit()
        return {"events": events, "detections": detections, "alerts": alerts}
