import json
from pathlib import Path

VERDICTS = {"hallucination", "no_hallucination", "not_verifiable"}
CATEGORIES = {
    "policy_error": "政策与优惠错误",
    "product_error": "产品事实错误",
    "business_fabrication": "业务信息编造",
    "capability_overreach": "能力越界与虚假执行",
    "safety_misleading": "安全提示失真",
    "misleading_omission": "条件遗漏与过度概括",
}

def load_rows(path, required):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("输入必须为非空 JSON 数组")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("每条数据必须为对象")
        for field in ("id", *required):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"字段缺失或不是非空字符串: {field}")
        if row["id"] in seen:
            raise ValueError(f"重复 ID: {row['id']}")
        seen.add(row["id"])
    return rows

def validate_prediction(result, row):
    if not isinstance(result, dict):
        raise ValueError("检测结果必须为对象")
    if result.get("id") != row["id"] or result.get("verdict") not in VERDICTS:
        raise ValueError("检测 ID 或 verdict 不合法")
    if not isinstance(result.get("types"), list) or any(t not in CATEGORIES for t in result["types"]):
        raise ValueError("分类不合法")
    if result.get("severity") not in {"low", "medium", "high", None}:
        raise ValueError("严重程度不合法")
    if not isinstance(result.get("reason"), str) or not result["reason"].strip():
        raise ValueError("缺少判断理由")
    if not isinstance(result.get("claims"), list):
        raise ValueError("claims 必须为数组")
    for claim in result["claims"]:
        if not isinstance(claim, dict) or claim.get("relation") not in {"supported", "contradicted", "unsupported", "not_verifiable"}:
            raise ValueError("事实声明不合法")
        for key, source in (("reply_quote", "system_reply"), ("knowledge_quote", "knowledge_base")):
            quote = claim.get(key)
            if not isinstance(quote, str) or (key == "reply_quote" and not quote) or quote not in row[source]:
                raise ValueError(f"引用不存在于输入: {key}")
        if claim["relation"] == "contradicted" and not claim["knowledge_quote"].strip():
            raise ValueError("冲突声明缺少知识引用")
    problems = any(c["relation"] in {"contradicted", "unsupported"} for c in result["claims"])
    if result["verdict"] == "hallucination" and not problems:
        raise ValueError("幻觉判断缺少冲突或无依据声明")
    if result["verdict"] == "no_hallucination" and (problems or result["types"] or result["severity"] is not None):
        raise ValueError("正常判断与声明或分类矛盾")
    if result["verdict"] == "hallucination" and (not result["types"] or not result["claims"] or result["severity"] is None):
        raise ValueError("幻觉判断必须有分类、严重程度和事实依据")
    return result

def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
