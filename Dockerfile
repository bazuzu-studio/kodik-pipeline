
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    \
    # S3 / MinIO
    S3_ENDPOINT=https://s3.otakuum.ru \
    S3_REGION=us-east-1 \
    S3_BUCKET=media \
    S3_ACCESS_KEY_ID=kodik-pipeline \
    S3_SECRET_ACCESS_KEY=supersecretpassword

WORKDIR /app

# Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application
COPY pipeline.py fetch_kodik.py genres.json ./
COPY kodik_pipeline ./kodik_pipeline

# Runtime user and data directory
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

VOLUME ["/app/data"]

# Batch container.
# Pipeline запускается через Dokploy Terminal / Schedule.
CMD ["sleep", "infinity"]

