"""Поиск существующей записи и franchise_id — без БД, на фейковом курсоре."""
from kodik_pipeline import api
from kodik_pipeline.load import find_existing_content, resolve_content_slug, set_franchise_id


class FakeCursor:
    """Отвечает на SELECT-ы find_existing_content по заранее заданным строкам."""

    def __init__(self, by_kodik=None, legacy=None, slugs=()):
        self.by_kodik = by_kodik or {}
        self.legacy = legacy or {}
        self.slugs = set(slugs)
        self.queries = []
        self._row = None

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.queries.append((sql, params))
        if "WHERE kodik_id = %s" in sql:
            self._row = self.by_kodik.get(params[0])
        elif "WHERE title_en = %s" in sql:
            self._row = self.legacy.get((params[0], params[1]))
        elif "WHERE slug = %s" in sql:
            self._row = (1,) if params[0] in self.slugs else None

    def fetchone(self):
        return self._row


def rec(**kw):
    base = {"titleEn": "The Thing", "type": "movie", "kodikId": "movie-2", "slug": "the-thing"}
    base.update(kw)
    return base


def test_same_title_different_kodik_id_is_not_merged():
    """Ремейк с тем же titleEn — новая запись, а не перезапись оригинала."""
    cur = FakeCursor(by_kodik={"movie-1": (10, "the-thing")})
    assert find_existing_content(cur, rec(kodikId="movie-2")) is None


def test_found_by_kodik_id_keeps_slug():
    cur = FakeCursor(by_kodik={"movie-2": (11, "old-slug")})
    found = find_existing_content(cur, rec())
    assert found == (11, "old-slug")
    assert resolve_content_slug(cur, rec(), found[1]) == "old-slug"


def test_legacy_record_without_kodik_id_is_adopted_by_title():
    cur = FakeCursor(legacy={("The Thing", "movie"): (5, "the-thing")})
    assert find_existing_content(cur, rec()) == (5, "the-thing")


def test_new_record_gets_suffix_when_slug_taken():
    cur = FakeCursor(slugs={"the-thing"})
    assert resolve_content_slug(cur, rec(), None) == "the-thing-movie-2"


def test_set_franchise_id_respects_schema_and_empty_values():
    cur = FakeCursor()
    set_franchise_id(cur, 1, "777", (False, False))
    set_franchise_id(cur, 1, None, (True, True))
    assert cur.queries == []  # нет колонок / пустое значение — ничего не пишем

    set_franchise_id(cur, 1, "777", (True, True))
    assert [q[0].split(" SET ")[0] for q in cur.queries] == ["UPDATE content", "UPDATE _content_v"]


def test_franchise_id_matches_cms_migration_format(monkeypatch):
    """CMS проставила существующим записям franchise_id = голый kinopoisk_id."""
    def item(kodik_id, year):
        return {"id": kodik_id, "type": "anime-serial", "title": "Show", "year": year,
                "kinopoisk_id": "777", "shikimori_id": kodik_id[-1],
                "material_data": {"title": "Шоу", "title_en": "Show", "anime_status": "ongoing"},
                "seasons": {"1": {"episodes": {"1": "//x/1"}}}}

    monkeypatch.setattr(api, "iter_items", lambda **_: iter([item("serial-1", 2020), item("serial-2", 2022)]))
    records = api.fetch_normalized()
    assert {r["franchiseId"] for r in records} == {"777"}
    assert sorted(r["seasonNumber"] for r in records) == [1, 2]
