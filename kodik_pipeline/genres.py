"""
Канонический список жанров (genres.json) — единственный источник правды.

Правила работы с жанрами:
  - Название жанра ВСЕГДА нормализуется (normalize_genre_title): пробелы
    схлопываются, регистр приводится к нижнему. В БД жанры хранятся в
    нижнем регистре, поэтому «Экшен», «экшен» и «ЭКШЕН» — один жанр.
  - Поиск существующего жанра ведётся по нормализованному названию в Python,
    а не через SQL lower() — так результат не зависит от collation БД.
  - Никаких ``ON CONFLICT (title)``: он требует уникального индекса, которого
    в схеме Payload по умолчанию нет. Вместо этого — поиск и вставка,
    а уникальность гарантирует ensure_unique_genres (слияние дублей +
    уникальный индекс).

sync_genres_table синхронизирует таблицу genres с файлом (upsert с
сохранением id, удаление устаревших записей, перенос связей с дублей).
get_or_create_genre сопоставляет жанры Kodik без учёта регистра, а неизвестные
создаёт как fallback и логирует.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from .json_io import load_json
from .text_utils import slugify

UNIQUE_GENRE_INDEX = "genres_title_unique_idx"

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

INSERT_GENRE_FALLBACK_SQL = """
INSERT INTO genres (title, slug, created_at, updated_at)
VALUES (%(title)s, %(slug)s, now(), now())
RETURNING id;
"""

FIND_GENRE_BY_SLUG_SQL = "SELECT id, title FROM genres WHERE slug = %s LIMIT 1;"

# Таблицы Payload со ссылками на жанры: (таблица, поле-родитель, условие по path).
GENRE_REL_TABLES: tuple[tuple[str, str], ...] = (
    ("content_rels", "genres"),
    ("_content_v_rels", "version.genres"),
)


# ─── Нормализация ───────────────────────────────────────────────

def normalize_genre_title(title: Any) -> str:
    """Каноническая форма названия жанра: без лишних пробелов, в нижнем
    регистре. Пустая строка — жанра нет."""
    if not isinstance(title, str):
        return ""
    return " ".join(title.split()).casefold()


def unique_genre_titles(titles: Iterable[Any]) -> list[str]:
    """Нормализованные названия без пустых и без повторов (порядок сохраняется)."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in titles or []:
        title = normalize_genre_title(raw)
        if title and title not in seen:
            seen.add(title)
            result.append(title)
    return result


# ─── Slug ───────────────────────────────────────────────────────

def unique_genre_slug(cur, title: str, *, own_id: int | None = None) -> str:
    """Возвращает slug, который не занят другим жанром.

    Даже после исправления slugify оставляем защиту от коллизий slug.
    """
    base = slugify(title)
    cur.execute(FIND_GENRE_BY_SLUG_SQL, (base,))
    row = cur.fetchone()
    if row is None or (own_id is not None and row[0] == own_id):
        return base

    suffix = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


# ─── Слияние дублей ─────────────────────────────────────────────

def merge_genre(cur, old_id: int, new_id: int) -> None:
    """Переносит все связи со старого жанра на новый (без дублей связей).
    Сам жанр old_id не удаляется — это делает вызывающий код."""
    if old_id == new_id:
        return
    for table, path in GENRE_REL_TABLES:
        # Если у родителя уже есть new_id — старую связь просто удаляем.
        cur.execute(
            f"""
            DELETE FROM {table} r
            WHERE r.genres_id = %(old)s AND r.path = %(path)s
              AND EXISTS (
                  SELECT 1 FROM {table} x
                  WHERE x.parent_id = r.parent_id AND x.path = r.path
                    AND x.genres_id = %(new)s
              )
            """,
            {"old": old_id, "new": new_id, "path": path},
        )
        cur.execute(
            f"UPDATE {table} SET genres_id = %(new)s WHERE genres_id = %(old)s",
            {"old": old_id, "new": new_id},
        )


def dedupe_genres(cur) -> int:
    """Склеивает жанры, совпадающие без учёта регистра/пробелов.

    Остаётся запись с наименьшим id, её title приводится к нормализованному
    виду; связи дублей переносятся на неё. Возвращает число удалённых дублей.
    """
    cur.execute("SELECT id, title FROM genres ORDER BY id")
    groups: dict[str, list[int]] = {}
    titles: dict[int, str] = {}
    for gid, title in cur.fetchall():
        titles[gid] = title
        key = normalize_genre_title(title)
        if key:
            groups.setdefault(key, []).append(gid)

    removed = 0
    for key, ids in groups.items():
        keep, dupes = ids[0], ids[1:]
        for dup in dupes:
            merge_genre(cur, dup, keep)
            cur.execute("DELETE FROM genres WHERE id = %s", (dup,))
            removed += 1
        if titles[keep] != key:
            cur.execute(
                "UPDATE genres SET title = %s, updated_at = now() WHERE id = %s",
                (key, keep),
            )
    if removed:
        print(f"  Объединено дублей жанров (без учёта регистра): {removed}")
    return removed


