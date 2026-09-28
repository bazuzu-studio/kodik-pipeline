from kodik_pipeline.api import normalize_item, fetch_normalized


def test_normalize_series_extracts_episode_links():
    item = {
        "id": "kodik-1",
        "title": "Show S1",
        "title_orig": "Show",
        "year": 2024,
        "type": "anime-serial",
        "shikimori_id": "42",
        "kinopoisk_id": "111",
        "seasons": {"1": {"episodes": {"10": "link10", "1": "link1"}}},
        "material_data": {
            "title": "Шоу",
            "title_en": "Show",
            "anime_genres": ["Экшен", "экшен"],
            "shikimori_rating": "8.31",
        },
    }

    result = normalize_item(item)
    assert result["type"] == "series"
    assert result["rating"] == 8.3
    assert result["genres"] == ["Экшен"]
    assert [e["number"] for e in result["seasons"][0]["episodes"]] == [1, 10]
    assert result["seasons"][0]["episodes"][0]["playerLink"] == "link1"


def test_fetch_normalized_groups_seasons(monkeypatch):
    items = [
        {
            "id": "s1", "title": "S1", "title_orig": "Show", "year": 2020,
            "type": "anime-serial", "kinopoisk_id": "1",
            "material_data": {"title": "Show", "title_en": "Show"},
            "seasons": {"1": {"episodes": {"1": "a"}}},
        },
        {
            "id": "s2", "title": "S2", "title_orig": "Show", "year": 2022,
            "type": "anime-serial", "kinopoisk_id": "1",
            "material_data": {"title": "Show", "title_en": "Show"},
            "seasons": {"1": {"episodes": {"1": "b"}}},
        },
    ]

    class Fake:
        def __iter__(self):
            return iter(items)

    monkeypatch.setattr("kodik_pipeline.api.iter_items", lambda **_: iter(items))
    result = fetch_normalized()
    assert [r["seasonNumber"] for r in result] == [1, 2]
    assert result[1]["titleEn"].endswith("S2")


def test_iter_items_follows_next_and_stops_on_empty(monkeypatch):
    import kodik_pipeline.api as api

    calls = []

    def fake_request(url):
        calls.append(url)
        if len(calls) == 1:
            return {"results": [{"id": "1"}], "next_page": "/list?second"}
        return {"results": [{"id": "2"}], "next": None}

    monkeypatch.setattr(api, "_request_url", fake_request)
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    result = list(api.iter_items(token="x", translation_id=609, limit=2, delay=0, max_pages=None))
    assert [item["id"] for item in result] == ["1", "2"]
    assert calls[1] == "https://kodik-api.com/list?second"


def test_iter_items_passes_extra_params(monkeypatch):
    import kodik_pipeline.api as api

    urls = []
    monkeypatch.setattr(api, "_request_url", lambda url: urls.append(url) or {"results": []})
    list(api.iter_items(token="x", translation_id=609, limit=5, delay=0,
                        extra_params={"anime_status": "ongoing", "skip": None}))
    assert "anime_status=ongoing" in urls[0]
    assert "skip" not in urls[0]


def test_iter_items_detects_pagination_loop(monkeypatch):
    import pytest
    import kodik_pipeline.api as api

    monkeypatch.setattr(api, "_request_url", lambda url: {"results": [{"id": url}], "next": url})
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="зацикленную"):
        list(api.iter_items(token="x", translation_id=609, limit=1, delay=0))


def _serial(kodik_id, status_field, status, **extra):
    md = {"title": kodik_id, "title_en": kodik_id, status_field: status}
    item = {"id": kodik_id, "type": "anime-serial", "material_data": md,
            "seasons": {"1": {"episodes": {"1": "//x/1"}}}}
    item.update(extra)
    return item


def test_normalize_item_normalizes_status():
    assert normalize_item(_serial("a", "anime_status", "ongoing"))["status"] == "ongoing"
    assert normalize_item(_serial("a", "all_status", "Released"))["status"] == "released"
    assert normalize_item(_serial("a", "anime_status", None))["status"] is None


def test_fetch_ongoing_filters_client_side_and_dedupes(monkeypatch):
    import kodik_pipeline.api as api

    def fake_iter(**kw):
        # «сервер» проигнорировал фильтр и вернул всё подряд
        yield _serial("a", "anime_status", "ongoing")
        yield _serial("b", "anime_status", "released")
        yield {"id": "m", "type": "anime", "material_data": {"anime_status": "ongoing"}, "link": "//m"}
        yield _serial("c", "all_status", "ongoing")

    monkeypatch.setattr(api, "iter_items", fake_iter)
    result = api.fetch_ongoing(token="x", translation_id=609)
    assert sorted(r["kodikId"] for r in result) == ["a", "c"]  # b — вышел, m — фильм


def test_fetch_by_kodik_id_returns_exact_match(monkeypatch):
    import kodik_pipeline.api as api

    monkeypatch.setattr(api, "_request_url", lambda url: {"results": [
        _serial("other", "anime_status", "ongoing"),
        _serial("want", "anime_status", "released"),
    ]})
    assert api.fetch_by_kodik_id("want", token="x")["status"] == "released"
    assert api.fetch_by_kodik_id("nope", token="x") is None
