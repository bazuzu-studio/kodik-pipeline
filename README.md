# Kodik → Payload pipeline

Самостоятельный Python-пайплайн для импорта каталога из **Kodik API** в PostgreSQL/Payload CMS и постеров в MinIO/S3.

## Что нового (v2.3) — совместимость с обновлённой CMS

- **`load` больше не использует `ON CONFLICT (title_en)`.** Миграция CMS `20261003_120000` удалила уникальный индекс по `title_en` (ремейки и одноимённые тайтлы), и со старым запросом любой `load` падал бы с «no unique or exclusion constraint matching the ON CONFLICT specification». Теперь тайтл ищется по `kodik_id` (индекс есть в CMS); запасной поиск по `title_en` — только среди записей без `kodik_id` и того же `type`. `slug` и `type` существующей записи не меняются — URL стабильны.
- **`content.franchise_id`** (и `_content_v.version_franchise_id`): общий идентификатор сезонов одного сериала. Формат — голый `kinopoisk_id` (или `imdb_id`), как у записей, заполненных миграцией CMS, поэтому франшизы не разъезжаются. Пишется, только если колонка есть.
- **Сброс кэша сайта**: после успешного `load` и `update-ongoing` (с изменениями, не в `--dry-run`) пайплайн сам вызывает `POST <REVALIDATE_URL>/api/revalidate`. Нужны `REVALIDATE_URL` и `REVALIDATE_SECRET`; без них шаг пропускается, ошибки сброса пайплайн не роняют. Вручную: `python pipeline.py revalidate`.
- В `docker-compose.yml` добавлен `init: true` (остановка контейнера без 10-секундной задержки).
- Новые тесты: `test_revalidate.py`, `test_load_lookup.py` и три интеграционных теста (повторный `load` идемпотентен, `_status = 'published'` + `franchise_id`, сброс кэша только после commit).

Порядок выкладки: **CMS (миграции) → пайплайн → один `sync`**. Новые тайтлы пишутся с `_status = 'published'`, иначе публичное чтение CMS их не отдаёт.

## Что нового (v2.2)

- **Новая команда `update-ongoing`** — быстро обновляет серии и статус у сериалов со статусом «выходит» (ongoing), не гоняя весь каталог. Подробнее — в разделе ниже.
- **Статус выхода сохраняется**: `anons` / `ongoing` / `released` пишется в `content.release_status` (колонку создаёт миграция CMS; без неё пайплайн работает как раньше).
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

Кэш сайта сбрасывается автоматически после этих задач, если в Environment заданы `REVALIDATE_URL` (по умолчанию `http://movhub-web:3000` — alias сервиса web в `dokploy-network`) и `REVALIDATE_SECRET` (тот же, что у web и CMS).

`update-ongoing` и `sync` можно ставить на пересекающееся время: если одна задача уже пишет в БД, вторая выведет «запуск пропущен» и завершится (следующий запуск подхватит изменения).

Для разовой проверки откройте **Terminal** контейнера `kodik-pipeline`:

```bash
python pipeline.py check-s3
python pipeline.py fetch --max-pages 1
python pipeline.py update-ongoing --dry-run
```

`data/kodik.json` лежит в томе `kodik_data` (`/app/data`) и переживает передеплой. Схема таблиц Payload (`content`, `episodes`, `media` и т.д.) должна существовать до запуска `load`: CMS должна применить свои миграции к этой БД, включая `add_release_status`.

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
python pipeline.py revalidate
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

Статус хранится в `content.release_status` и `_content_v.version_release_status` (enum `anons` / `ongoing` / `released`, поле `releaseStatus` в коллекции Content). Колонки создаёт **миграция CMS** `20260928_193844_add_release_status` — она применяется автоматически при старте CMS, вручную ничего добавлять не нужно.

Порядок выкладки: сначала деплой CMS, затем один `sync` (проставит статусы всему каталогу), затем расписание `update-ongoing`.

- В БД пишутся только `anons`, `ongoing`, `released`; любое другое значение Kodik превращается в пустое (иначе enum отверг бы всю запись).
- Другое имя колонки — переменная `RELEASE_STATUS_COLUMN` (нужно только при собственной схеме).
- Пока миграция не применена, пайплайн не падает: серии добавляются, но статус не сохраняется и завершившиеся сериалы не перепроверяются (в логе будет примечание).

### Фон (backdrop) из скриншота

Шаг `posters` помимо постера берёт **первый скриншот** из API (`material_data.screenshots[0]`; если он невалидный — первый корректный http(s)-URL), загружает его в S3/MinIO как `<slug>-backdrop.<ext>`, создаёт запись `media` и ставит её в `content.backdrop` (и в версии `_content_v`).

- Заполняется только **пустой** `backdrop`: фон, выбранный редактором, не перезаписывается (и скриншот в этом случае даже не скачивается).
- Тайтл ищется по `kodik_id` (если есть), иначе по `title_en`.
- `load` теперь копирует `poster_id` / `backdrop_id` из `content` в пересозданные версии, так что картинки не пропадают из админки после повторной загрузки.
- Чтобы заполнить уже загруженный каталог, один раз выполните `python pipeline.py posters`.

### SEO-поля CMS

В CMS подключён SEO-плагин (миграция `20261006_120000_add_seo_meta`): у `content` есть `meta.title`, `meta.description`, `meta.image`. Пайплайн заполняет их из данных Kodik API:

| Поле | Источник |
| --- | --- |
| `meta.title` | `titleRu` + год + «— смотреть онлайн», до 60 символов (длинное название обрезается с «…») |
| `meta.description` | описание из API (HTML/BBCode и пометка «Источник» убираются), до 155 символов по границе слова; если описания нет — шаблон из названия, года и жанров |
| `meta.image` | постер (проставляет шаг `posters`) |

- `load` заполняет `title`/`description`, `posters` — картинку.
- Значения пишутся **только в пустые поля**: то, что отредактировано в админке, не затирается (но и автоматически не обновляется, если Kodik изменит описание).
- `load` пересоздаёт строки `_content_v`, поэтому `meta_*` копируются из `content` в версии — иначе админка (она показывает последнюю версию) не увидела бы значения.
- Чтобы заполнить уже загруженный каталог, один раз выполните `load` и `posters`. Без миграции CMS шаг пропускается.

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

Интеграционные тесты (`tests/test_integration_db.py`) запускаются на БД, в которой применены миграции CMS (`pnpm payload migrate` в проекте CMS), и проверяют `load` и `update-ongoing` на реальном Postgres: перенумерованные сезоны, dry-run, сохранение статуса, перепроверку завершившихся, изоляцию битых записей и advisory-lock. Без переменной они пропускаются:

```bash
TEST_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/test_db pytest -q
```

⚠ Эти тесты **очищают** таблицы Payload (`TRUNCATE`) — используйте только отдельную тестовую БД. Схему они не меняют.
