import unittest
from app.schema import validate_prediction
from app.evaluate import evaluate

class RealContractTests(unittest.TestCase):
    def prediction(self):
        return {'id':'x','verdict':'hallucination','types':['product_error'],'severity':'medium','reason':'规格不一致',
                'claims':[{'relation':'contradicted','reply_quote':'版本2','knowledge_quote':'版本1'}]}
    def test_conflict_requires_evidence(self):
        p=self.prediction();p['claims'][0]['knowledge_quote']=''
        with self.assertRaises(ValueError): validate_prediction(p,{'id':'x','system_reply':'版本2','knowledge_base':'版本1'})
    def test_normal_cannot_contain_conflict(self):
        p=self.prediction();p.update(verdict='no_hallucination',types=[],severity=None)
        with self.assertRaises(ValueError): validate_prediction(p,{'id':'x','system_reply':'版本2','knowledge_base':'版本1'})
    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError): evaluate({'mode':'typo','results':[]},[])
    def test_technical_failures_not_scored(self):
        with self.assertRaises(ValueError): evaluate({'mode':'llm','results':[],'errors':[{'id':'x'}]},[])
