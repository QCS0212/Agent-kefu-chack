"""Run real detection with per-case checkpoints, then evaluate independently."""
import argparse
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from app.llm import LLMDetector, PROMPT_VERSION, PROMPT
from app.schema import load_rows, save_json
from app.evaluate import evaluate
from app.report import write_report

ROOT = Path(__file__).resolve().parent.parent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = ROOT/'data/task4_replies.json'
    rows = load_rows(source, ('user_question','system_reply','knowledge_base'))
    detector = LLMDetector()
    prompt_hash=sha256(PROMPT.encode()).hexdigest()
    input_hash=sha256(source.read_bytes()).hexdigest()
    path=args.output/'predictions.json'
    run={'schema_version':1,'mode':'llm','created_at':datetime.now(timezone.utc).isoformat(),
         'input_sha256':input_hash,'model':detector.model,'endpoint':detector.endpoint,'prompt_version':PROMPT_VERSION,
         'prompt_sha256':prompt_hash,'temperature':0,'results':[],'errors':[]}
    if path.exists():
        old=json.loads(path.read_text(encoding='utf-8'))
        if any(old.get(k)!=run[k] for k in ('input_sha256','model','endpoint','prompt_sha256')):
            raise ValueError('运行配置已改变，请使用新目录')
        run=old
    save_json(args.output/'prompt.json',{'version':PROMPT_VERSION,'system':PROMPT})
    done={r['id'] for r in run['results']}
    for row in rows[:args.limit]:
        if row['id'] in done: continue
        run['errors']=[r for r in run['errors'] if r['id']!=row['id']]
        try:
            result=detector.detect(row);run['results'].append(result)
            print(f"{row['id']} 完成：{result['verdict']}",flush=True)
        except ValueError as exc:
            run['errors'].append({'id':row['id'],'error':str(exc)})
            print(f"{row['id']} 失败：{exc}",flush=True)
        finally:
            if detector.last_response:
                save_json(args.output/'raw'/f"{row['id']}.json",detector.last_response)
            save_json(path,run)
        if run['errors'] and not run['results']:
            raise SystemExit('首条未完成，已保存诊断，停止后续请求。')
    if len(run['results'])!=len(rows) or run['errors']:
        print(f"部分完成：{len(run['results'])}/{len(rows)}；错误 {len(run['errors'])}");return
    # Only now read ground truth; no label is passed into the detector.
    truth=load_rows(ROOT/'data/task4_ground_truth.json',())
    metrics=evaluate(run,truth);save_json(args.output/'metrics.json',metrics)
    by_id={r['id']:r for r in rows};labels={r['id']:r for r in truth};pred={r['id']:r for r in run['results']}
    mistakes=[{'kind':kind,'source':by_id[i],'prediction':pred[i],'ground_truth':labels[i]}
              for kind,key in [('false_positive','false_positive_ids'),('false_negative','false_negative_ids')] for i in metrics[key]]
    save_json(args.output/'errors.json',mistakes)
    write_report(args.output/'report.html',run,metrics,rows)
    print(json.dumps(metrics,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__': main()
