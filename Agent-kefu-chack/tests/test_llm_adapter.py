import json
import unittest
from unittest.mock import patch, MagicMock
from app.llm import LLMDetector

class AdapterTests(unittest.TestCase):
    def test_request_fields_and_response_validation(self):
        row={'id':'private-id','user_question':'参数？','system_reply':'版本2','knowledge_base':'版本1','ground_truth':'禁止发送'}
        answer={'verdict':'hallucination','types':['product_error'],'severity':'medium','reason':'参数冲突',
                'claims':[{'relation':'contradicted','reply_quote':'版本2','knowledge_quote':'版本1','reason':'版本不同'}]}
        with patch('app.llm.config',return_value={'LLM_API_KEY':'test-key','LLM_BASE_URL':'https://example.invalid','LLM_MODEL':'test-model'}):
            detector=LLMDetector()
        response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(answer)}}]}).encode()
        detector.opener=MagicMock();detector.opener.open.return_value=response
        result=detector.detect(row)
        body=json.loads(detector.opener.open.call_args.args[0].data)
        sent=json.loads(body['messages'][1]['content'])
        self.assertEqual(set(sent),{'user_question','system_reply','knowledge_base'})
        self.assertEqual(result['id'],'private-id')
        self.assertEqual(detector.endpoint,'https://example.invalid/v1/chat/completions')

    def test_remote_disconnect_retries_and_reports_failure(self):
        from http.client import RemoteDisconnected
        with patch('app.llm.config',return_value={'LLM_API_KEY':'test-key','LLM_BASE_URL':'https://example.invalid','LLM_MODEL':'test-model'}):
            detector=LLMDetector()
        detector.opener=MagicMock()
        detector.opener.open.side_effect=RemoteDisconnected('closed')
        with patch('app.llm.time.sleep'), self.assertRaises(ValueError):
            detector.detect({'user_question':'问','system_reply':'答','knowledge_base':'依据'})
        self.assertEqual(detector.opener.open.call_count,2)

if __name__=='__main__': unittest.main()
