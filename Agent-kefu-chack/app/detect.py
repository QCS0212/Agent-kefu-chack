from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from app.providers import get_detector
from app.schema import load_rows, validate_prediction

def detect(input_path, mode):
    rows = load_rows(input_path, ("user_question", "system_reply", "knowledge_base"))
    detector = get_detector(mode)
    results = [validate_prediction(detector.detect(row), row) for row in rows]
    return {
        "schema_version": 1,
        "mode": mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_sha256": sha256(Path(input_path).read_bytes()).hexdigest(),
        "results": results,
    }
