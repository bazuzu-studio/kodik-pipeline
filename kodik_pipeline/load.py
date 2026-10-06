"""
Шаг 2: загрузка data/kodik.json в таблицы Payload.

Особенности:
  - episodes.player_link — ссылка на плеер для СЕРИЙ. Пишется напрямую.
  - content.player_link — ссылка на плеер для ФИЛЬМОВ (Kodik item.link).
  - content.age_rating — возрастное ограничение (minimalAge из material_data).
  - content.release_status — статус выхода (ongoing/released/anons) из
    material_data.anime_status / all_status. Пишется, только если такая
    колонка есть (см. status.py); не путать со служебными content.status /
    _status (draft/published).
  - Каждая запись грузится в своём SAVEPOINT: битая запись пропускается и
    попадает в отчёт, остальные сохраняются. При ошибках код возврата = 1.
  - Пока идёт загрузка, удерживается advisory-lock, поэтому update-ongoing
    в это время не запустится (и наоборот).
  - Тайтл ищется по kodik_id (а не по title_en: уникальный индекс по title_en
    в CMS удалён — ремейки и одноимённые тайтлы). Без kodik_id — запасной
    поиск по title_en среди записей без kodik_id.
  - content.franchise_id — общий идентификатор сезонов одного сериала
    (kinopoisk_id или imdb_id); сайт группирует по нему сезоны. Пишется,
    только если колонка есть (миграция CMS 20261003_120000).
  - SEO-поля CMS (content.meta_*, плагин plugin-seo): пустые title/description
    заполняются из данных API, заполненные вручную не трогаются; при
    пересоздании _content_v значения копируются из content (см. seo.py).
  - После успешной загрузки сбрасывается кэш сайта (см. revalidate.py).

Запуск: python pipeline.py load --data data/kodik.json --genres genres.json
"""

from __future__ import annotations

import argparse
from typing import Any

import psycopg2.extras

from . import genres as genres_mod
from .config import database_url
from .db import table_columns, transaction, try_advisory_lock
from .json_io import load_json, save_json
from .revalidate import notify_frontend
from .seo import SeoSupport, copy_seo_to_versions, detect_seo_support, set_seo_meta
from .richtext import build_richtext
from .status import StatusSupport, detect_status_support, set_release_status

# ─── SQL: content ───────────────────────────────────────────────

INSERT_CONTENT_SQL = """
INSERT INTO content (
    type, title_en, title_ru, original_title, slug,
    description, release_year, duration, rating, age_rating,
    player_link, status, _status, shikimori_id, kodik_id, kinopoisk_id,
    created_at, updated_at
) VALUES (
    %(type)s, %(title_en)s, %(title_ru)s, %(original_title)s, %(slug)s,
    %(description)s, %(release_year)s, %(duration)s, %(rating)s, %(age_rating)s,
    %(player_link)s, 'draft', 'published', %(shikimori_id)s, %(kodik_id)s, %(kinopoisk_id)s,
    COALESCE(%(created_at)s::timestamptz, now()), COALESCE(%(updated_at)s::timestamptz, now())
)
RETURNING id;
"""

# slug и type существующей записи не меняются: URL тайтла должен быть стабильным.
UPDATE_CONTENT_SQL = """
UPDATE content SET
    title_en = %(title_en)s,
    title_ru = %(title_ru)s,
    original_title = %(original_title)s,
    description = %(description)s,
    release_year = %(release_year)s,
    duration = %(duration)s,
    rating = %(rating)s,
    age_rating = %(age_rating)s,
    player_link = %(player_link)s,
    shikimori_id = %(shikimori_id)s,
    kodik_id = %(kodik_id)s,
    kinopoisk_id = %(kinopoisk_id)s,
    created_at = COALESCE(%(created_at)s::timestamptz, created_at),
    updated_at = COALESCE(%(updated_at)s::timestamptz, updated_at),
    _status = 'published'
WHERE id = %(id)s;
"""

# ─── SQL: content_rels (жанры) ──────────────────────────────────

DELETE_OLD_RELS_SQL = "DELETE FROM content_rels WHERE parent_id = %(id)s AND path = 'genres';"