def ensure_unique_genres(cur) -> None:
    """Гарантирует уникальность жанров в таблице: сливает дубли и создаёт
    уникальный индекс по title (все названия хранятся в нижнем регистре,
    поэтому индекс по title = уникальность без учёта регистра).

    Если прав на CREATE INDEX нет — печатает предупреждение, импорт идёт дальше
    (дубли всё равно не создаются: поиск ведётся по нормализованному названию).
    """
    dedupe_genres(cur)
    cur.execute("SAVEPOINT genres_unique_idx")
    try:
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_GENRE_INDEX} ON genres (title)"
        )
    except Exception as exc:  # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT genres_unique_idx")
        print(f"  [WARN] не удалось создать уникальный индекс жанров: {exc}")
    else:
        cur.execute("RELEASE SAVEPOINT genres_unique_idx")


# ─── Fallback-жанры ─────────────────────────────────────────────

def find_genre_id(cur, title: str) -> int | None:
    """id жанра по нормализованному названию (регистронезависимо)."""
    key = normalize_genre_title(title)
    if not key:
        return None
    cur.execute("SELECT id, title FROM genres ORDER BY id")
    for gid, existing in cur.fetchall():
        if normalize_genre_title(existing) == key:
            return gid
    return None


def upsert_fallback_genre(cur, title: str) -> int:
    """Находит жанр без учёта регистра или создаёт новый (в нижнем регистре).
    Не использует ON CONFLICT и не нарушает unique(slug)."""
    key = normalize_genre_title(title)
    if not key:
        raise ValueError("пустое название жанра")

    existing_id = find_genre_id(cur, key)
    if existing_id is not None:
        return existing_id

    slug = unique_genre_slug(cur, key)
    cur.execute(INSERT_GENRE_FALLBACK_SQL, {"title": key, "slug": slug})
    return cur.fetchone()[0]


# ─── Канонический справочник ────────────────────────────────────

def load_canonical_genres(path: str) -> list[dict[str, Any]]:
    return load_json(path)


def title_index(genres: list[dict[str, Any]]) -> dict[str, str]:
    """{название в нижнем регистре: оригинальное название}.
    Для регистронезависимого сопоставления жанров Kodik
    с каноническим списком."""
    return {g["title"].casefold(): g["title"] for g in genres}


def sync_genres_table(cur, genres_path: str) -> dict[str, int]:
    """Загружает genres.json, апсертит каждый жанр с явным id, удаляет из
    БД жанры, отсутствующие в файле (каскадно удалятся и связи), и
    синхронизирует sequence. Возвращает {название в нижнем регистре: id}.

    Если в БД есть «лишний» жанр, совпадающий по названию (без учёта
    регистра) с каноническим, его связи переносятся на канонический id —
    привязки контента не теряются.
    """
    genres = load_canonical_genres(genres_path)

    canonical: dict[str, dict[str, Any]] = {}
    for g in genres:
        key = normalize_genre_title(g["title"])
        if not key:
            continue
        if key in canonical:
            raise SystemExit(
                f"Ошибка: в {genres_path} жанр «{g['title']}» повторяется (без учёта регистра)"
            )
        canonical[key] = g
    kept_ids = [g["id"] for g in canonical.values()]

    # 1. «Чужие» строки с тем же названием освобождают title/slug для
    #    канонических записей (иначе упрёмся в unique).
    cur.execute("SELECT id, title FROM genres ORDER BY id")
    clashing: list[tuple[int, str]] = []
    for gid, title in cur.fetchall():
        key = normalize_genre_title(title)
        if gid not in kept_ids and key in canonical:
            clashing.append((gid, key))
            cur.execute(
                "UPDATE genres SET title = %s, slug = %s WHERE id = %s",
                (f"__dup_{gid}", f"__dup_{gid}", gid),
            )

    # 2. Upsert канонических жанров с явными id.
    index: dict[str, int] = {}
    for key, g in canonical.items():
        gid = g["id"]
        cur.execute(
            UPSERT_GENRE_WITH_ID_SQL,
            {
                "id": gid,
                "title": key,
                "slug": g["slug"],
                "created_at": g.get("created_at"),
                "updated_at": g.get("updated_at"),
            },
        )
        cur.fetchone()
        index[key] = gid

    # 3. Переносим связи с дублей на канонические id.
    for old_id, key in clashing:
        merge_genre(cur, old_id, index[key])

    # 4. Удаляем устаревшие.
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
    key = normalize_genre_title(title)
    genre_id = genre_index.get(key)
    if genre_id is not None:
        return genre_id

    genre_id = upsert_fallback_genre(cur, key)
    genre_index[key] = genre_id
    unmapped_genres.append(key)
    return genre_id


def sync_genres_from_records(cur, records: list[dict[str, Any]]) -> dict[str, int]:
    """Создаёт/находит жанры непосредственно из данных Kodik API.

    В API-режиме genres.json не требуется. Существующие жанры в БД не
    удаляются: каталог API может быть постраничным/фильтрованным.
    """
    all_titles: list[Any] = []
    for rec in records:
        all_titles.extend(rec.get("genres", []) or [])

    index: dict[str, int] = {}
    for key in unique_genre_titles(all_titles):
        index[key] = upsert_fallback_genre(cur, key)

    print(f"Синхронизировано жанров из Kodik API: {len(index)}")
    return index
