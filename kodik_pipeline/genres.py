"""
Канонический список жанров (genres.json) — единственный источник правды.

merge_kodik_sources.py использует его, чтобы отфильтровать "сырые" жанры
Kodik и оставить только те, что есть в каноническом списке.
load_to_postgres.py использует его, чтобы синхронизировать таблицу genres
в БД (upsert с сохранением id, удаление устаревших записей).

Раньше обе логики были продублированы (в merge_kodik_sources.py — простая
загрузка в dict, в load_to_postgres.py — полноценный upsert в БД) — здесь
они собраны в одном месте.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .json_io import load_json
from .text_utils import slugify

UPSERT_GENRE_WITH_ID_SQL = """
INSERT INTO genres (id, title, slug, created_at, updated_at)
VALUES (%(id)s, %(title)s, %(slug)s, COALESCE(%(created_at)s, now()), COALESCE(%(updated_at)s, now()))
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title,
    slug = EXCLUDED.slug,
    updated_at = now()
RETURNING id;
"""

DELETE_STALE_GENRES_SQL = "DELETE FROM genres WHERE id NOT IN %(kept_ids)s;"

# Fallback для жанров, которых нет в genres.json (встретились в дампах Kodik,
# но отсутствуют в каноническом списке) — создаются на лету и логируются.
UPSERT_GENRE_FALLBACK_SQL = """
INSERT INTO genres (title, slug, created_at, updated_at)
VALUES (%(title)s, %(slug)s, now(), now())
ON CONFLICT (title) DO UPDATE SET
    slug = EXCLUDED.slug,
    updated_at = now()
RETURNING id;
"""

FIND_GENRE_BY_TITLE_SQL = "SELECT id, slug FROM genres WHERE title = %s LIMIT 1;"
FIND_GENRE_BY_SLUG_SQL = "SELECT id, title FROM genres WHERE slug = %s LIMIT 1;"


def unique_genre_slug(cur, title: str) -> str:
    """Возвращает slug, который не занят другим жанром.

    В старой версии кириллические названия превращались в ``unknown``.
    Даже после исправления slugify оставляем защиту от коллизий slug.
    """
    base = slugify(title)
    cur.execute(FIND_GENRE_BY_SLUG_SQL, (base,))
    row = cur.fetchone()
    if row is None or row[1] == title:
        return base

    suffix = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


def upsert_fallback_genre(cur, title: str) -> int:
    """Создаёт/обновляет жанр без нарушения unique(slug)."""
    cur.execute(FIND_GENRE_BY_TITLE_SQL, (title,))
    existing = cur.fetchone()
    slug = unique_genre_slug(cur, title)

    if existing:
        cur.execute(
            "UPDATE genres SET slug = %s, updated_at = now() WHERE id = %s RETURNING id;",
            (slug, existing[0]),
        )
        return cur.fetchone()[0]

    cur.execute(
        UPSERT_GENRE_FALLBACK_SQL,
        {"title": title, "slug": slug},
    )
    return cur.fetchone()[0]


def load_canonical_genres(path: str) -> list[dict[str, Any]]:
    return load_json(path)


def title_index(genres: list[dict[str, Any]]) -> dict[str, str]:
    """{название в нижнем регистре: оригинальное название}.
    Используется merge_kodik_sources.py для регистронезависимого
    сопоставления жанров Kodik с каноническим списком."""
    return {g["title"].casefold(): g["title"] for g in genres}


def sync_genres_table(cur, genres_path: str) -> dict[str, int]:
    """Загружает genres.json, апсертит каждый жанр с явным id, удаляет из
    БД жанры, отсутствующие в файле (каскадно удалятся и связи), и
    синхронизирует sequence. Возвращает {title: id}."""
    genres = load_canonical_genres(genres_path)

    index: dict[str, int] = {}
    kept_ids: list[int] = []

    for g in genres:
        gid = g["id"]
        kept_ids.append(gid)
        cur.execute(
            UPSERT_GENRE_WITH_ID_SQL,
            {
                "id": gid,
                "title": g["title"],
                "slug": g["slug"],
                "created_at": g.get("created_at"),
                "updated_at": g.get("updated_at"),
            },
        )
        cur.fetchone()
        index[g["title"].casefold()] = gid

    if kept_ids:
        cur.execute(DELETE_STALE_GENRES_SQL, {"kept_ids": tuple(kept_ids)})
        if cur.rowcount > 0:
            print(f"  Удалено устаревших жанров из БД: {cur.rowcount}")

    cur.execute("SELECT COALESCE(MAX(id), 1) FROM genres;")
    max_id = cur.fetchone()[0]
    cur.execute("SELECT setval('genres_id_seq', %s, %s);", (max_id, max_id > 0))
    print(f"Загружено жанров из {genres_path}: {len(index)}")
    return index


def get_or_create_genre(
    cur,
    genre_index: dict[str, int],
    title: str,
    unmapped_genres: list[str],
) -> int:
    """id жанра по каноническому индексу; если жанра нет — создаёт как
    fallback и добавляет в unmapped_genres для итогового отчёта."""
    canonical_title = genre_index.get(title.casefold())
    genre_id = canonical_title
    if genre_id is not None:
        return genre_id

    genre_id = upsert_fallback_genre(cur, title)
    genre_index[title.casefold()] = genre_id
    unmapped_genres.append(title)
    return genre_id


def sync_genres_from_records(cur, records: list[dict[str, Any]]) -> dict[str, int]:
    """Создаёт/находит жанры непосредственно из данных Kodik API.

    В API-режиме genres.json не требуется. Существующие жанры в БД не
    удаляются: каталог API может быть постраничным/фильтрованным.
    """
    index: dict[str, int] = {}
    titles: dict[str, str] = {}
    for rec in records:
        for title in rec.get("genres", []) or []:
            if isinstance(title, str) and title.strip():
                titles.setdefault(title.casefold(), title.strip())

    for title in titles.values():
        genre_id = upsert_fallback_genre(cur, title)
        index[title.casefold()] = genre_id

    print(f"Синхронизировано жанров из Kodik API: {len(index)}")
    return index