INSERT_REL_SQL = """
INSERT INTO content_rels (parent_id, path, genres_id, "order")
VALUES (%(parent_id)s, 'genres', %(genres_id)s, %(order)s);
"""

# ─── SQL: _content_v (версии Payload) ───────────────────────────

DELETE_OLD_VERSION_RELS_SQL = """
DELETE FROM _content_v_rels WHERE parent_id IN (
    SELECT id FROM _content_v WHERE parent_id = %(id)s
);
"""

DELETE_OLD_VERSIONS_SQL = "DELETE FROM _content_v WHERE parent_id = %(id)s;"

_VERSION_COLUMNS = """
    parent_id, version_type, version_title_en, version_title_ru,
    version_original_title, version_slug, version_description,
    version_release_year, version_duration, version_rating, version_age_rating,
    version_player_link,
    version_status, version__status, version_shikimori_id, version_kodik_id,
    version_updated_at, version_created_at, latest
"""

INSERT_VERSION_PUBLISHED_SQL = f"""
INSERT INTO _content_v ({_VERSION_COLUMNS}) VALUES (
    %(parent_id)s, %(type)s, %(title_en)s, %(title_ru)s,
    %(original_title)s, %(slug)s, %(description)s,
    %(release_year)s, %(duration)s, %(rating)s, %(age_rating)s,
    %(player_link)s,
    'draft', 'published', %(shikimori_id)s, %(kodik_id)s,
    COALESCE(%(updated_at)s::timestamptz, now()),
    COALESCE(%(created_at)s::timestamptz, now()),
    true
)
RETURNING id;
"""

INSERT_VERSION_DRAFT_SQL = f"""
INSERT INTO _content_v ({_VERSION_COLUMNS}) VALUES (
    %(parent_id)s, %(type)s, %(title_en)s, %(title_ru)s,
    %(original_title)s, %(slug)s, %(description)s,
    %(release_year)s, %(duration)s, %(rating)s, %(age_rating)s,
    %(player_link)s,
    'draft', 'draft', %(shikimori_id)s, %(kodik_id)s,
    COALESCE(%(updated_at)s::timestamptz, now()),
    COALESCE(%(created_at)s::timestamptz, now()),
    false
)
RETURNING id;
"""

INSERT_VERSION_REL_SQL = """
INSERT INTO _content_v_rels (parent_id, path, genres_id, "order")
VALUES (%(parent_id)s, 'version.genres', %(genres_id)s, %(order)s);
"""

# ─── SQL: seasons ───────────────────────────────────────────────

FIND_SEASON_SQL = """
SELECT id FROM seasons WHERE content_id = %s AND season_number = %s
"""

INSERT_SEASON_SQL = """
INSERT INTO seasons (content_id, season_number, title, release_year)
VALUES (%s, %s, %s, %s)
RETURNING id
"""

UPDATE_SEASON_SQL = """
UPDATE seasons
SET title = COALESCE(%s, title),
    release_year = COALESCE(%s, release_year),
    updated_at = now()
WHERE id = %s
"""

# ─── SQL: episodes ──────────────────────────────────────────────

FIND_EPISODE_SQL = """
SELECT id FROM episodes WHERE season_id = %(season_id)s AND episode_number = %(episode_number)s
"""


# ─── Предварительная проверка схемы ──────────────────────────────

