# 企业部署镜像：非 root 运行、内置健康检查、离线可复现依赖
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    PYTHONIOENCODING=utf-8

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 运行（数据目录需要可写，提前创建并授权）
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/outputs /app/data /app/.run \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
port=os.environ.get('HEALTHCHECK_PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status==200 else 1)"

# 默认启动批量检测服务；监测平台用同一镜像的另一个命令（见 docker-compose.yml）
CMD ["python", "-B", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]