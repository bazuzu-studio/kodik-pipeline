"""Интеграционные тесты на реальном Postgres.

Запуск:  TEST_DATABASE_URL=postgres://user@host:5432/db pytest tests/test_integration_db.py
Нужна тестовая БД, в которой применены миграции CMS (`pnpm payload migrate`),
включая колонки release_status / version_release_status (enum)
и franchise_id / version_franchise_id (миграция 20261003_120000).
Без TEST_DATABASE_URL тесты пропускаются. Таблицы ОЧИЩАЮТСЯ — используйте тестовую БД!
Схему тесты не меняют.
"""
import argparse
import os

import psycopg2
import pytest

from kodik_pipeline import api, load, ongoing
from kodik_pipeline.json_io import save_json

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL не задан")


def serial(kodik_id, year, episodes, status="ongoing", updated="2026-09-01T00:00:00Z"):
    return {
        "id": kodik_id, "type": "anime-serial", "title": "Show", "title_orig": "Show",
        "year": year, "kinopoisk_id": "777", "shikimori_id": kodik_id[-1],
        "updated_at": updated,
        "material_data": {"title": "Шоу", "title_en": "Show", "anime_status": status,
                          "anime_genres": ["Экшен"]},
        # у Kodik сезон каждой записи — «1», в БД франшиза будет перенумерована
        "seasons": {"1": {"episodes": {str(n): f"//x/{kodik_id}/{n}/v1" for n in episodes}}},
    }


@pytest.fixture()
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.delenv("RELEASE_STATUS_COLUMN", raising=False)
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE episodes, seasons, _content_v_rels, _content_v, content_rels, content, genres RESTART IDENTITY CASCADE")
    cur.execute("""SELECT count(*) FROM information_schema.columns
                   WHERE table_name IN ('content', '_content_v')
                     AND column_name IN ('release_status', 'version_release_status')""")
    if cur.fetchone()[0] != 2:
        pytest.skip("в тестовой БД нет колонок release_status — примените миграции CMS")
    yield cur
    conn.close()


def import_catalog(monkeypatch, tmp_path, items):
    monkeypatch.setattr(api, "iter_items", lambda **_: iter(items))
    path = tmp_path / "kodik.json"
    save_json(str(path), api.fetch_normalized())
    load.main([str(path)])


def ongoing_args(**kw):
    base = dict(token="t", translation_id="609", limit=None, delay=0, max_pages=None,
                dry_run=False, no_recheck=False, recheck_limit=200)
    base.update(kw)
    return argparse.Namespace(**base)


def episodes_of(cur, title_en):
    cur.execute("""SELECT s.season_number, e.episode_number, e.player_link
                   FROM episodes e JOIN seasons s ON s.id = e.season_id
                   JOIN content c ON c.id = s.content_id
                   WHERE c.title_en = %s ORDER BY 1, 2""", (title_en,))
    return cur.fetchall()


def test_update_ongoing_adds_episodes_to_renumbered_season(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("serial-1", 2020, [1, 2], "released"),
                                           serial("serial-2", 2022, [1, 2])])
    assert [r[0] for r in episodes_of(db, "Show S2")] == [2, 2]  # сезон в БД = 2

    new = serial("serial-2", 2022, [1, 2, 3])
    new["seasons"]["1"]["episodes"]["1"] = "//x/serial-2/1/v2"  # ссылка сменилась
    unknown = serial("serial-9", 2026, [1])
    unknown["kinopoisk_id"] = "999"
    monkeypatch.setattr(api, "fetch_ongoing",
                        lambda **_: [api.normalize_item(new), api.normalize_item(unknown)])

    ongoing.run(ongoing_args(no_recheck=True))

    rows = episodes_of(db, "Show S2")
    assert [(s, e) for s, e, _ in rows] == [(2, 1), (2, 2), (2, 3)]  # НЕ создан сезон 1
    assert rows[0][2].endswith("/v2") and rows[1][2].endswith("/2/v1")
    assert len(episodes_of(db, "Show")) == 2  # S1 не тронут
    db.execute("SELECT count(*) FROM seasons"); assert db.fetchone()[0] == 2


def test_dry_run_rolls_back(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("serial-2", 2022, [1])])
    monkeypatch.setattr(api, "fetch_ongoing",
                        lambda **_: [api.normalize_item(serial("serial-2", 2022, [1, 2]))])
    ongoing.run(ongoing_args(dry_run=True, no_recheck=True))
    assert len(episodes_of(db, "Show")) == 1


def test_status_saved_and_finished_title_rechecked(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("serial-1", 2020, [1, 2]),
                                           serial("serial-2", 2022, [1])])
    db.execute("SELECT title_en, release_status FROM content ORDER BY id")
    assert db.fetchall() == [("Show", "ongoing"), ("Show S2", "ongoing")]  # load пишет статус

    # serial-1 пропал из онгоингов и на самом деле завершился, выйдя ещё одной серией
    finished = serial("serial-1", 2020, [1, 2, 3], "released")
    monkeypatch.setattr(api, "fetch_ongoing",
                        lambda **_: [api.normalize_item(serial("serial-2", 2022, [1, 2]))])
    monkeypatch.setattr(api, "fetch_by_kodik_id",
                        lambda kid, **_: api.normalize_item(finished) if kid == "serial-1" else None)

    ongoing.run(ongoing_args())

    db.execute("SELECT title_en, release_status FROM content ORDER BY id")
    assert db.fetchall() == [("Show", "released"), ("Show S2", "ongoing")]
    db.execute("SELECT DISTINCT version_release_status FROM _content_v WHERE version_title_en='Show'")
    assert db.fetchall() == [("released",)]
    assert len(episodes_of(db, "Show")) == 3


