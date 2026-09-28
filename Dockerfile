FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pipeline.py fetch_kodik.py genres.json ./
COPY kodik_pipeline ./kodik_pipeline

# data/ — том с kodik.json; unmapped_genres.json пишется в рабочую папку
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

# Пайплайн — batch-задача, не сервис. Контейнер остаётся «живым»,
# а запуск делается через Dokploy Schedules / Terminal (см. README).
CMD ["sleep", "infinity"]
