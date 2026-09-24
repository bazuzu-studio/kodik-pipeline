# Kodik → Payload pipeline

Самостоятельный Python-пайплайн для импорта каталога из **Kodik API** в PostgreSQL/Payload CMS и постеров в MinIO/S3.

## Что обновлено

- убраны секреты из `.env.example`; рабочий `.env` не входит в архив;
- добавлены `Dockerfile`, `docker-compose.yml` и раздел «Деплой в Dokploy»; Postgres подключается как отдельный сервис проекта;
- `fetch` использует пагинацию Kodik через `next`/`next_page`, без `page=N`;
- добавлена защита от зацикленной пагинации и обработка `Retry-After` для 429;
- настройки API (`limit`, `delay`, `timeout`, `retries`) можно задавать через `.env`;
- жанры сопоставляются без учёта регистра;
- исправлен счётчик новых media-записей при загрузке постеров;
- скачивание постеров ограничено по размеру файла;
- добавлены проверки и тесты для новых сценариев;
- `data/*.json` и служебные Python-файлы исключены из git.

## Быстрый старт

```bash
cp .env.example .env
# заполнить .env
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Минимальные переменные:

```env
DATABASE_URL=postgres://user:password@127.0.0.1:5432/movhub
S3_BUCKET=media
S3_ENDPOINT=http://localhost:9000
S3_REGION=us-east-1
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_PUBLIC_URL=http://localhost:9000/media
KODIK_TOKEN=YOUR_KODIK_TOKEN_HERE
KODIK_TRANSLATION_ID=609
```

## Деплой в Dokploy

Пайплайн — batch-задача, поэтому в Dokploy он запускается как **Compose**-сервис с «спящим» контейнером, а сами команды выполняются по расписанию (Schedules) или вручную через Terminal. Postgres в `docker-compose.yml` не входит: он должен быть отдельным сервисом **Database → PostgreSQL** в том же проекте Dokploy.

1. В проекте создайте **Database → PostgreSQL** (если ещё нет) и скопируйте его **Internal Host**.
2. Создайте сервис **Compose** (тип Docker Compose), источник — git-репозиторий с этим кодом, Compose Path: `./docker-compose.yml`.
3. Во вкладке **Environment** задайте переменные из `.env.example`. Главное:
   `DATABASE_URL=postgres://USER:PASSWORD@<internal-host>:5432/<db>`
   Хост — Internal Host базы, а не `localhost` и не внешний адрес.
4. Compose подключён к внешней сети `dokploy-network`, поэтому контейнер видит Postgres (и MinIO, если он тоже в Dokploy) по внутренним именам. Если включён **Isolated Deployments**, добавьте эту сеть в сервисы Postgres/MinIO или используйте внешние адреса.
5. Нажмите **Deploy**.
6. Во вкладке **Schedules** создайте задачи (тип Compose, Service Name `kodik-pipeline`):

| Задача | Команда | Пример cron |
| --- | --- | --- |
| Импорт каталога | `python pipeline.py sync --genres genres.json` | `0 3 * * *` |
| Постеры | `python pipeline.py posters` | `30 3 * * *` |

Для разовой проверки откройте **Terminal** контейнера `kodik-pipeline`:

```bash
python pipeline.py check-s3
python pipeline.py fetch --max-pages 1
```

`data/kodik.json` лежит в томе `kodik_data` (`/app/data`) и переживает передеплой. Схема таблиц Payload (`content`, `episodes`, `media` и т.д.) должна существовать до запуска `load`, то есть Payload-приложение должно хотя бы раз применить миграции к этой БД.

### MinIO (отдельный сервис)

MinIO разворачивается из шаблона Dokploy (`pgsty/minio`, форк с рабочей консолью) в этом же проекте:

- порт `9000` — S3 API (домен API), порт `9001` — консоль (основной домен);
- `MINIO_BROWSER_REDIRECT_URL` — публичный адрес консоли;
- `S3_ENDPOINT=http://minio:9000` — внутренний адрес, работает, если оба Compose в сети `dokploy-network` (Dokploy подключает её сервисам с доменами). При Isolated Deployments имя может отличаться — смотрите Internal Host / имя сервиса;
- бакет (`media`) создаётся вручную в консоли; пайплайн его не создаёт;
- чтобы постеры открывались по `S3_PUBLIC_URL`, у бакета нужна анонимная политика чтения (Anonymous → `readonly`/download для префикса `*`);
- `check-s3` выведет бакеты, видимые с текущим ключом.

## Команды

```bash
python pipeline.py fetch
python pipeline.py fetch --max-pages 1
python pipeline.py load
python pipeline.py posters
python pipeline.py sync
python pipeline.py check-s3
```

По умолчанию JSON сохраняется в `data/kodik.json`.

`sync` выполняет только `fetch → load`. Постеры остаются отдельным шагом, чтобы временный сбой S3 не мешал импорту каталога.

## Настройки Kodik

```env
KODIK_LIMIT=100
KODIK_DELAY=0.25
KODIK_TIMEOUT=60
KODIK_RETRIES=5
```

Пайплайн следует URL из поля `next`/`next_page`. Параметр `page` вручную не используется.

Повторяются временные ошибки 429/500/502/503/504 и сетевые ошибки. Для 429 учитывается `Retry-After`, если сервер его вернул. При зацикленной ссылке `next` выполнение прекращается с ошибкой.

## Данные

Источник: `GET https://kodik-api.com/list` с `with_episodes=true`, `with_material_data=true`, `translation_id` и `limit`.

Результат нормализации содержит фильмы и сериалы в едином формате. Для сериалов сохраняются сезоны и эпизоды с `playerLink`. Записи сериалов с одинаковым `kinopoisk_id` (или, при его отсутствии, одинаковым `imdb_id`) группируются в франшизу.

`genres.json` — канонический справочник. Если он передан в `load`, таблица жанров синхронизируется с ним; неизвестные жанры создаются как fallback и записываются в `unmapped_genres.json`.

## Постеры

```bash
python pipeline.py posters --data data/kodik.json
```

Дополнительные ограничения:

```bash
python pipeline.py posters --data data/kodik.json --timeout 20 --retries 3 --max-bytes 10485760
```

Перед загрузкой проверяется `Content-Type`, размер ответа ограничивается `--max-bytes`, затем изображение сохраняется в S3/MinIO и привязывается к `content`/последней версии Payload.

## Безопасность

**Никогда не коммитьте рабочие `KODIK_TOKEN`, `DATABASE_URL`, S3 secret/access keys.** Если настоящий токен когда-либо попал в git, архив или публичный канал, его следует перевыпустить в кабинете Kodik.

`.env.example` содержит только плейсхолдер `YOUR_KODIK_TOKEN_HERE`.

## Тесты

```bash
pytest -q
```

Тесты покрывают нормализацию, сезоны/эпизоды, richText, жанры, slug и числовой рейтинг.
