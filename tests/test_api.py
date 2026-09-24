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
    pages = {
        "https://kodik-api.com/list?first": {"results": [{"id": "1"}], "next": "/list?second"},
        "https://kodik-api.com/list?second": {"results": [{"id": "2"}], "next": None},
    }
    monkeypatch.setattr("kodik_pipeline.api._request_url", lambda url: pages[url])
    monkeypatch.setattr("kodik_pipeline.api.time.sleep", lambda _: None)
    result = list(__import__("kodik_pipeline.api", fromlist=["iter_items"]).iter_items(
        token="x", translation_id=609, limit=2, delay=0, max_pages=None
    ))
    assert [item["id"] for item in result] == ["1", "2"]
