"""
Шаг 2: загрузка data/kodik.json в таблицы Payload.

Особенности:
  - episodes.player_link — ссылка на плеер для СЕРИЙ (колонка player_link
    в таблице episodes, поле playerLink в Payload у Episode). Пишется
    всегда напрямую, без fallback.
  - content.player_link — ссылка на плеер для ФИЛЬМОВ (колонка player_link
    в таблице content, поле playerLink у Content с условием
    data?.type === 'movie'). Источник: Kodik item.link (см.
    api.normalize_item, только для type === "movie").
  - content.age_rating — возрастное ограничение (поле minimalAge в Payload).
    Источник: Kodik API → material_data.age_rating (см. api.normalize_item).
  - content.status / _status — служебные поля статуса записи в Payload
    (черновик/опубликовано), не путать с анимешным статусом (онгоинг/вышел),
    который Kodik отдаёт в material_data.anime_status. Анимешный статус
    сейчас не сохраняется — есть только в normalize_item как rec["status"].
  - episodesTotal/episodesAired (rec) — тоже берутся из material_data,
    но пока не пишутся ни в одну таблицу; при необходимости добавляются
    по той же схеме, что и age_rating ниже.

Запуск: python pipeline.py load data/kodik.json genres.json
"""

from __future__ import annotations

import argparse
from typing import Any

import psycopg2.extras

from . import genres as genres_mod
from .config import database_url
from .db import table_columns, transaction
from .json_io import load_json, save_json
from .richtext import build_richtext

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
ON CONFLICT (title_en) DO UPDATE SET
    title_ru = EXCLUDED.title_ru,
    original_title = EXCLUDED.original_title,
    slug = EXCLUDED.slug,
    description = EXCLUDED.description,
    release_year = EXCLUDED.release_year,
    duration = EXCLUDED.duration,
    rating = EXCLUDED.rating,
    age_rating = EXCLUDED.age_rating,
    player_link = EXCLUDED.player_link,
    shikimori_id = EXCLUDED.shikimori_id,
    kodik_id = EXCLUDED.kodik_id,
    kinopoisk_id = EXCLUDED.kinopoisk_id,
    created_at = COALESCE(EXCLUDED.created_at, content.created_at),
    updated_at = COALESCE(EXCLUDED.updated_at, content.updated_at),
    _status = 'published'
RETURNING id;
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

def resolve_content_slug(cur, rec: dict[str, Any]) -> str:
    """Возвращает безопасный slug для content.

    Если запись с таким title_en уже существует — сохраняем её текущий slug.
    Если slug занят другой записью — добавляем kodikId (или shikimoriId).
    Это предотвращает UniqueViolation по content_slug_idx.
    """
    title_en = rec["titleEn"]
    requested_slug = (rec.get("slug") or "").strip()

    # При повторной загрузке существующей записи не меняем её slug.
    cur.execute(
        "SELECT slug FROM content WHERE title_en = %s LIMIT 1",
        (title_en,),
    )
    existing = cur.fetchone()
    if existing and existing[0]:
        return existing[0]

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


# ─── Основная функция загрузки записи ────────────────────────────

def load_record(
    cur,
    rec: dict[str, Any],
    genre_index: dict[str, int],
    unmapped_genres: list[str],
) -> tuple[int, int]:
    """Загружает одну запись из data/kodik.json.

    Поля content, приходящие из Kodik material_data (см. api.normalize_item):
      - rating       ← material_data.shikimori_rating
      - age_rating  ← material_data.age_rating
    status/_status здесь не связаны с material_data — это служебные поля
    Payload ('draft'/'published'), выставляются константой при каждой загрузке.

    Возвращает (новых_сезонов, новых_эпизодов) — 0, 0 для фильмов.
    """
    richtext = build_richtext(rec.get("description"))

    params = {
        "type": rec["type"],
        "title_en": rec["titleEn"],
        "title_ru": rec.get("titleRu") or rec["titleEn"],
        "original_title": rec.get("originalTitle"),
        "slug": resolve_content_slug(cur, rec),
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

    inserted = 0
    skipped_no_type = 0
    total_seasons = 0
    total_episodes = 0
    unmapped_genres: list[str] = []

    with transaction(database_url()) as conn:
        with conn.cursor() as cur:
            require_content_columns(cur)

            if args.genres:
                genre_index = genres_mod.sync_genres_table(cur, args.genres)
            else:
                genre_index = genres_mod.sync_genres_from_records(cur, records)

            for idx, rec in enumerate(records, start=1):
                if not rec.get("type"):
                    skipped_no_type += 1
                    continue

                s_count, ep_count = load_record(cur, rec, genre_index, unmapped_genres)
                inserted += 1
                total_seasons += s_count
                total_episodes += ep_count

                if idx % 100 == 0:
                    print(f"  ...обработано {idx} записей")

    print("\n--- Сводка ---")
    print(f"Вставлено/обновлено записей content: {inserted}")
    print(f"Пропущено (не определён type): {skipped_no_type}")
    print(f"Сезонов создано: {total_seasons}")
    print(f"Эпизодов создано: {total_episodes}")

    if unmapped_genres:
        unique = sorted(set(unmapped_genres))
        print(f"\n⚠ ВНИМАНИЕ: жанры из data/kodik.json отсутствуют в genres.json ({len(unique)} шт.):")
        print("Созданы как fallback. Проверьте genres.json — возможно, список нужно дополнить:")
        for g in unique:
            print(f"  ! {g}")
        save_json("unmapped_genres.json", unique)
        print("Список сохранён в unmapped_genres.json")
    else:
        print("Все жанры из data/kodik.json найдены в genres.json — расхождений нет.")


if __name__ == "__main__":
    main()