def require_content_columns(cur) -> None:
    """Проверяет наличие нужных колонок перед стартом."""
    cols = table_columns(cur, "content")
    if "kinopoisk_id" not in cols:
        raise SystemExit(
            "Ошибка: в таблице content нет колонки kinopoisk_id.\n"
            "Выполните перед запуском:\n"
            "  ALTER TABLE content ADD COLUMN IF NOT EXISTS kinopoisk_id VARCHAR;\n"
        )

    if "age_rating" not in cols:
        raise SystemExit(
            "Ошибка: в таблице content нет колонки age_rating.\n"
            "Выполните перед запуском:\n"
            "  ALTER TABLE content ADD COLUMN IF NOT EXISTS age_rating INTEGER;\n"
            "  ALTER TABLE _content_v ADD COLUMN IF NOT EXISTS version_age_rating INTEGER;\n"
        )

    if "player_link" not in cols:
        raise SystemExit(
            "Ошибка: в таблице content нет колонки player_link.\n"
            "Выполните перед запуском:\n"
            "  ALTER TABLE content ADD COLUMN IF NOT EXISTS player_link TEXT;\n"
            "  ALTER TABLE _content_v ADD COLUMN IF NOT EXISTS version_player_link TEXT;\n"
        )

    missing_time_cols = {"created_at", "updated_at"} - cols
    if missing_time_cols:
        if "created_at" in missing_time_cols:
            cur.execute("ALTER TABLE content ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ")
        if "updated_at" in missing_time_cols:
            cur.execute("ALTER TABLE content ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
        print("Добавлены отсутствующие колонки content.created_at / content.updated_at")

    ep_cols = table_columns(cur, "episodes")
    if "player_link" not in ep_cols:
        raise SystemExit(
            "Ошибка: в таблице episodes нет колонки player_link.\n"
            "Выполните перед запуском:\n"
            "  ALTER TABLE episodes ADD COLUMN IF NOT EXISTS player_link TEXT;\n"
        )


# ─── Функции: версии и реляции ───────────────────────────────────

def insert_version_rels(cur, version_id: int, genre_ids: list[int]) -> None:
    for order, genre_id in enumerate(genre_ids):
        cur.execute(
            INSERT_VERSION_REL_SQL,
            {"parent_id": version_id, "genres_id": genre_id, "order": order},
        )


# ─── Функции: сезоны и эпизоды ───────────────────────────────────

def insert_episode(cur, season_id: int, ep_number: int, kodik_link: str | None) -> None:
    """Создаёт новую серию (без проверки на существование)."""
    insert_fields: dict[str, Any] = {
        "season_id": season_id,
        "episode_number": ep_number,
        "title": f"Серия {ep_number}",
    }
    if kodik_link:
        insert_fields["player_link"] = kodik_link

    col_names = ", ".join(insert_fields)
    placeholders = ", ".join(f"%({c})s" for c in insert_fields)
    cur.execute(f"INSERT INTO episodes ({col_names}) VALUES ({placeholders})", insert_fields)


def upsert_episode(cur, season_id: int, ep_number: int, kodik_link: str | None) -> bool:
    """Создаёт эпизод или обновляет ссылку у уже существующего.
    Возвращает True, если эпизод был СОЗДАН (для счётчика).

    Ссылка всегда пишется в player_link. Description не трогается
    (там может быть реальное описание эпизода).
    """
    cur.execute(FIND_EPISODE_SQL, {"season_id": season_id, "episode_number": ep_number})
    row = cur.fetchone()

    if row:
        if kodik_link:
            cur.execute(
                "UPDATE episodes SET player_link = %(player_link)s, updated_at = now() WHERE id = %(id)s",
                {"player_link": kodik_link, "id": row[0]},
            )
        return False

    insert_episode(cur, season_id, ep_number, kodik_link)
    return True


def load_seasons_and_episodes(
    cur,
    content_id: int,
    seasons: list[dict[str, Any]],
) -> tuple[int, int]:
    """Создаёт/обновляет сезоны и эпизоды для content_id.
    Возвращает (новых_сезонов, новых_эпизодов).
    """
    count_seasons = 0
    count_episodes = 0

    for season in seasons:
        season_number = season.get("seasonNumber")
        if season_number is None:
            continue

        cur.execute(FIND_SEASON_SQL, (content_id, season_number))
        row = cur.fetchone()

        if row:
            season_id = row[0]
            cur.execute(
                UPDATE_SEASON_SQL,
                (season.get("title"), season.get("releaseYear"), season_id),
            )
        else:
            cur.execute(
                INSERT_SEASON_SQL,
                (content_id, season_number, season.get("title"), season.get("releaseYear")),
            )
            season_id = cur.fetchone()[0]
            count_seasons += 1

        for ep in season.get("episodes", []):
            ep_number = ep.get("number")
            if ep_number is None:
                continue
            created = upsert_episode(cur, season_id, ep_number, ep.get("playerLink"))
            count_episodes += created

    return count_seasons, count_episodes


# ─── Защита от конфликтов slug ───────────────────────────────────

def find_existing_content(cur, rec: dict[str, Any]) -> tuple[int, str | None] | None:
    """(id, slug) уже загруженной записи или None.

    Основной ключ — kodik_id (в CMS он проиндексирован). title_en больше НЕ
    уникален, поэтому запасной поиск по нему ограничен записями без kodik_id
    (загруженными старыми версиями пайплайна) и тем же type — иначе можно
    «усыновить» чужой одноимённый тайтл.
    """
    kodik_id = rec.get("kodikId")
    if kodik_id not in (None, ""):
        cur.execute(
            "SELECT id, slug FROM content WHERE kodik_id = %s ORDER BY id LIMIT 1",
            (str(kodik_id),),
        )
        row = cur.fetchone()
        if row:
            return row[0], row[1]

    cur.execute(
        "SELECT id, slug FROM content WHERE title_en = %s AND type = %s "
        "AND (kodik_id IS NULL OR kodik_id = '') ORDER BY id LIMIT 1",
        (rec["titleEn"], rec["type"]),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def resolve_content_slug(cur, rec: dict[str, Any], existing_slug: str | None = None) -> str:
    """Возвращает безопасный slug для content.

    Если запись уже есть — сохраняем её текущий slug (URL не меняется).
    Если slug занят другой записью — добавляем kodikId (или shikimoriId).
    Это предотвращает UniqueViolation по content_slug_idx.
    """
    if existing_slug:
        return existing_slug

    requested_slug = (rec.get("slug") or "").strip()

    if not requested_slug:
        requested_slug = "content"

    # Проверяем, свободен ли исходный slug.
    cur.execute(
        "SELECT id FROM content WHERE slug = %s LIMIT 1",
        (requested_slug,),
    )
    if cur.fetchone() is None:
        return requested_slug

    # Slug занят другой записью. Делаем стабильный суффикс.
    suffix_source = (
        rec.get("kodikId")
        or rec.get("shikimoriId")
        or rec.get("kinopoiskId")
        or "kodik"
    )
    suffix = str(suffix_source).strip().lower()
    safe_suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in suffix)
    safe_suffix = safe_suffix.strip("-") or "kodik"

    candidate = f"{requested_slug}-{safe_suffix}"

    # На случай крайне редкого повторения суффикса.
    cur.execute(
        "SELECT id FROM content WHERE slug = %s LIMIT 1",
        (candidate,),
    )
    if cur.fetchone() is None:
        return candidate

    n = 2
    while True:
        candidate_n = f"{candidate}-{n}"
        cur.execute(
            "SELECT id FROM content WHERE slug = %s LIMIT 1",
            (candidate_n,),
        )
        if cur.fetchone() is None:
            return candidate_n
        n += 1


# ─── franchise_id ────────────────────────────────────────────────

def detect_franchise_support(cur) -> tuple[bool, bool]:
    """(есть content.franchise_id, есть _content_v.version_franchise_id).
    Колонки создаёт миграция CMS 20261003_120000; без неё пайплайн работает как раньше."""
    return (
        "franchise_id" in table_columns(cur, "content"),
        "version_franchise_id" in table_columns(cur, "_content_v"),
    )


def set_franchise_id(cur, content_id: int, franchise_id: str | None, support: tuple[bool, bool]) -> None:
    """Пишет franchise_id в content и его версии. Пустое значение существующее не затирает."""
    if not franchise_id:
        return
    has_content, has_version = support
    if has_content:
        cur.execute("UPDATE content SET franchise_id = %s WHERE id = %s", (str(franchise_id), content_id))
    if has_version:
        cur.execute(
            "UPDATE _content_v SET version_franchise_id = %s WHERE parent_id = %s",
            (str(franchise_id), content_id),
        )


# ─── Основная функция загрузки записи ────────────────────────────

def load_record(
    cur,
    rec: dict[str, Any],
    genre_index: dict[str, int],
    unmapped_genres: list[str],
    status_support: StatusSupport | None = None,
    franchise_support: tuple[bool, bool] = (False, False),
    seo_support: SeoSupport | None = None,
) -> tuple[int, int]:
    """Загружает одну запись из data/kodik.json.

    Поля content, приходящие из Kodik material_data (см. api.normalize_item):
      - rating       ← material_data.shikimori_rating
      - age_rating  ← material_data.age_rating
    status/_status — служебные поля Payload ('draft'/'published'),
    выставляются константой. Статус выхода (ongoing/released) пишется
    отдельно в release_status, если колонка существует.

    Возвращает (новых_сезонов, новых_эпизодов) — 0, 0 для фильмов.
    """
    richtext = build_richtext(rec.get("description"))

    existing = find_existing_content(cur, rec)

    params = {
        "type": rec["type"],
        "title_en": rec["titleEn"],
        "title_ru": rec.get("titleRu") or rec["titleEn"],
        "original_title": rec.get("originalTitle"),
        "slug": resolve_content_slug(cur, rec, existing[1] if existing else None),
        "description": psycopg2.extras.Json(richtext),
        "release_year": rec.get("releaseYear"),
        "duration": rec.get("duration"),
        "rating": rec.get("rating") if rec.get("rating") is not None else 0,
        # minimalAge приходит из Kodik material_data.age_rating (см. api.py).
        "age_rating": rec.get("minimalAge"),
        # playerLink — только для фильмов, берётся из Kodik item.link
        # (см. api.normalize_item); для сериалов остаётся None, т.к. там
        # ссылка на плеер хранится по эпизодам в таблице episodes.
        "player_link": rec.get("playerLink"),
        "shikimori_id": rec.get("shikimoriId"),
        "kodik_id": rec.get("kodikId"),
        "kinopoisk_id": rec.get("kinopoiskId"),
        "created_at": rec.get("createdAt"),
        "updated_at": rec.get("updatedAt"),
    }

    if existing:
        content_id = existing[0]
        cur.execute(UPDATE_CONTENT_SQL, {**params, "id": content_id})
    else:
        cur.execute(INSERT_CONTENT_SQL, params)
        content_id = cur.fetchone()[0]

    # Очистка старых реляций и версий
    cur.execute(DELETE_OLD_RELS_SQL, {"id": content_id})
    cur.execute(DELETE_OLD_VERSION_RELS_SQL, {"id": content_id})
    cur.execute(DELETE_OLD_VERSIONS_SQL, {"id": content_id})

    # Жанры
    genre_ids: list[int] = []
    for genre_title in rec.get("genres", []):
        genre_ids.append(
            genres_mod.get_or_create_genre(cur, genre_index, genre_title, unmapped_genres)
        )

    # Версии Payload (published + draft)
    version_params = {**params, "parent_id": content_id}
    cur.execute(INSERT_VERSION_PUBLISHED_SQL, version_params)
    published_version_id = cur.fetchone()[0]

    cur.execute(INSERT_VERSION_DRAFT_SQL, version_params)
    draft_version_id = cur.fetchone()[0]

    # Реляции жанров: content_rels + _content_v_rels
    for order, genre_id in enumerate(genre_ids):
        cur.execute(
            INSERT_REL_SQL,
            {"parent_id": content_id, "genres_id": genre_id, "order": order},
        )

    insert_version_rels(cur, published_version_id, genre_ids)
    insert_version_rels(cur, draft_version_id, genre_ids)

    # Статус выхода и франшиза — после пересоздания версий, чтобы попали и в них.
    if status_support is not None:
        set_release_status(cur, content_id, rec.get("status"), status_support)
    set_franchise_id(cur, content_id, rec.get("franchiseId"), franchise_support)
    # SEO: пустые meta_title/meta_description заполняем из данных API, затем
    # переносим meta_* в пересозданные версии (заполненное руками не теряется).
    if seo_support is not None:
        set_seo_meta(cur, content_id, rec, seo_support)
        copy_seo_to_versions(cur, content_id, seo_support)

    # Сезоны и эпизоды (только для series)
    seasons_count = 0
    episodes_count = 0
    if rec["type"] == "series" and rec.get("seasons"):
        seasons_count, episodes_count = load_seasons_and_episodes(
            cur, content_id, rec["seasons"]
        )

    return seasons_count, episodes_count


# ─── CLI ─────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Загружает данные Kodik API в таблицы content/_content_v/seasons/episodes"
    )
    parser.add_argument("merged", help="JSON, полученный из Kodik API")
    parser.add_argument("genres", nargs="?", default=None, help="опциональный genres.json; без него жанры берутся из API")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    records = load_json(args.merged)
    if not isinstance(records, list):
        raise SystemExit(f"Ошибка: {args.merged} должен содержать JSON-массив записей")

    inserted = 0
    skipped_no_type = 0
    total_seasons = 0
    total_episodes = 0
    unmapped_genres: list[str] = []
    failed: list[str] = []

    with transaction(database_url()) as conn:
        with conn.cursor() as cur:
            if not try_advisory_lock(cur):
                raise SystemExit("Другая задача пайплайна уже пишет в БД — запуск пропущен.")

            require_content_columns(cur)
            status_support = detect_status_support(cur)
            franchise_support = detect_franchise_support(cur)
            seo_support = detect_seo_support(cur)
            if not franchise_support[0]:
                print(
                    "Примечание: колонки content.franchise_id нет (миграция CMS "
                    "20261003_120000 не применена) — франшизы не сохраняются."
                )
            if not status_support.content:
                print(
                    f"Примечание: колонки content.{status_support.column} нет — "
                    "статус выхода (ongoing/released) не сохраняется."
                )

            if args.genres:
                genre_index = genres_mod.sync_genres_table(cur, args.genres)
            else:
                genre_index = genres_mod.sync_genres_from_records(cur, records)

            for idx, rec in enumerate(records, start=1):
                if not rec.get("type"):
                    skipped_no_type += 1
                    continue

                # SAVEPOINT на запись: одна битая запись не откатывает весь импорт.
                index_snapshot = dict(genre_index)
                unmapped_len = len(unmapped_genres)
                cur.execute("SAVEPOINT rec")
                try:
                    s_count, ep_count = load_record(
                        cur, rec, genre_index, unmapped_genres, status_support, franchise_support,
                        seo_support,
                    )
                except Exception as exc:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT rec")
                    genre_index.clear()
                    genre_index.update(index_snapshot)
                    del unmapped_genres[unmapped_len:]
                    label = rec.get("titleEn") or rec.get("kodikId") or f"#{idx}"
                    failed.append(f"{label}: {exc}")
                    print(f"  [ERR] {label}: {exc}")
                    continue
                cur.execute("RELEASE SAVEPOINT rec")

                inserted += 1
                total_seasons += s_count
                total_episodes += ep_count

                if idx % 100 == 0:
                    print(f"  ...обработано {idx} записей")

    # Транзакция закоммичена — теперь можно сбросить кэш сайта.
    if inserted:
        notify_frontend()

    print("\n--- Сводка ---")
    print(f"Вставлено/обновлено записей content: {inserted}")
    print(f"Пропущено (не определён type): {skipped_no_type}")
    print(f"Ошибок: {len(failed)}")
    print(f"Сезонов создано: {total_seasons}")
    print(f"Эпизодов создано: {total_episodes}")

    if unmapped_genres:
        unique = sorted(set(unmapped_genres))
        print(f"\n⚠ ВНИМАНИЕ: жанры из {args.merged} отсутствуют в genres.json ({len(unique)} шт.):")
        print("Созданы как fallback. Проверьте genres.json — возможно, список нужно дополнить:")
        for g in unique:
            print(f"  ! {g}")
        save_json("unmapped_genres.json", unique)
        print("Список сохранён в unmapped_genres.json")
    else:
        print(f"Все жанры из {args.merged} найдены в справочнике — расхождений нет.")

    if failed:
        raise SystemExit(f"Импорт завершён с ошибками ({len(failed)} записей пропущено).")


if __name__ == "__main__":
    main()
