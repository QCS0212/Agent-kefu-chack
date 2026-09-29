"""Generate a self-contained, offline ECharts report."""
import json
from pathlib import Path
from app.schema import CATEGORIES


def write_report(path, predictions, evaluation, source_rows=None):
    root = Path(__file__).resolve().parent
    payload = {
        "predictions": predictions,
        "evaluation": evaluation,
        "sources": {row["id"]: row for row in (source_rows or [])},
        "categories": CATEGORIES,
    }
    # Escape script delimiters even when the input contains hostile HTML.
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = (root / "templates/report.html").read_text(encoding="utf-8")
    library = (root / "vendor/echarts.min.js").read_text(encoding="utf-8")
    content = template.replace("/*__ECHARTS__*/", library).replace("__REPORT_DATA__", data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
