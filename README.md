# Kodik → Payload pipeline

Самостоятельный Python-пайплайн для импорта каталога из **Kodik API** в PostgreSQL/Payload CMS и постеров в MinIO/S3.

## Что нового (v2.2)

- **Новая команда `update-ongoing`** — быстро обновляет серии и статус у сериалов со статусом «выходит» (ongoing), не гоняя весь каталог. Подробнее — в разделе ниже.
- **Статус выхода сохраняется**: `ongoing` / `released` / `anons` пишется в `content.release_status` (если колонка есть; без неё пайплайн работает как раньше).
- **Защита от параллельных запусков**: `sync`/`load` и `update-ongoing` берут advisory-lock в Postgres — две записывающие задачи одновременно не работают (вторая честно пропускает запуск).
- **`load` больше не падает целиком из-за одной записи**: каждая запись в своём `SAVEPOINT`, битые попадают в отчёт, остальные сохраняются; код возврата 1, чтобы сбой был виден в Dokploy Schedules.
- **`fetch` безопаснее**: запись JSON атомарная (tmp + rename), пустой ответ API не затирает прошлый `data/kodik.json`.
- `fetch_kodik.py` стал тонкой обёрткой над `pipeline.py fetch` (раньше дублировал код); у `sync` появились все API-опции `fetch`.
- Исправлены два падавших теста; добавлены тесты на онгоинги и интеграционные тесты на реальном Postgres (см. «Тесты»).
- Убраны устаревшие упоминания старых скриптов (`merge_kodik_sources.py`, `movies.json` и т.д.) из докстрингов.

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
| Новые серии онгоингов | `python pipeline.py update-ongoing` | `0 * * * *` |

`update-ongoing` и `sync` можно ставить на пересекающееся время: если одна задача уже пишет в БД, вторая выведет «запуск пропущен» и завершится (следующий запуск подхватит изменения).

Для разовой проверки откройте **Terminal** контейнера `kodik-pipeline`:

```bash
python pipeline.py check-s3
python pipeline.py fetch --max-pages 1
python pipeline.py update-ongoing --dry-run
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
python pipeline.py update-ongoing
python pipeline.py check-s3
```

По умолчанию JSON сохраняется в `data/kodik.json`.

`sync` выполняет только `fetch → load`. Постеры остаются отдельным шагом, чтобы временный сбой S3 не мешал импорту каталога.

## Обновление онгоингов (`update-ongoing`)

```bash
python pipeline.py update-ongoing              # обычный запуск
python pipeline.py update-ongoing --dry-run    # показать изменения и откатить их
python pipeline.py update-ongoing --no-recheck # без перепроверки завершившихся
```

Что происходит:

1. Запрашиваются только сериалы со статусом `ongoing` (фильтры `anime_status` и `all_status`; результат дополнительно фильтруется на стороне пайплайна, поэтому лишнее не попадёт, даже если API проигнорирует фильтр).
2. Тайтл ищется в БД по `kodik_id`. Новые серии добавляются, изменившиеся ссылки на плеер обновляются, у тайтла обновляется `updated_at`. Существующие серии без изменений не трогаются.
3. Сезон Kodik сопоставляется с уже существующим сезоном в БД, поэтому перенумерация франшиз (S2/S3) не ломается.
4. Пишется `release_status`.
5. Тайтлы, которые в БД числятся `ongoing`, но пропали из списка Kodik (обычно сериал завершился), перепроверяются отдельным запросом по Kodik ID: фиксируется новый статус и финальные серии. Лимит — `--recheck-limit` (по умолчанию 200). Работает только при наличии колонки статуса.

Тайтлы, которых ещё нет в БД, **не создаются** (иначе нумерация сезонов франшизы была бы неверной) — команда выводит их список, они появятся при ближайшем `sync`. Если Kodik вернул ноль онгоингов, БД не трогается и команда завершается с ошибкой (похоже на сбой API).

`--max-pages` предназначен для проверок; с ним перепроверка пропавших автоматически отключается, т.к. список неполный.

### Колонка статуса

Чтобы статус сохранялся, добавьте поле в коллекцию Content в Payload, например:

```ts
{
  name: 'releaseStatus',           // колонка release_status
  type: 'select',
  options: ['ongoing', 'released', 'anons'],
}
```

Или, если поле пока не нужно в админке, создайте колонки вручную:

```sql
ALTER TABLE content ADD COLUMN IF NOT EXISTS release_status VARCHAR;
ALTER TABLE _content_v ADD COLUMN IF NOT EXISTS version_release_status VARCHAR;
```

Другое имя колонки задаётся переменной `RELEASE_STATUS_COLUMN`. Учтите: колонки, которых нет в схеме Payload, он может удалить при своих миграциях — поэтому лучше завести поле в коллекции. Без колонки `update-ongoing` всё равно добавляет серии, но не сохраняет статус и не перепроверяет завершившиеся. После добавления колонки один раз выполните `sync`, чтобы проставить статусы всему каталогу.

## Настройки Kodik

```env
KODIK_LIMIT=100
KODIK_DELAY=0.25
KODIK_TIMEOUT=60
KODIK_RETRIES=5
RELEASE_STATUS_COLUMN=release_status   # необязательно
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

Юнит-тесты покрывают нормализацию, пагинацию, статусы, выборку онгоингов, атомарную запись JSON, сезоны/эпизоды, richText, жанры, slug и рейтинг.

Интеграционные тесты (`tests/test_integration_db.py`) проверяют `load` и `update-ongoing` на реальном Postgres: перенумерованные сезоны, dry-run, сохранение статуса, перепроверку завершившихся, изоляцию битых записей и advisory-lock. Без переменной они пропускаются:

```bash
TEST_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/test_db pytest -q
```

⚠ Эти тесты **очищают** таблицы Payload (`TRUNCATE`) — используйте только отдельную тестовую БД со схемой Payload.
