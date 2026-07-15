FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

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

COPY app ./app
RUN mkdir -p /app/.runtime/customer-service

EXPOSE 5001

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001", "--no-access-log"]
