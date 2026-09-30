"""导出两个服务的 OpenAPI 规范到 docs/。

用法（项目根目录）：.venv/bin/python -B tools/export_openapi.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LOG_CONFIGURE", "0")


def main() -> int:
    from app.api import create_app as create_check_app
    from app.service import ApiSettings
    from monitor.api import create_app as create_monitor_app
    from monitor.config import Settings

    workdir = ROOT / "outputs" / "_openapi"
    workdir.mkdir(parents=True, exist_ok=True)

    check_app = create_check_app(workdir / "check", settings=ApiSettings(log_json=False))
    monitor_app = create_monitor_app(Settings(db_path=str(workdir / "monitor.db")), worker=False)

    outputs = [
        (check_app, ROOT / "docs" / "openapi.json"),
        (monitor_app, ROOT / "docs" / "openapi_monitor.json"),
    ]
    try:
        for app, path in outputs:
            spec = app.openapi()
            path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{path.relative_to(ROOT)}：{len(spec.get('paths', {}))} 个路径")
    finally:
        if getattr(monitor_app.state, "store", None) is not None:
            monitor_app.state.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())