import unittest
from app.evaluate import evaluate
from app.schema import validate_prediction
from app.providers import MockDetector

class CoreTests(unittest.TestCase):
    def test_confusion_matrix_and_abstention(self):
        verdicts = ["hallucination", "hallucination", "not_verifiable", "no_hallucination"]
        predictions = {"mode": "llm", "results": [{"id": str(i), "verdict": v} for i, v in enumerate(verdicts)]}
        truth = [{"id": str(i), "is_hallucination": v} for i, v in enumerate([True, False, True, False])]
        result = evaluate(predictions, truth)
        self.assertEqual(result["metrics"], dict(tp=1, fp=1, fn=1, tn=1, precision=.5, recall=.5, f1=.5, accuracy=.5))
        self.assertEqual(result["false_negative_ids"], ["2"])

    def test_missing_ids_rejected(self):
        with self.assertRaises(ValueError):
            evaluate({"mode": "llm", "results": []}, [{"id": "a", "is_hallucination": True}])

    def test_mock_has_no_metrics(self):
        result = evaluate({"mode": "mock", "results": [{"id": "a", "verdict": "not_verifiable"}]}, [{"id": "a", "is_hallucination": True}])
        self.assertIsNone(result["metrics"])

    def test_fabricated_evidence_rejected(self):
        row = {"id": "a", "system_reply": "原回复", "knowledge_base": "原知识"}
        result = MockDetector().detect(row)
        result["claims"] = [{"relation": "contradicted", "reply_quote": "原回复", "knowledge_quote": "虚构知识"}]
        with self.assertRaises(ValueError):
            validate_prediction(result, row)

if __name__ == "__main__":
    unittest.main()
