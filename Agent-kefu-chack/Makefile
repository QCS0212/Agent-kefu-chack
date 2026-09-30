# 常用运维命令。Windows 下可用 PowerShell 脚本 scripts\start_all.ps1 代替。
PY ?= .venv/bin/python
PORT_API ?= 8000
PORT_MONITOR ?= 8010

.PHONY: help install test run api monitor start stop docker-build docker-up docker-down openapi fmt

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-14s %s\n", $$1, $$2}'

install: ## 创建虚拟环境并安装依赖
	python3 -m venv .venv && $(PY) -m pip install -r requirements.txt

test: ## 运行全部离线单元测试
	$(PY) -m unittest discover -s tests -v

api: ## 启动批量检测 API（本机）
	$(PY) -B -m uvicorn app.api:app --host 127.0.0.1 --port $(PORT_API) --workers 1

monitor: ## 启动监测平台（本机）
	$(PY) -B -m monitor --host 127.0.0.1 --port $(PORT_MONITOR)

start: ## 一键启动两个服务（macOS/Linux）
	bash scripts/start_all.sh

stop: ## 停止两个服务
	bash scripts/stop_all.sh

docker-build: ## 构建镜像
	docker build -t kefu-check:latest .

docker-up: ## 用 compose 启动两个服务
	docker compose up --build -d

docker-down: ## 停止 compose 服务
	docker compose down

openapi: ## 重新导出两个服务的 OpenAPI 规范
	$(PY) -B tools/export_openapi.py

fmt: ## 语法编译检查
	$(PY) -m compileall -q app monitor common tools