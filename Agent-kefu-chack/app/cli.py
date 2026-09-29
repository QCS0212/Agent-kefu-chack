import argparse
import json
from pathlib import Path
from app.detect import detect
from app.evaluate import evaluate
from app.report import write_report
from app.schema import load_rows, save_json

ROOT = Path(__file__).resolve().parent.parent

def main():
    parser = argparse.ArgumentParser(description="客服回复幻觉检测基础版")
    sub = parser.add_subparsers(dest="command")
    demo = sub.add_parser("demo", help="运行 mock 演示并生成报告")
    demo.add_argument("--output-dir", type=Path, default=ROOT / "outputs/mock")
    detector = sub.add_parser("detect", help="独立检测，不读取人工标签")
    detector.add_argument("--mode", choices=["mock", "llm"], default="mock")
    detector.add_argument("--input", type=Path, default=ROOT / "data/task4_replies.json")
    detector.add_argument("--output", type=Path, default=ROOT / "outputs/mock/predictions.json")
    evaluator = sub.add_parser("evaluate", help="独立评估")
    evaluator.add_argument("--predictions", type=Path, required=True)
    evaluator.add_argument("--truth", type=Path, default=ROOT / "data/task4_ground_truth.json")
    evaluator.add_argument("--output", type=Path, default=ROOT / "outputs/metrics.json")
    args = parser.parse_args()
    try:
        if args.command in (None, "demo"):
            output = getattr(args, "output_dir", ROOT / "outputs/mock")
            predictions = detect(ROOT / "data/task4_replies.json", "mock")
            truth = load_rows(ROOT / "data/task4_ground_truth.json", ())
            evaluation = evaluate(predictions, truth)
            save_json(output / "predictions.json", predictions)
            save_json(output / "metrics.json", evaluation)
            write_report(output / "report.html", predictions, evaluation,
                         load_rows(ROOT / "data/task4_replies.json", ()))
            print(f"MOCK 演示完成：{len(predictions['results'])} 条。未进行真实检测，不提供实测检出率。")
            print(f"报告：{output / 'report.html'}")
        elif args.command == "detect":
            save_json(args.output, detect(args.input, args.mode))
            print(f"结果：{args.output}")
        else:
            predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
            evaluation = evaluate(predictions, load_rows(args.truth, ()))
            save_json(args.output, evaluation)
            print(json.dumps(evaluation, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"错误：{exc}\n")
