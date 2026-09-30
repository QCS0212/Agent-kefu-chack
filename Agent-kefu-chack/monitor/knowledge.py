"""知识自动装配。

替换“每条数据人工贴 knowledge_base”的做法：按 category/sku/store_id 从知识源检索，
把命中的业务资料组装成核验依据，并记录知识版本，供结果可复现与知识缺口统计。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol


class KnowledgeSource(Protocol):
    """知识源协议：实现 retrieve 即可接入政策库、商品库、配置中心。"""

    def retrieve(self, *, category: str | None, sku: str | None, store_id: str | None) -> str: ...


@dataclass
class KnowledgeSnippet:
    text: str
    version: str
    matched_by: list[str] = field(default_factory=list)
    source: str = "file"

    def short_version(self) -> str:
        return self.version[:10]


def _version_of(path: Path) -> str:
    stat = path.stat()
    raw = f"{path}|{stat.st_mtime_ns}".encode()
    return sha256(raw).hexdigest()


def scan_at_mention(text: str, names: list[str]) -> list[str]:
    """检测回复中提到知识里不存在的人/地/品牌名（如“张经理”）。"""
    hits: list[str] = []
    for name in names:
        if name and name in text:
            hits.append(name)
    return hits


@dataclass
class FileKnowledgeSource:
    """基于目录的示例知识源。

    knowledge/
      policy.json     # 全局政策
      categories.json # 类目 -> 条款
      skus.json       # SKU -> 商品参数
      contacts.json   # 联系人/仓库名单（用于发现编造的联系人）
    """

    directory: Path

    def __post_init__(self):
        self.directory = Path(self.directory)
        self.names = self._load_names()

    def _load_json(self, name: str) -> dict:
        path = self.directory / name
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _load_names(self) -> list[str]:
        contacts = self._load_json("contacts.json")
        names: list[str] = []
        for value in contacts.values():
            if isinstance(value, str):
                names.append(value)
            elif isinstance(value, list):
                names.extend(str(item) for item in value if isinstance(item, str))
        return names

    def retrieve(self, *, category, sku, store_id) -> str:
        parts: list[str] = []
        matched: list[str] = []
        sections = [
            ("policy", "全局政策", self._load_json("policy.json")),
            ("categories", "类目条款", self._load_json("categories.json")),
            ("skus", "商品参数", self._load_json("skus.json")),
        ]
        for key, label, mapping in sections:
            hit = None
            if key == "skus" and sku:
                hit = mapping.get(sku) or mapping.get("default")
                if mapping.get(sku):
                    matched.append(f"sku:{sku}")
            elif key == "categories" and category:
                hit = mapping.get(category) or mapping.get("default")
                if mapping.get(category):
                    matched.append(f"category:{category}")
            elif key == "policy":
                hit = mapping.get("default") or mapping.get("store", {}).get(store_id or "")
                if hit:
                    matched.append("policy:default")
            if isinstance(hit, str) and hit.strip():
                parts.append(f"【{label}】{hit.strip()}")
        if not parts:
            parts.append("（知识库未命中：未找到与该类目或商品对应的业务资料）")
        version = _version_of(self.directory / "policy.json") if (self.directory / "policy.json").exists() else "none"
        self._last = KnowledgeSnippet(text="\n".join(parts), version=version, matched_by=matched)
        return self._last.text

    def last_snippet(self) -> KnowledgeSnippet | None:
        return getattr(self, "_last", None)


# 数字类模式都带边界，避免把身份证号里的片段误当成手机号
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ORDER = re.compile(r"\b[A-Z]{2,4}\d{6,}[-\d]*\b")
_LANDLINE = re.compile(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(text: str) -> str:
    """脱敏手机号、座机、身份证、银行卡、邮箱与订单号，发往外部 LLM 前必须执行。"""
    redacted = _PHONE.sub("[手机号]", text)
    redacted = _LANDLINE.sub("[座机]", redacted)
    redacted = _ID_CARD.sub("[身份证号]", redacted)
    redacted = _BANK_CARD.sub("[银行卡号]", redacted)
    redacted = _EMAIL.sub("[邮箱]", redacted)
    return _ORDER.sub("[订单号]", redacted)


@dataclass
class KnowledgeGap:
    """知识缺口：客服答复中作出了确定性断言，但知识库无对应资料。"""

    pattern: str
    example: str
    category: str | None = None
    count: int = 0
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


CERTAINTY_MARKERS = ["保证", "一定", "100%", "肯定会", "必须", "我已经", "已帮您", "已经为您"]


def find_certainty_claims(reply: str, knowledge: str) -> list[str]:
    """找出“确定性承诺”且知识中没有对应依据的表述。"""
    if not knowledge or "知识库未命中" in knowledge:
        return []
    sentences = [s.strip() for s in re.split(r"[。！；]", reply) if s.strip()]
    gaps: list[str] = []
    for sentence in sentences:
        if any(marker in sentence for marker in CERTAINTY_MARKERS):
            gaps.append(sentence)
    return gaps


# 从回复中识别专属称谓的启发式模式：中文姓名通常一字姓，仓库名通常两字地名。
# 这只是规则快检的辅助信号，覆盖常见写法，不追求穷尽所有称谓。
CONTACT_PATTERN = re.compile(r"[一-龥](?:经理|主管|店长|负责人)")
WAREHOUSE_PATTERN = re.compile(r"[一-龥]{2}仓")
# 通用职务前缀（如“客服经理”“值班主管”）：不构成专属联系人的证据，命中即跳过。
GENERIC_NAME_PREFIXES = {
    "客服", "售后", "值班", "当班", "门店", "区域", "客户", "运营",
    "项目", "产品", "销售", "服务", "物流", "仓储", "前台", "电话",
}


def _generic_window(reply: str, match: re.Match) -> str:
    start = match.start()
    return (reply[start - 1] if start > 0 else "") + reply[start]


def _candidate_names(reply: str) -> list[str]:
    """提取回复中形如「张经理」「杭州仓」的专属称谓候选。"""
    candidates: list[str] = []
    for pattern in (CONTACT_PATTERN, WAREHOUSE_PATTERN):
        for match in pattern.finditer(reply):
            if _generic_window(reply, match) in GENERIC_NAME_PREFIXES:
                continue
            candidates.append(match.group(0))
    return candidates


def scan_unsupported_names(reply: str, knowledge: str, names: list[str]) -> list[str]:
    """编造联系人/仓库名：回复里出现知识库未登记的专属名词。

    两类信号：名单内的联系人出现在回复中但知识依据未提供；回复出现名单之外的同类称谓。
    规则只提示可疑，命中后仍需人工复核，不代表已经证实的结论。
    """
    hits: list[str] = []
    for name in names:
        if name and name in reply and name not in knowledge and name not in hits:
            hits.append(name)
    for candidate in _candidate_names(reply):
        if candidate in names or candidate in knowledge or candidate in hits:
            continue
        hits.append(candidate)
    return hits
