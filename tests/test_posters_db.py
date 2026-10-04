"""Постеры на реальном Postgres (TEST_DATABASE_URL).

Нужна схема CMS с media, content.poster_id и _content_v.version_poster_id.
Таблицы ОЧИЩАЮТСЯ — используйте тестовую БД.
"""
import json
import os
from types import SimpleNamespace

import psycopg2
import pytest

from kodik_pipeline import load, posters

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL не задан")


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw["Key"])


CFG = SimpleNamespace(bucket="media", public_url="http://s3/media")


@pytest.fixture()
def downloads():
    return []


@pytest.fixture()
def db(monkeypatch, tmp_path, downloads):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn = psycopg2.connect(URL)
    cur = conn.cursor()  # как в проде: autocommit выключен, SAVEPOINT работает
    cur.execute("SELECT count(*) FROM information_schema.columns WHERE column_name IN ('poster_id','version_poster_id')")
    if cur.fetchone()[0] != 2:
        pytest.skip("в тестовой БД нет колонок постера")
    cur.execute("TRUNCATE episodes, seasons, _content_v_rels, _content_v, content_rels, content, genres, media RESTART IDENTITY CASCADE")
    conn.commit()
    def fake_download(url, **kw):
        downloads.append(url)
        return b"img", "image/jpeg", 100, 150

    monkeypatch.setattr(posters, "download_image", fake_download)
    yield cur
    conn.close()


def import_records(tmp_path, records):  # load открывает своё соединение
    path = tmp_path / "k.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    load.main([str(path)])


def rec(title, kodik_id, poster="http://img/1.jpg"):
    return {"type": "movie", "titleEn": title, "titleRu": title, "slug": title.lower(),
            "kodikId": kodik_id, "genres": [], "posterUrl": poster}


def run(cur, r, s3):
    media_cols = posters.table_columns(cur, "media")
    version_cols = posters.table_columns(cur, "_content_v")
    status = posters.process_record(cur, r, s3, CFG, media_cols, ["poster_id"], version_cols=version_cols)
    cur.connection.commit()
    return status


def test_poster_linked_in_content_and_versions(db, tmp_path):
    r = rec("Show", "m-1")
    import_records(tmp_path, [r])
    s3 = FakeS3()
    assert run(db, r, s3) == "ok"
    db.execute("SELECT poster_id FROM content"); media_id = db.fetchone()[0]
    assert media_id is not None
    db.execute("SELECT DISTINCT version_poster_id FROM _content_v"); assert db.fetchall() == [(media_id,)]
    assert s3.puts == ["show.jpg"]


def test_non_empty_poster_is_skipped(db, tmp_path, downloads):
    r = rec("Show", "m-1")
    import_records(tmp_path, [r])
    s3 = FakeS3()
    assert run(db, r, s3) == "ok"
    db.execute("SELECT updated_at FROM content"); before = db.fetchone()[0]

    assert run(db, r, s3) == "has_poster"
    assert len(downloads) == 1 and len(s3.puts) == 1  # ни скачивания, ни загрузки
    db.execute("SELECT updated_at FROM content"); assert db.fetchone()[0] == before  # ни обновления
    db.execute("SELECT count(*) FROM media"); assert db.fetchone()[0] == 1


def test_no_content_means_no_download(db, downloads):
    s3 = FakeS3()
    assert run(db, rec("Ghost", "m-9"), s3) == "no_content"
    assert downloads == [] and s3.puts == []


def test_existing_media_is_linked_without_download(db, tmp_path, downloads):
    r = rec("Show", "m-1")
    import_records(tmp_path, [r])
    db.execute("INSERT INTO media (filename) VALUES ('show.jpg')")
    db.connection.commit()
    s3 = FakeS3()
    assert run(db, r, s3) == "exists"
    assert downloads == [] and s3.puts == []
    db.execute("SELECT poster_id FROM content"); assert db.fetchone()[0] == 1


def test_reload_keeps_version_poster(db, tmp_path):
    r = rec("Show", "m-1")
    import_records(tmp_path, [r])
    run(db, r, FakeS3())
    import_records(tmp_path, [r])  # load пересоздаёт версии
    db.execute("SELECT DISTINCT version_poster_id FROM _content_v")
    assert db.fetchall() == [(1,)]