def test_works_without_status_column(db, monkeypatch, tmp_path):
    """Если колонки статуса нет (другое имя) — серии обновляются, статус не пишется."""
    monkeypatch.setenv("RELEASE_STATUS_COLUMN", "no_such_column")
    import_catalog(monkeypatch, tmp_path, [serial("serial-2", 2022, [1])])
    monkeypatch.setattr(api, "fetch_ongoing",
                        lambda **_: [api.normalize_item(serial("serial-2", 2022, [1, 2]))])
    monkeypatch.setattr(api, "fetch_by_kodik_id",
                        lambda *a, **k: pytest.fail("перепроверка без колонки статуса не нужна"))
    ongoing.run(ongoing_args())
    assert len(episodes_of(db, "Show")) == 2
    db.execute("SELECT release_status FROM content")
    assert db.fetchall() == [(None,)]


def test_unknown_status_value_does_not_break_enum_column(db, monkeypatch, tmp_path):
    """Kodik может прислать статус вне anons/ongoing/released — запись не должна падать."""
    item = serial("serial-2", 2022, [1], status="какой-то новый статус")
    import_catalog(monkeypatch, tmp_path, [item])
    db.execute("SELECT count(*), max(release_status::text) FROM content")
    assert db.fetchone() == (1, None)


def test_empty_ongoing_list_leaves_db_untouched(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("serial-2", 2022, [1])])
    monkeypatch.setattr(api, "fetch_ongoing", lambda **_: [])
    with pytest.raises(SystemExit):
        ongoing.run(ongoing_args())


def test_load_skips_broken_record_but_keeps_others(db, monkeypatch, tmp_path):
    items = [serial("serial-1", 2020, [1]), serial("serial-2", 2022, [1])]
    monkeypatch.setattr(api, "iter_items", lambda **_: iter(items))
    records = api.fetch_normalized()
    records[0]["releaseYear"] = "не число"  # сломает INSERT
    path = tmp_path / "k.json"
    save_json(str(path), records)
    with pytest.raises(SystemExit):
        load.main([str(path)])
    db.execute("SELECT count(*) FROM content")
    assert db.fetchone()[0] == 1  # вторая запись сохранена


def test_advisory_lock_blocks_concurrent_writer(db, monkeypatch, tmp_path):
    other = psycopg2.connect(URL)
    try:
        cur = other.cursor()
        assert ongoing.try_advisory_lock(cur) is True  # «sync» держит блокировку
        monkeypatch.setattr(api, "fetch_ongoing",
                            lambda **_: [api.normalize_item(serial("serial-2", 2022, [1]))])
        with pytest.raises(SystemExit, match="уже пишет"):
            ongoing.run(ongoing_args(no_recheck=True))
    finally:
        other.rollback()
        other.close()


def test_reload_is_idempotent_and_title_en_is_not_a_key(db, monkeypatch, tmp_path):
    """В CMS unique-индекс по title_en удалён: повторный load не должен падать
    (раньше ON CONFLICT (title_en) требовал этот индекс) и не плодить дубли;
    два тайтла с одинаковым title_en живут рядом."""
    items = [serial("serial-1", 2020, [1]), serial("serial-2", 2022, [1])]
    import_catalog(monkeypatch, tmp_path, items)
    import_catalog(monkeypatch, tmp_path, items)
    db.execute("SELECT count(*), count(DISTINCT kodik_id), count(DISTINCT slug) FROM content")
    assert db.fetchone() == (2, 2, 2)

    db.execute("SELECT slug FROM content ORDER BY id")
    slugs_before = db.fetchall()
    import_catalog(monkeypatch, tmp_path, items)
    db.execute("SELECT slug FROM content ORDER BY id")
    assert db.fetchall() == slugs_before  # URL тайтлов стабильны


def test_load_publishes_and_sets_franchise(db, monkeypatch, tmp_path):
    import_catalog(monkeypatch, tmp_path, [serial("serial-1", 2020, [1]), serial("serial-2", 2022, [1])])
    db.execute("SELECT DISTINCT _status::text FROM content")
    assert db.fetchall() == [("published",)]  # публичное чтение отдаёт только published
    db.execute("SELECT DISTINCT franchise_id FROM content")
    assert db.fetchall() == [("777",)]
    db.execute("SELECT DISTINCT version_franchise_id FROM _content_v")
    assert db.fetchall() == [("777",)]


def test_load_notifies_frontend_only_after_commit(db, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(load, "notify_frontend", lambda: calls.append(1))
    import_catalog(monkeypatch, tmp_path, [serial("serial-2", 2022, [1])])
    assert calls == [1]

    monkeypatch.setattr(api, "fetch_ongoing",
                        lambda **_: [api.normalize_item(serial("serial-2", 2022, [1, 2]))])
    monkeypatch.setattr(ongoing, "notify_frontend", lambda: calls.append(2))
    ongoing.run(ongoing_args(dry_run=True, no_recheck=True))
    assert calls == [1]  # dry-run кэш не сбрасывает
    ongoing.run(ongoing_args(no_recheck=True))
    assert calls == [1, 2]
