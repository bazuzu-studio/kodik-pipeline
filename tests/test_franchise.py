"""Нумерация сезонов и частей франшизы — без БД."""
from kodik_pipeline import api
from kodik_pipeline.franchise import detect_part, detect_season_marker, title_suffix


def item(kodik_id, year, shiki, title, seasons=None, kp="1"):
    return {
        "id": kodik_id, "type": "anime-serial", "title": title, "title_orig": title,
        "year": year, "kinopoisk_id": kp, "shikimori_id": shiki,
        "material_data": {"title": title, "title_en": title},
        "seasons": seasons or {"1": {"episodes": {"1": f"//x/{kodik_id}/1"}}},
    }


def run(monkeypatch, items):
    monkeypatch.setattr(api, "iter_items", lambda **_: iter(items))
    return {r["kodikId"]: r for r in api.fetch_normalized()}


def test_detect_part_and_marker():
    assert detect_part("Атака титанов. Часть 2") == 2
    assert detect_part("Show 2 часть") == 2
    assert detect_part("Show Part 3") == 3
    assert detect_part("Show") is None
    assert detect_season_marker("Show 2nd Season") == 2
    assert detect_season_marker("Шоу 3 сезон") == 3
    assert detect_season_marker("Show") is None


def test_parts_share_season_number(monkeypatch):
    recs = run(monkeypatch, [
        item("a", 2013, "1", "Show"),
        item("b", 2017, "2", "Show Season 2"),
        item("c", 2018, "3", "Show Season 3"),
        item("d", 2019, "4", "Show Season 3 Part 2"),
        item("e", 2020, "5", "Show Season 4"),
    ])
    assert [recs[k]["seasonNumber"] for k in "abcde"] == [1, 2, 3, 3, 4]
    assert recs["d"]["seasonPart"] == 2 and recs["c"]["seasonPart"] == 1
    assert recs["d"]["titleEn"].endswith("S3 Part 2") and recs["d"]["slug"].endswith("-s3-p2")
    assert detect_part(recs["d"]["seasons"][0]["title"]) == 2
    assert detect_part(recs["c"]["seasons"][0]["title"]) == 1  # «Часть 1» дописана, раз есть часть 2
    assert len({r["franchiseId"] for r in recs.values()}) == 1


def test_part_with_different_season_marker_is_a_new_season(monkeypatch):
    recs = run(monkeypatch, [
        item("a", 2020, "1", "Show Season 1"),
        item("b", 2022, "2", "Show Season 2 Part 2"),
    ])
    assert [recs[k]["seasonNumber"] for k in "ab"] == [1, 2]


def test_shikimori_ids_sorted_numerically(monkeypatch):
    # одинаковый год: «9999» должен идти раньше «10000» (раньше сравнивалось как строка)
    recs = run(monkeypatch, [
        item("late", 2020, "10000", "Show B"),
        item("early", 2020, "9999", "Show A"),
    ])
    assert recs["early"]["seasonNumber"] == 1 and recs["late"]["seasonNumber"] == 2


def test_same_shikimori_id_is_one_season(monkeypatch):
    recs = run(monkeypatch, [
        item("a", 2020, "7", "Show"),
        item("a-copy", 2020, "7", "Show", seasons={"1": {"episodes": {"1": "x", "2": "y"}}}),
        item("b", 2022, "8", "Show 2"),
    ])
    assert set(recs) == {"a-copy", "b"}               # дубль пропущен, остался с большим числом серий
    assert recs["a-copy"]["duplicateKodikIds"] == ["a"]
    assert [recs[k]["seasonNumber"] for k in ("a-copy", "b")] == [1, 2]


def test_multi_season_record_keeps_real_numbers_and_zero(monkeypatch):
    seasons = {n: {"episodes": {"1": f"//x/{n}"}} for n in ("0", "1", "2")}
    recs = run(monkeypatch, [item("a", 2010, "1", "Show", seasons=seasons, kp=None)])
    # раньше первому сезону («0») принудительно ставилась «1» и он сливался с настоящим первым
    assert [s["seasonNumber"] for s in recs["a"]["seasons"]] == [0, 1, 2]


def test_multi_season_record_continues_franchise_numbering(monkeypatch):
    seasons = {n: {"episodes": {"1": f"//x/{n}"}} for n in ("1", "2")}
    recs = run(monkeypatch, [
        item("a", 2010, "1", "Show", seasons=seasons),
        item("b", 2024, "2", "Show Returns"),
    ])
    assert [s["seasonNumber"] for s in recs["a"]["seasons"]] == [1, 2]
    assert recs["b"]["seasonNumber"] == 3  # а не второй «Сезон 2»


def test_title_suffix_first_season_is_clean():
    assert title_suffix({"seasonNumber": 1, "seasonPart": None}) == ""
    assert title_suffix({"seasonNumber": 1, "seasonPart": 2}) == " S1 Part 2"


def test_part_without_season_marker_joins_previous_season(monkeypatch):
    """Re:Zero: «2 сезон. Часть 1» (2020) и «2. Часть 2» (2021) — один сезон; дальше нумерация не съезжает."""
    recs = run(monkeypatch, [
        item("s1", 2016, "1", "Re:Zero"),
        item("s2a", 2020, "2", "Re:Zero 2 сезон. Часть 1"),
        item("s2b", 2021, "3", "Re:Zero 2. Часть 2"),
        item("s3", 2024, "4", "Re:Zero 3"),
        item("s4", 2026, "5", "Re:Zero 4"),
    ])
    assert [recs[k]["seasonNumber"] for k in ("s1", "s2a", "s2b", "s3", "s4")] == [1, 2, 2, 3, 4]
    assert [recs[k]["seasonPart"] for k in ("s2a", "s2b")] == [1, 2]


def test_season_marker_on_the_part_itself_must_match(monkeypatch):
    recs = run(monkeypatch, [item("a", 2020, "1", "Show"), item("b", 2022, "2", "Show Season 2 Part 2")])
    assert [recs[k]["seasonNumber"] for k in "ab"] == [1, 2]
