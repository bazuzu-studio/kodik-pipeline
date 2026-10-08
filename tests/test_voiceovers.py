"""Справочник озвучек: проверка voiceovers.json и синхронизация с Postgres (TEST_DATABASE_URL)."""
import json
import os
from pathlib import Path

import psycopg2
import pytest

from kodik_pipeline import voiceovers as vo
from kodik_pipeline.text_utils import slugify

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL не задан")
CANONICAL = Path(__file__).resolve().parent.parent / "voiceovers.json"

DDL = """
DROP TABLE IF EXISTS voiceovers;
CREATE TABLE voiceovers (
  id serial PRIMARY KEY, title text NOT NULL UNIQUE, slug text NOT NULL UNIQUE,
  kodik_translation_id numeric UNIQUE,
  updated_at timestamptz DEFAULT now() NOT NULL, created_at timestamptz DEFAULT now() NOT NULL
)
"""


# ─── файл ─────────────────────────────────────────────────────────

def test_canonical_file_has_all_45_voiceovers_in_order():
    items = vo.load_voiceovers(str(CANONICAL))
    assert len(items) == 45
    assert [i["title"] for i in items][:3] == ["AniLibria", "AniDub", "JAM CLUB"]
    assert [i["title"] for i in items][-1] == "Kitsune Studio"
    assert len({i["slug"] for i in items}) == 45
    assert len({i["title"].casefold() for i in items}) == 45


def test_canonical_titles_are_kept_exactly_as_given():
    titles = {i["title"] for i in vo.load_voiceovers(str(CANONICAL))}
    for expected in ("DeliciousDub (ChillDUB)", "Re: Voice", "AniLeague.tv", "HekaiPROject", "AnimeVostorg", "STEPonee"):
        assert expected in titles


def test_slug_is_generated_when_missing(tmp_path):
    f = tmp_path / "v.json"
    f.write_text(json.dumps([{"title": "Re: Voice"}]), encoding="utf-8")
    assert vo.load_voiceovers(str(f))[0]["slug"] == slugify("Re: Voice") == "re-voice"


@pytest.mark.parametrize("data", [
    [{"title": "A", "slug": "a"}, {"title": "B", "slug": "a"}],          # одинаковый slug
    [{"title": "Anidub"}, {"title": "ANIDUB"}],                           # одинаковое название без учёта регистра
    [{"title": "  "}],                                                    # пустое название
    [{"title": "A", "kodikTranslationId": "609"}],                        # id не число
    {"title": "A"},                                                       # не массив
])
def test_invalid_file_is_rejected(tmp_path, data):
    f = tmp_path / "v.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit):
        vo.load_voiceovers(str(f))


# ─── БД ───────────────────────────────────────────────────────────

@pytest.fixture()
def cur():
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    c = conn.cursor()
    c.execute(DDL)
    yield c
    c.execute("DROP TABLE IF EXISTS voiceovers")
    conn.close()


def write(tmp_path, items):
    f = tmp_path / "v.json"
    f.write_text(json.dumps(items), encoding="utf-8")
    return str(f)


@needs_db
def test_first_sync_creates_all_with_ids_in_file_order(cur):
    stats = vo.sync_voiceovers_table(cur, str(CANONICAL))
    assert (stats.created, stats.updated, stats.unchanged) == (45, 0, 0)
    cur.execute("SELECT id, title FROM voiceovers ORDER BY id")
    rows = cur.fetchall()
    assert rows[0] == (1, "AniLibria") and rows[-1] == (45, "Kitsune Studio")


@needs_db
def test_second_sync_changes_nothing(cur):
    vo.sync_voiceovers_table(cur, str(CANONICAL))
    cur.execute("SELECT max(updated_at) FROM voiceovers"); before = cur.fetchone()[0]
    stats = vo.sync_voiceovers_table(cur, str(CANONICAL))
    assert (stats.created, stats.updated, stats.unchanged) == (0, 0, 45)
    cur.execute("SELECT count(*), max(updated_at) FROM voiceovers")
    assert cur.fetchone() == (45, before)  # строки не трогались


@needs_db
def test_manual_row_with_other_slug_is_adopted_not_duplicated(cur, tmp_path):
    cur.execute("INSERT INTO voiceovers (title, slug) VALUES ('anidub', 'my-anidub') RETURNING id")
    manual_id = cur.fetchone()[0]
    stats = vo.sync_voiceovers_table(cur, write(tmp_path, [{"title": "AniDub"}]))
    assert (stats.created, stats.updated) == (0, 1)
    cur.execute("SELECT id, title, slug FROM voiceovers")
    assert cur.fetchall() == [(manual_id, "AniDub", "anidub")]


@needs_db
def test_rows_outside_file_are_kept_and_counted(cur, tmp_path):
    cur.execute("INSERT INTO voiceovers (title, slug) VALUES ('Своя студия', 'svoya')")
    stats = vo.sync_voiceovers_table(cur, write(tmp_path, [{"title": "AniDub"}]))
    assert stats.outside_file == 1
    cur.execute("SELECT count(*) FROM voiceovers"); assert cur.fetchone()[0] == 2


@needs_db
def test_kodik_id_is_written_but_never_erased(cur, tmp_path):
    vo.sync_voiceovers_table(cur, write(tmp_path, [{"title": "AniLibria", "kodikTranslationId": 609}]))
    cur.execute("SELECT kodik_translation_id FROM voiceovers"); assert cur.fetchone()[0] == 609
    # в файле id убрали — значение в БД остаётся
    vo.sync_voiceovers_table(cur, write(tmp_path, [{"title": "AniLibria"}]))
    cur.execute("SELECT kodik_translation_id FROM voiceovers"); assert cur.fetchone()[0] == 609


@needs_db
def test_works_without_optional_columns(cur, tmp_path):
    cur.execute("DROP TABLE voiceovers")
    cur.execute("CREATE TABLE voiceovers (id serial PRIMARY KEY, title text NOT NULL, slug text NOT NULL)")
    stats = vo.sync_voiceovers_table(cur, write(tmp_path, [{"title": "AniDub", "kodikTranslationId": 1}]))
    assert stats.created == 1


@needs_db
def test_missing_table_gives_clear_error(cur):
    cur.execute("DROP TABLE voiceovers")
    with pytest.raises(SystemExit) as exc:
        vo.sync_voiceovers_table(cur, str(CANONICAL))
    assert "Voiceovers" in str(exc.value)


@needs_db
def test_dry_run_does_not_burn_ids(cur, monkeypatch, tmp_path):
    """--dry-run откатывает строки, но sequence не откатывается сама — команда возвращает её."""
    import argparse
    monkeypatch.setenv("DATABASE_URL", URL)
    vo.run(argparse.Namespace(file=str(CANONICAL), dry_run=True))
    cur.execute("SELECT count(*) FROM voiceovers"); assert cur.fetchone()[0] == 0

    vo.run(argparse.Namespace(file=str(CANONICAL), dry_run=False))
    cur.execute("SELECT min(id), max(id), count(*) FROM voiceovers")
    assert cur.fetchone() == (1, 45, 45)
