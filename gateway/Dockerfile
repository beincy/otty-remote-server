FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    OTTY_GATEWAY_HOST=0.0.0.0 \
    OTTY_GATEWAY_PORT=8080

WORKDIR /app

# 先安装依赖以利用 Docker 层缓存（使用阿里云镜像源）
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    "fastapi>=0.115,<1" \
    "httpx>=0.28,<1" \
    "uvicorn>=0.32,<1" \
    "websockets>=14,<17"

# 复制 Gateway 与 PWA 静态资源
COPY gateway/ ./gateway/
COPY client/ ./client/

EXPOSE 8080

CMD ["python", "-m", "gateway"]
