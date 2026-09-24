FROM python:3.12-slim

# Флаги поведения Python и pip
# LANG/LC_ALL — чтобы UTF-8 в названиях и логах не падал на codecpages
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# requirements отдельно: код меняем часто, зависимости редко — слой установки не протухает
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
 && pip install -r requirements.txt

# Сначала группа, потом пользователь: без groupadd useradd --gid 1000 падает
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app app \
 && mkdir -p /app/data \
 && chown -R app:app /app

# --chown ставит владельца на этапе COPY — отдельный chown -R слоем больше не нужен
COPY --chown=app:app pipeline.py fetch_kodik.py genres.json ./
COPY --chown=app:app kodik_pipeline ./kodik_pipeline

USER app

# Пайплайн — batch-задача, не сервис. Контейнер держим «живым»,
# запуск делается через Dokploy Schedules / Terminal (см. README).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, sys; sys.exit(0 if os.path.isfile('/app/pipeline.py') else 1)"

STOPSIGNAL SIGTERM

CMD ["sleep", "infinity"]
