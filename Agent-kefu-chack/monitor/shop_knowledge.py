"""商城知识源：把商城（如智选票）的真实业务数据接成检测依据。

背景：客服/机器人回复里的事实，需要和商城的真实数据核对，否则只能长期停留在"不可核验"。
本模块通过一个只读接口把商城的政策、节目信息与订单/退款执行记录装配成核验依据。

接口契约（商城侧实现，POST + JSON，兼容 zhixuanpiao 的 ApiResponse 包装）：

    请求  {"category": "after_sale", "sku": "1001", "store_id": null, "question": "可以退款吗？"}
    响应  {"code": 200, "msg": "ok", "data": {
              "knowledge": "【退改政策】…\n【订单执行记录】…",
              "version": "2026-09-30T20:00:00+08:00",
              "matched_by": ["program:1001", "order:202609300001"],
              "source": "zhixuanpiao"}}

约定：
- 只读、幂等、超时可重试；商城侧不得因该调用产生业务副作用。
- 接口不可用时回退到本地 knowledge/ 目录（可配置关闭），并把原因写进「知识库未命中」提示，
  让检测结果落成 not_verifiable 而不是误判为幻觉。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .knowledge import FileKnowledgeSource, KnowledgeSnippet

logger = logging.getLogger("monitor.shop_knowledge")

UNAVAILABLE_TEMPLATE = (
    "（知识库未命中：商城知识接口不可用（{reason}）。"
    "请把回复中的事实声明标注为不可核验，不要臆测结论。）"
)


def _default_post(url: str, payload: dict, token: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Monitor-Token": token,
            "User-Agent": "kefu-monitor/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


@dataclass
class ShopKnowledgeSource:
    """通过只读接口取商城真实依据。实现与 FileKnowledgeSource 相同的 retrieve 协议。"""

    url: str
    token: str = ""
    timeout: float = 5.0
    fallback: FileKnowledgeSource | None = None
    timeout_reason: str = "timeout"
    post: Callable[[str, dict, str, float], dict] = _default_post
    last: KnowledgeSnippet | None = field(default=None, init=False)

    def last_snippet(self) -> KnowledgeSnippet | None:
        return self.last

    def retrieve(self, *, category: str | None, sku: str | None, store_id: str | None) -> str:
        payload = {
            "category": category,
            "sku": sku,
            "store_id": store_id,
        }
        try:
            body = self.post(self.url, payload, self.token, self.timeout)
            snippet = self._parse(body)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, KeyError) as exc:
            logger.warning("商城知识接口调用失败：%s", exc)
            snippet = self._from_fallback(category, sku, store_id, reason=type(exc).__name__)
        self.last = snippet
        return snippet.text

    # ------------------------------------------------------------------ 内部

    def _parse(self, body: Any) -> KnowledgeSnippet:
        if not isinstance(body, dict):
            raise ValueError("响应必须是 JSON 对象")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        text = data.get("knowledge") or data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("响应缺少 knowledge 字段")
        matched = data.get("matched_by")
        if isinstance(matched, str):
            matched = [matched]
        if not isinstance(matched, list):
            matched = []
        version = str(data.get("version") or data.get("updated_at") or "shop")
        source = str(data.get("source") or "shop")
        return KnowledgeSnippet(
            text=text.strip(),
            version=version,
            matched_by=[str(item) for item in matched],
            source=source,
        )

    def _from_fallback(
        self, category: str | None, sku: str | None, store_id: str | None, reason: str
    ) -> KnowledgeSnippet:
        if self.fallback is not None:
            text = self.fallback.retrieve(category=category, sku=sku, store_id=store_id)
            base = self.fallback.last_snippet()
            note = f"【商城知识接口不可用（{reason}），以下为本地知识库依据】"
            return KnowledgeSnippet(
                text=f"{note}\n{text}",
                version=(base.version if base else "fallback"),
                matched_by=(base.matched_by if base else []),
                source="shop_fallback",
            )
        return KnowledgeSnippet(
            text=UNAVAILABLE_TEMPLATE.format(reason=reason),
            version="unavailable",
            matched_by=[],
            source="shop_unavailable",
        )