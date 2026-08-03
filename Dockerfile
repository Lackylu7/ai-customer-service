FROM ai-customer-service/docling-models:2.112.0 AS docling_models

FROM python:3.12-slim@sha256:c3d81d25b3154142b0b42eb1e61300024426268edeb5b5a26dd7ddf64d9daf28

ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=3 update \
    && DEBIAN_FRONTEND=noninteractive apt-get \
        -o Acquire::https::Timeout=30 -o Acquire::Retries=3 \
        install --yes --no-install-recommends \
        libgl1 libglib2.0-0t64 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docling.txt ./
RUN python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.13.0+cpu" \
    && python -m pip install --no-cache-dir -r requirements-docling.txt \
    && python -m pip check
RUN python -m pip install --no-cache-dir --force-reinstall --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        "torchvision==0.28.0+cpu" \
    && python -c "import torch, torchvision; assert not torch.cuda.is_available()" \
    && python -m pip check

ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models

COPY scripts/verify_docling_models.py ./scripts/verify_docling_models.py
COPY --from=docling_models / /opt/docling-models
RUN python scripts/verify_docling_models.py

COPY app ./app
COPY config/enterprise.example.json ./config/enterprise.example.json
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/.runtime/customer-service \
    && chown -R 10001:10001 /app/.runtime

EXPOSE 5001

USER 10001:10001

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001", "--no-access-log", "--no-server-header"]
