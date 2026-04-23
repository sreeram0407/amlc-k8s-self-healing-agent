FROM python:3.12-slim

# Don't buffer stdout — we want logs to flush immediately in CronJob pods
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first for better layer caching
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY poller.py ./

# Non-root user (we don't need root in the pod)
RUN useradd -u 10001 -m healer && chown -R healer:healer /app
USER healer

ENTRYPOINT ["python", "/app/poller.py"]
