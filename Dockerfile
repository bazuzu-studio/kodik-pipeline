FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application files
COPY pipeline.py fetch_kodik.py genres.json ./
COPY kodik_pipeline ./kodik_pipeline

# Non-root user + persistent data directory
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

# data/ предназначен для volume с kodik.json.
# unmapped_genres.json и другие runtime-файлы также могут
# сохраняться в /app/data.
VOLUME ["/app/data"]

# Batch-контейнер: не запускаем pipeline автоматически.
# Запуск — через Dokploy Terminal / Schedule.
CMD ["sleep", "infinity"]
