import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from fastapi.testclient import TestClient
from app.api import create_app
from app.providers import MockDetector

ITEM={'id':'case-1','user_question':'支持退货吗？','system_reply':'支持七天退货。','knowledge_base':'支持七天退货。'}

class APITests(unittest.TestCase):
    def wait(self,client,task_id):
        for _ in range(200):
            job=client.get('/api/checks/'+task_id).json()
            if job['status'] not in ('queued','running'):return job
            time.sleep(.01)
        self.fail('任务未完成')

    def test_mock_end_to_end_and_persisted_readback(self):
        with TemporaryDirectory() as d:
            with TestClient(create_app(d)) as c:
                self.assertEqual(c.get('/health').status_code,200)
                res=c.post('/api/checks',json={'mode':'mock','items':[ITEM]})
                self.assertEqual(res.status_code,202)
                tid=res.json()['task_id'];job=self.wait(c,tid)
                self.assertEqual(job['status'],'completed');self.assertEqual(job['completed'],1)
                self.assertIsNone(job['evaluation'])
                self.assertIn('客服回复检测报告',c.get(f'/api/checks/{tid}/report').text)
                res=c.post(f'/api/checks/{tid}/evaluate',json={'labels':[{'id':'case-1','is_hallucination':False}]})
                self.assertEqual(res.status_code,200);self.assertIsNone(res.json()['metrics'])
            with TestClient(create_app(d)) as c:
                self.assertEqual(c.get('/api/checks/'+tid).json()['status'],'completed')

    def test_validation_and_unknown_task(self):
        with TemporaryDirectory() as d, TestClient(create_app(d)) as c:
            for payload in ({'items':[]},{'mode':'bad','items':[ITEM]},{'items':[ITEM,ITEM]},
                            {'items':[{**ITEM,'ground_truth':True}]},{'items':[ITEM]*21}):
                self.assertEqual(c.post('/api/checks',json=payload).status_code,422)
            self.assertEqual(c.get('/api/checks/00000000-0000-0000-0000-000000000000').status_code,404)
            self.assertEqual(c.get('/health',headers={'Origin':'https://unrelated.invalid'}).status_code,403)

    def test_failure_is_not_a_normal_result(self):
        class Failing:
            def detect(self,row): raise ValueError('上游超时')
        with TemporaryDirectory() as d, TestClient(create_app(d,lambda mode:Failing())) as c:
            tid=c.post('/api/checks',json={'items':[ITEM]}).json()['task_id']
            job=self.wait(c,tid)
            self.assertEqual(job['status'],'failed');self.assertEqual(job['results'],[])
            self.assertEqual(job['failed'],1)
            self.assertEqual(c.get(f'/api/checks/{tid}/report').status_code,409)
            self.assertEqual(c.post(f'/api/checks/{tid}/evaluate',json={'labels':[{'id':'case-1','is_hallucination':False}]}).status_code,409)

    def test_real_mode_contract_without_network(self):
        seen=[]
        class Fake:
            model='offline-test'
            def detect(self,row):
                seen.append(set(row))
                return {'id':row['id'],'verdict':'no_hallucination','types':[],'severity':None,'claims':[],'reason':'测试结果'}
        with TemporaryDirectory() as d, TestClient(create_app(d,lambda mode:Fake())) as c:
            tid=c.post('/api/checks',json={'mode':'llm','items':[ITEM]}).json()['task_id']
            self.wait(c,tid)
            response=c.post(f'/api/checks/{tid}/evaluate',json={'labels':[{'id':'case-1','is_hallucination':False}]}).json()
            self.assertEqual(response['metrics']['tn'],1)
            self.assertEqual(seen,[set(ITEM)])
            self.assertEqual(c.post(f'/api/checks/{tid}/evaluate',json={'labels':[{'id':'wrong','is_hallucination':False}]}).status_code,422)

    def test_queue_limit(self):
        gate=threading.Event()
        class Blocking(MockDetector):
            def detect(self,row):gate.wait(5);return super().detect(row)
        with TemporaryDirectory() as d, TestClient(create_app(d,lambda mode:Blocking())) as c:
            try:
                for _ in range(8):self.assertEqual(c.post('/api/checks',json={'items':[ITEM]}).status_code,202)
                self.assertEqual(c.post('/api/checks',json={'items':[ITEM]}).status_code,429)
            finally:gate.set()

    def test_restart_marks_unfinished_job(self):
        from uuid import uuid4
        with TemporaryDirectory() as d:
            tid=str(uuid4());folder=Path(d)/tid;folder.mkdir()
            (folder/'task.json').write_text(json.dumps({'task_id':tid,'status':'running'}))
            with TestClient(create_app(d)) as c:
                self.assertEqual(c.app.state.store.get(tid)['status'],'interrupted')
