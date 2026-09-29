"""Small single-process task runner with atomic disk checkpoints."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from app.providers import get_detector
from app.schema import validate_prediction
from app.report import write_report
from app.evaluate import evaluate


def now(): return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, root, factory=get_detector):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.factory=factory
        self.lock=threading.RLock()
        self.executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='kefu')
        self.jobs={}
        for path in self.root.glob('*/task.json'):
            job=json.loads(path.read_text(encoding='utf-8'))
            if job['status'] in ('queued','running'):
                job['status']='interrupted';job['error']='服务重启中断了任务；请重新提交。'
                job['updated_at']=now()
            self.jobs[job['task_id']]=job
            self.save(job)

    def save(self, job):
        folder=self.root/job['task_id'];folder.mkdir(exist_ok=True)
        temp=folder/'task.json.tmp'
        temp.write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(folder/'task.json')

    def get(self, task_id):
        with self.lock:
            if task_id not in self.jobs: raise KeyError(task_id)
            return json.loads(json.dumps(self.jobs[task_id]))

    def submit(self, mode, items):
        with self.lock:
            active=sum(j['status'] in ('queued','running') for j in self.jobs.values())
            if active>=8: raise OverflowError('任务队列已满，请稍后再试')
            # Validate configuration before accepting a paid task, but do not call the API here.
            detector=self.factory(mode)
            task_id=str(uuid4())
            job={'task_id':task_id,'status':'queued','mode':mode,'created_at':now(),'updated_at':now(),
                 'total':len(items),'completed':0,'failed':0,'results':[],'errors':[],
                 'evaluation':None,'report_ready':False,'items':items,
                 'model':getattr(detector,'model',None),
                 'input_sha256':sha256(json.dumps(items,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
            if mode=='llm':
                from app.llm import PROMPT, PROMPT_VERSION
                job.update(prompt_version=PROMPT_VERSION,prompt_sha256=sha256(PROMPT.encode()).hexdigest(),temperature=0)
            self.jobs[task_id]=job;self.save(job)
            self.executor.submit(self.work,task_id,detector)
            return task_id

    def predictions(self, job):
        return {k:job[k] for k in ('mode','created_at','model','input_sha256','results','errors')}

    def report(self, job):
        evaluation=job['evaluation'] or {'metrics':None,'note':
            '模拟结果不计算正式检出率。' if job['mode']=='mock' else '未提交人工标签，仅展示检测结果，不计算检出率。'}
        folder=self.root/job['task_id'];temp=folder/'report.tmp.html'
        write_report(temp,self.predictions(job),evaluation,job['items'])
        temp.replace(folder/'report.html')
        job['report_ready']=True

    def work(self, task_id, detector):
        job=self.jobs[task_id]
        try:
            with self.lock:
                job['status']='running';job['updated_at']=now();self.save(job)
            for index,row in enumerate(job['items']):
                try:
                    result=validate_prediction(detector.detect(row),row)
                    error=None
                except Exception as exc:
                    result=None
                    error={'id':row['id'],'code':'DETECTION_FAILED','message':
                           str(exc) if isinstance(exc,ValueError) else '检测执行失败，请检查服务端配置或上游状态。'}
                raw=getattr(detector,'last_response',None)
                if raw:
                    folder=self.root/task_id/'raw';folder.mkdir(exist_ok=True)
                    (folder/f'{index+1:03}.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
                with self.lock:
                    if error:job['errors'].append(error);job['failed']+=1
                    else:job['results'].append(result);job['completed']+=1
                    job['updated_at']=now();self.save(job)
            with self.lock:
                if job['failed']:
                    job['status']='partial_failed' if job['completed'] else 'failed'
                else:
                    self.report(job);job['status']='completed'
                job['updated_at']=now();self.save(job)
        except Exception:
            with self.lock:
                job['status']='failed';job['error']='任务保存或报告生成失败，请检查服务端。'
                job['updated_at']=now();self.save(job)

    def score(self, task_id, labels):
        with self.lock:
            job=self.jobs[task_id]
            if job['status']!='completed':raise RuntimeError('任务尚未完整完成，不能评估')
            result=evaluate(self.predictions(job),labels)
            job['evaluation']=result
            self.report(job)
            job['updated_at']=now();self.save(job)
            path=self.root/task_id/'evaluation.json'
            path.write_text(json.dumps({'labels':labels,'evaluation':result},ensure_ascii=False,indent=2),encoding='utf-8')
            return result

    def close(self):self.executor.shutdown(wait=True)
