from kodik_pipeline import posters, posters_sync



class Cfg:
    bucket = "b"
    public_url = "http://cdn"


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw["Key"])


class FakeCur:
    """Отвечает на SELECT по очереди из scripted, остальное просто записывает."""

    def __init__(self, selects):
        self.selects = list(selects)
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.selects.pop(0)


REC = {
    "titleEn": "Eureka", "titleRu": "Эврика", "slug": "eureka", "kodikId": "k1",
    "screenshots": ["", "ftp://x/1.jpg", "https://shiki/1.jpg", "https://shiki/2.jpg"],
}


def test_first_screenshot_url_skips_invalid():
    assert posters.first_screenshot_url(REC) == "https://shiki/1.jpg"
    assert posters.first_screenshot_url({"screenshots": []}) is None
    assert posters.first_screenshot_url({"screenshots": "oops"}) is None
    assert posters.first_screenshot_url({}) is None


def test_no_screenshot_no_db(monkeypatch):
    cur = FakeCur([])
    assert posters.process_backdrop(cur, {"titleEn": "a", "slug": "a"}, FakeS3(), Cfg, set()) == "no_url"
    assert cur.sql == []


def test_existing_backdrop_is_not_downloaded(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("не должно скачиваться")
    monkeypatch.setattr(posters, "download_image", boom)
    cur = FakeCur([(7, 99)])
    s3 = FakeS3()
    assert posters.process_backdrop(cur, REC, s3, Cfg, set()) == "has_backdrop"
    assert s3.puts == []


def test_backdrop_uploaded_and_linked(monkeypatch):
    urls = []
    monkeypatch.setattr(
        posters, "download_image",
        lambda url, **k: (urls.append(url), (b"img", "image/jpeg", 1280, 720))[1],
    )
    # content(id=7, backdrop NULL) -> media не найдено -> INSERT RETURNING id=42
    cur = FakeCur([(7, None), None, (42,)])
    s3 = FakeS3()
    status = posters.process_backdrop(cur, REC, s3, Cfg, {"filename", "alt", "url", "mime_type"})
    assert status == "ok"
    assert urls == ["https://shiki/1.jpg"]  # именно первый валидный скриншот
    assert s3.puts == ["eureka-backdrop.jpg"]
    joined = " | ".join(q for q, _ in cur.sql)
    assert "WHERE kodik_id" in joined
    assert "UPDATE content SET backdrop_id" in joined and "backdrop_id IS NULL" in joined
    assert "UPDATE _content_v SET version_backdrop_id = COALESCE" in joined


def test_missing_content(monkeypatch):
    cur = FakeCur([None])
    assert posters.process_backdrop(cur, REC, FakeS3(), Cfg, set()) == "no_content"


def test_copy_images_to_versions(monkeypatch):
    cols = {
        "content": {"poster_id", "backdrop_id"},
        "_content_v": {"version_poster_id", "version_backdrop_id"},
    }
    monkeypatch.setattr(posters_sync, "table_columns", lambda cur, t: cols[t])
    cur = FakeCur([])
    posters_sync.copy_images_to_versions(cur, 3)
    sql, params = cur.sql[0]
    assert "version_poster_id = c.poster_id" in sql and "version_backdrop_id = c.backdrop_id" in sql
    assert params == (3,)
    cols["_content_v"] = set()
    cur2 = FakeCur([])
    posters_sync.copy_images_to_versions(cur2, 3)
    assert cur2.sql == []
