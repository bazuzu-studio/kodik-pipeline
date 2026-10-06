from kodik_pipeline import seo


class FakeCur:
    def __init__(self, cols):
        self.cols, self.executed, self._last = cols, [], None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = params[0] if params else None

    def fetchall(self):
        return [(c,) for c in self.cols[self._last]]


def test_detect_seo_support(monkeypatch):
    full = {
        "content": ["meta_title", "meta_description", "meta_image_id"],
        "_content_v": ["version_meta_title", "version_meta_description", "version_meta_image_id"],
    }
    monkeypatch.setattr(seo, "table_columns", lambda cur, t: set(full[t]))
    assert seo.detect_seo_support(None).enabled
    full["_content_v"] = []
    assert not seo.detect_seo_support(None).enabled


def test_copy_skipped_without_columns():
    cur = FakeCur({})
    seo.copy_seo_to_versions(cur, 1, seo.SeoSupport(content=True, version=False))
    assert cur.executed == []


def test_copy_runs_with_columns():
    cur = FakeCur({})
    seo.copy_seo_to_versions(cur, 7, seo.SeoSupport(content=True, version=True))
    assert len(cur.executed) == 1 and cur.executed[0][1] == (7,)


def test_meta_title_fits_and_uses_year():
    t = seo.build_meta_title({"titleRu": "Атака титанов", "releaseYear": 2013})
    assert t == "Атака титанов (2013) — смотреть онлайн"
    long = seo.build_meta_title({"titleRu": "А" * 80, "releaseYear": 2020})
    assert len(long) <= seo.TITLE_MAX and long.endswith("…")
    assert seo.build_meta_title({}) is None


def test_meta_description_from_api_is_cleaned_and_truncated():
    raw = "<p>Давно  [character=1]Эрен[/character] жил</p> " + "слово " * 60 + "(Источник: Wiki)"
    d = seo.build_meta_description({"description": raw})
    assert len(d) <= seo.DESCRIPTION_MAX and d.endswith("…")
    assert "<" not in d and "[" not in d and "Источник" not in d


def test_meta_description_fallback_template():
    d = seo.build_meta_description(
        {"titleRu": "Матрица", "releaseYear": 1999, "type": "movie", "genres": ["Фантастика", "Боевик"]}
    )
    assert "фильм «Матрица» (1999)" in d and "фантастика, боевик" in d


def test_set_seo_meta_only_fills_empty_via_coalesce():
    cur = FakeCur({})
    rec = {"titleRu": "Матрица", "releaseYear": 1999, "description": "Хакер узнаёт правду."}
    seo.set_seo_meta(cur, 5, rec, seo.SeoSupport(content=True, version=True))
    sql, params = cur.executed[0]
    assert "COALESCE(NULLIF(meta_title" in sql
    assert params[0].startswith("Матрица (1999)") and params[1] == "Хакер узнаёт правду." and params[2] == 5


def test_set_seo_image_skipped_without_support():
    cur = FakeCur({})
    seo.set_seo_image(cur, 1, 9, seo.SeoSupport(content=True, version=False))
    assert cur.executed == []
    seo.set_seo_image(cur, 1, 9, seo.SeoSupport(content=True, version=True))
    assert len(cur.executed) == 2
