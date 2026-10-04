"""Жанры и upsert content на реальном Postgres (TEST_DATABASE_URL).

Схема в тестовой БД не должна иметь уникальных индексов на content.title_en
и genres.title — именно в такой схеме раньше падало
«there is no unique or exclusion constraint matching the ON CONFLICT specification».
"""
import json
import os

import psycopg2
import pytest

from kodik_pipeline import genres as g
from kodik_pipeline import load

URL = os.environ.get("TEST_DATABASE_URL")


def test_normalize_genre_title():
    assert g.normalize_genre_title("  Экшен ") == "экшен"
    assert g.normalize_genre_title("Научная   Фантастика") == "научная фантастика"
    assert g.normalize_genre_title(None) == ""
    assert g.unique_genre_titles(["Экшен", "ЭКШЕН", " ", "драма", None]) == ["экшен", "драма"]


needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL не задан")


@pytest.fixture()
def cur(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("DROP INDEX IF EXISTS genres_title_unique_idx")
    c.execute("TRUNCATE episodes, seasons, _content_v_rels, _content_v, content_rels, content, genres RESTART IDENTITY CASCADE")
    yield c
    conn.close()


def rec(title, kodik_id, genres, **kw):
    base = {"type": "movie", "titleEn": title, "titleRu": title, "slug": title.lower().replace(" ", "-"),
            "kodikId": kodik_id, "genres": genres}
    base.update(kw)
    return base


@needs_db
def test_load_works_without_unique_title_en(cur, tmp_path):
    path = tmp_path / "k.json"
    records = [rec("Enen no Shouboutai", "movie-1", ["Экшен"]), rec("Monkey Turn", "movie-2", ["Спорт"])]
    path.write_text(json.dumps(records), encoding="utf-8")
    load.main([str(path)])
    load.main([str(path)])  # повторный запуск обновляет, а не дублирует
    cur.execute("SELECT count(*), count(DISTINCT title_en) FROM content")
    assert cur.fetchone() == (2, 2)


@needs_db
def test_genres_are_lowercase_and_unique(cur, tmp_path):
    cur.execute("INSERT INTO genres (title, slug) VALUES ('Экшен', 'a'), ('экшен', 'b'), ('ДРАМА', 'c')")
    path = tmp_path / "k.json"
    records = [rec("A", "m-1", ["ЭКШЕН", "экшен", "Драма"]), rec("B", "m-2", ["Экшен", "Комедия"])]
    path.write_text(json.dumps(records), encoding="utf-8")
    cur.execute("INSERT INTO content (title_en, slug) VALUES ('A', 'a-old')")
    cur.execute("INSERT INTO content_rels (parent_id, path, genres_id) VALUES (1, 'genres', 2)")  # связь с дублем

    load.main([str(path)])

    cur.execute("SELECT title FROM genres ORDER BY title")
    assert [r[0] for r in cur.fetchall()] == ["драма", "комедия", "экшен"]
    cur.execute("SELECT count(*) FROM content_rels c JOIN genres g ON g.id = c.genres_id "
                "WHERE c.parent_id = 1 AND g.title = 'экшен'")
    assert cur.fetchone()[0] == 1  # без повтора жанра у тайтла
    cur.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'genres_title_unique_idx'")
    assert cur.fetchone()[0] == 1
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO genres (title, slug) VALUES ('экшен', 'zzz')")


@needs_db
def test_sync_genres_file_merges_case_duplicates(cur, tmp_path):
    cur.execute("INSERT INTO genres (title, slug) VALUES ('Аниме', 'old-anime')")
    cur.execute("INSERT INTO content (title_en, slug) VALUES ('A', 'a')")
    cur.execute("INSERT INTO content_rels (parent_id, path, genres_id) VALUES (1, 'genres', 1)")
    gj = tmp_path / "genres.json"
    gj.write_text(json.dumps([{"id": 7, "title": "аниме", "slug": "anime"}]), encoding="utf-8")

    g.sync_genres_table(cur, str(gj))

    cur.execute("SELECT id, title FROM genres")
    assert cur.fetchall() == [(7, "аниме")]
    cur.execute("SELECT genres_id FROM content_rels")
    assert cur.fetchall() == [(7,)]  # связь перенесена, а не потеряна


@needs_db
def test_all_content_is_published(cur, tmp_path):
    cur.execute("INSERT INTO content (title_en, slug, status, _status) VALUES ('Old', 'old', 'draft', 'draft')")
    cur.execute("INSERT INTO _content_v (parent_id, version_status, version__status, latest) VALUES (1, 'draft', 'draft', true)")
    path = tmp_path / "k.json"
    path.write_text(json.dumps([rec("New", "m-1", [])]), encoding="utf-8")

    load.main([str(path)])

    cur.execute("SELECT DISTINCT status, _status FROM content")
    assert cur.fetchall() == [("published", "published")]
    cur.execute("SELECT DISTINCT version_status FROM _content_v")
    assert cur.fetchall() == [("published",)]
    cur.execute("SELECT version__status FROM _content_v WHERE parent_id = 1")
    assert cur.fetchall() == [("published",)]
