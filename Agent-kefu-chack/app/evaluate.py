from app.schema import VERDICTS

def evaluate(predictions, truth):
    if predictions.get("mode") not in {"mock", "llm"}:
        raise ValueError("未知运行模式")
    if predictions.get("errors"):
        raise ValueError("本次运行有技术失败，不能生成完整评估")
    rows = predictions["results"]
    def index(items):
        result = {}
        for row in items:
            if row["id"] in result:
                raise ValueError(f"重复 ID: {row['id']}")
            result[row["id"]] = row
        return result
    predicted, actual = index(rows), index(truth)
    if predicted.keys() != actual.keys():
        raise ValueError("预测与人工标签 ID 集合不一致，不能静默忽略缺失案例")
    if any(type(r.get("is_hallucination")) is not bool for r in truth):
        raise ValueError("人工标签必须是布尔值")
    if any(r.get("verdict") not in VERDICTS for r in rows):
        raise ValueError("预测 verdict 不合法")
    pending = [r["id"] for r in rows if r["verdict"] == "not_verifiable"]
    base = {"mode": predictions["mode"], "total": len(rows), "pending_ids": pending}
    if predictions["mode"] == "mock":
        return {**base, "status": "simulation_only", "metrics": None,
                "note": "模拟结果不计算正式检出率。"}
    tp, fp, fn, tn = [], [], [], []
    for case_id, row in predicted.items():
        detected = row["verdict"] == "hallucination"
        label = actual[case_id]["is_hallucination"]
        (tp if detected and label else fp if detected else fn if label else tn).append(case_id)
    def ratio(a, b):
        return a / b if b else None
    return {**base, "status": "evaluated", "false_positive_ids": fp, "false_negative_ids": fn,
            "metrics": {"tp": len(tp), "fp": len(fp), "fn": len(fn), "tn": len(tn),
                        "precision": ratio(len(tp), len(tp) + len(fp)),
                        "recall": ratio(len(tp), len(tp) + len(fn)),
                        "f1": ratio(2 * len(tp), 2 * len(tp) + len(fp) + len(fn)),
                        "accuracy": ratio(len(tp) + len(tn), len(rows))},
            "note": "仅明确判为幻觉视为检出；证据不足的真实幻觉计为漏检。"}
