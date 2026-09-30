"""启动入口：python -m monitor [--host 0.0.0.0] [--port 8010] [--no-worker]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .api import create_app
from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="客服幻觉监测平台")
    parser.add_argument("--host", default=None, help="监听地址，默认读 MONITOR_BIND_HOST")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认读 MONITOR_BIND_PORT")
    parser.add_argument("--no-worker", action="store_true", help="只起 API，不启动后台检测线程")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    host = args.host or settings.bind_host
    port = args.port or settings.bind_port

    if settings.auth_strict:
        logging.getLogger("monitor").warning(
            "已要求鉴权但未配置 MONITOR_API_TOKENS，接入接口将返回 503。"
        )

    app = create_app(settings, worker=not args.no_worker)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
