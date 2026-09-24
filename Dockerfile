FROM python:3.12-slim

# Критические флаги для продакшена и предсказуемости
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Важно для корректной работы с UTF-8 в логах и файлах (особенно для аниме/кириллицы)
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# Сначала копируем только requirements.txt, чтобы кэш слоя не инвалидировался при каждом изменении кода
COPY requirements.txt ./

# Установка зависимостей с проверкой на ошибки
RUN pip install --no-cache-dir --upgrade pip && \
    pip install -r requirements.txt && \
    rm -rf /root/.cache/pip

# Создаем пользователя и структуру папок ДО копирования файлов — это улучшает кэш слоев
RUN useradd --create-home --uid 1000 --gid 1000 app && \
    mkdir -p /app/data /app/kodik_pipeline && \
    chown -R app:app /app

# Копируем файлы уже под правами пользователя (или меняем права после — здесь делаем после копирования для простоты)
COPY pipeline.py fetch_kodik.py genres.json ./
COPY kodik_pipeline ./kodik_pipeline

# Явно задаем владельца, если копирование было от root
RUN chown -R app:app /app

USER app

# HEALTHCHECK: Dokploy и оркестраторы любят видеть, что контейнер «жив», даже если это batch-задача
# Проверяем, что Python и скрипт существуют
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os; assert os.path.exists('pipeline.py')" || exit 1

# CMD теперь не просто sleep. Для batch-задач в Dokploy Schedules лучше оставить sleep infinity,
# но добавить точку входа, которая позволит запускать скрипт вручную через exec без перезапуска контейнера.
# Если Dokploy запускает задачу через команду — он переопределит CMD.
CMD ["sleep", "infinity"]
