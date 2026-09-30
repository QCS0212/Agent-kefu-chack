"""合并多次 HTTP 检测任务的结果，生成一次完整评估与报告。

用法（在项目根目录执行）：

    .venv/bin/python tools/merge_api_results.py TASK_ID [TASK_ID ...]

任务目录来自 outputs/api/<task_id>/，它们必须覆盖同一批样本且不重复。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evaluate import evaluate  # noqa: E402
from app.report import write_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="合并多次检测任务并独立评估")
    parser.add_argument("task_ids", nargs="+", help="outputs/api 下的任务 ID")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/api-real-merged")
    parser.add_argument("--replies", type=Path, default=ROOT / "data/task4_replies.json")
    parser.add_argument("--truth", type=Path, default=ROOT / "data/task4_ground_truth.json")
    args = parser.parse_args()

    merged: dict[str, dict] = {}
    model = None
    for task_id in args.task_ids:
        job_path = ROOT / "outputs" / "api" / task_id / "task.json"
        if not job_path.exists():
            parser.error(f"找不到任务文件：{job_path}")
        job = json.loads(job_path.read_text(encoding="utf-8"))
        model = model or job.get("model")
        for result in job.get("results", []):
            if result["id"] in merged:
                print(f"警告：{result['id']} 出现多次，使用后出现的任务结果", file=sys.stderr)
            merged[result["id"]] = result

    rows = json.loads(args.replies.read_text(encoding="utf-8"))
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    missing = [row["id"] for row in rows if row["id"] not in merged]
    if missing:
        # 只做显式报错，不静默取交集
        raise SystemExit(f"以下样本没有任何任务结果，无法评估：{missing}")

    predictions = {
        "schema_version": 1,
        "mode": "llm",
        "created_at": None,
        "model": model,
        "input_sha256": None,
        "results": [merged[row["id"]] for row in rows],
        "errors": [],
    }
    evaluation = evaluate(predictions, truth)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    write_report(out / "report.html", predictions, evaluation, rows)
    (out / "predictions.json").write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "metrics.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("merged items:", len(merged))
    print(json.dumps(evaluation["metrics"], ensure_ascii=False, indent=2))
    print("report:", out / "report.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())