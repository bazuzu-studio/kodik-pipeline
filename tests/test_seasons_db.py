"""Сезоны без дублей — на реальном Postgres (см. test_integration_db.py: TEST_DATABASE_URL, очищает таблицы)."""
import argparse
import os

import psycopg2
import pytest

from kodik_pipeline import api, fix_seasons, load, ongoing
from kodik_pipeline.franchise import detect_part
from kodik_pipeline.json_io import save_json

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL не задан")


def serial(kodik_id, year, shiki, title="Show", episodes=(1, 2), kp="777"):
    return {
        "id": kodik_id, "type": "anime-serial", "title": title, "title_orig": title, "year": year,
        "kinopoisk_id": kp, "shikimori_id": shiki,
        "material_data": {"title": title, "title_en": title, "anime_status": "released"},
        "seasons": {"1": {"episodes": {str(n): f"//x/{kodik_id}/{n}" for n in episodes}}},
    }


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE episodes, seasons, _content_v_rels, _content_v, content_rels, content, genres RESTART IDENTITY CASCADE")
    yield cur
    conn.close()


def import_catalog(monkeypatch, tmp_path, items):
    monkeypatch.setattr(api, "iter_items", lambda **_: iter(items))
    path = tmp_path / "kodik.json"
    save_json(str(path), api.fetch_normalized())
    load.main([str(path)])
    return path


def seasons_of(cur):
    cur.execute("""SELECT c.kodik_id, s.season_number, count(e.id) FROM seasons s
                   JOIN content c ON c.id = s.content_id LEFT JOIN episodes e ON e.season_id = s.id
                   GROUP BY 1, 2 ORDER BY 2, 1""")
    return cur.fetchall()


def test_earlier_season_added_later_renumbers_instead_of_duplicating(db, monkeypatch, tmp_path):
    base = [serial("k1", 2020, "10"), serial("k2", 2022, "20")]
    import_catalog(monkeypatch, tmp_path, base)
    assert seasons_of(db) == [("k1", 1, 2), ("k2", 2, 2)]

    # Kodik добавил в франшизу запись более раннего года: старые сезоны сдвигаются.
    import_catalog(monkeypatch, tmp_path, [serial("k0", 2018, "5")] + base)
    assert seasons_of(db) == [("k0", 1, 2), ("k1", 2, 2), ("k2", 3, 2)]  # ровно 3 строки, без дублей
    import_catalog(monkeypatch, tmp_path, [serial("k0", 2018, "5")] + base)
    db.execute("SELECT count(*) FROM seasons"); assert db.fetchone()[0] == 3


def test_partial_fetch_does_not_renumber_existing(db, monkeypatch, tmp_path):
    base = [serial("k1", 2020, "10"), serial("k2", 2022, "20"), serial("k3", 2024, "30")]
    import_catalog(monkeypatch, tmp_path, base)
    # неполная выдача (--max-pages): в данных только последний сезон
    import_catalog(monkeypatch, tmp_path, [base[2]])
    assert seasons_of(db) == [("k1", 1, 2), ("k2", 2, 2), ("k3", 3, 2)]


def test_parts_stored_under_one_season_number(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [
        serial("k1", 2020, "10", "Show Season 1"),
        serial("k2", 2022, "20", "Show Season 2"),
        serial("k3", 2023, "30", "Show Season 2 Part 2"),
    ])
    assert seasons_of(db) == [("k1", 1, 2), ("k2", 2, 2), ("k3", 2, 2)]
    db.execute("SELECT title FROM seasons s JOIN content c ON c.id = s.content_id WHERE c.kodik_id = 'k3'")
    assert detect_part(db.fetchone()[0]) == 2


def test_fix_seasons_merges_existing_duplicates(db, monkeypatch, tmp_path):
    path = import_catalog(monkeypatch, tmp_path, [serial("k1", 2020, "10"), serial("k2", 2022, "20")])
    # Состояние, которое оставляла старая версия: у k2 вторая строка (старый номер) с теми же сериями.
    db.execute("SELECT id FROM content WHERE kodik_id = 'k2'"); cid = db.fetchone()[0]
    db.execute("INSERT INTO seasons (content_id, season_number, title) VALUES (%s, 1, 'old') RETURNING id", (cid,))
    old = db.fetchone()[0]
    db.execute("INSERT INTO episodes (season_id, episode_number, title) VALUES (%s, 1, 'e'), (%s, 2, 'e')", (old, old))
    assert len(seasons_of(db)) == 3

    fix_seasons.run(argparse.Namespace(data=str(path), dry_run=True))
    assert len(seasons_of(db)) == 3  # dry-run откатывает

    fix_seasons.run(argparse.Namespace(data=str(path), dry_run=False))
    assert seasons_of(db) == [("k1", 1, 2), ("k2", 2, 2)]
    db.execute("SELECT count(*) FROM episodes WHERE season_id NOT IN (SELECT id FROM seasons)")
    assert db.fetchone()[0] == 0  # серии удалённого сезона не осиротели


def test_extra_season_with_unique_episodes_is_kept(db, monkeypatch, tmp_path):
    path = import_catalog(monkeypatch, tmp_path, [serial("k1", 2020, "10")])
    db.execute("SELECT id FROM content"); cid = db.fetchone()[0]
    db.execute("INSERT INTO seasons (content_id, season_number) VALUES (%s, 5) RETURNING id", (cid,))
    extra = db.fetchone()[0]
    db.execute("INSERT INTO episodes (season_id, episode_number, title) VALUES (%s, 99, 'уникальная')", (extra,))
    fix_seasons.run(argparse.Namespace(data=str(path), dry_run=False))
    assert len(seasons_of(db)) == 2  # ничего не теряем


def test_new_kodik_id_for_same_shikimori_reuses_content(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("old-id", 2020, "10")])
    import_catalog(monkeypatch, tmp_path, [serial("new-id", 2020, "10", episodes=(1, 2, 3))])
    db.execute("SELECT count(*), max(kodik_id) FROM content"); assert db.fetchone() == (1, "new-id")
    assert seasons_of(db) == [("new-id", 1, 3)]


def test_update_ongoing_does_not_add_season_when_db_has_duplicates(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("k1", 2020, "10"), serial("k2", 2022, "20")])
    db.execute("SELECT id FROM content WHERE kodik_id = 'k2'"); cid = db.fetchone()[0]
    db.execute("INSERT INTO seasons (content_id, season_number) VALUES (%s, 1) RETURNING id", (cid,))
    dup = db.fetchone()[0]
    db.execute("INSERT INTO episodes (season_id, episode_number, title) VALUES (%s, 1, 'e')", (dup,))

    monkeypatch.setattr(api, "fetch_ongoing", lambda **_: [api.normalize_item(serial("k2", 2022, "20", episodes=(1, 2, 3)))])
    ongoing.run(argparse.Namespace(token="t", translation_id="609", limit=None, delay=0, max_pages=None,
                                   dry_run=False, no_recheck=True, recheck_limit=200))
    assert seasons_of(db) == [("k1", 1, 2), ("k2", 2, 3)]
