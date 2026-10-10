"""sync-dubs: даты появления, title_dubs, лимит по episodes_aired, режим --by-title (без БД)."""
from kodik_pipeline import sources
from kodik_pipeline.sources import Features

from tests.test_sources import COLS, FakeCursor, db, rec, season

NEW_COLS = COLS | {"first_seen_at"}
ALL = Features(first_seen=True, episode_dates=True, title_dubs=True)


class FeatureCursor(FakeCursor):
    """FakeCursor + запросы новой схемы."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.title_dubs = {}      # (content_id, voiceover_id) -> (kodik_id, last, count, updated)
        self.stamped_sources = 0  # источников, вставленных с first_seen_at
        self.stamped_episodes = 0
        self.refreshed = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        if sql.startswith("UPDATE episodes e SET sources_count"):
            self.log.append(sql)
            self.refreshed.append(list(params[0]))
        elif sql.startswith("SELECT (kodik_id = %s"):
            self.log.append(sql)
            kodik_id, last, count, updated, content_id, voiceover_id = params
            row = self.title_dubs.get((content_id, voiceover_id))
            self.result = [(row == (kodik_id, last, count, updated),)] if row else []
        elif sql.startswith("INSERT INTO title_dubs"):
            self.log.append(sql)
            content_id, voiceover_id, kodik_id, last, count, updated = params
            self.title_dubs[(content_id, voiceover_id)] = (kodik_id, last, count, updated)
        else:
            if sql.startswith("INSERT INTO episode_sources") and "first_seen_at" in sql:
                self.stamped_sources += 1
            if sql.startswith("INSERT INTO episodes") and "first_available_at" in sql:
                self.stamped_episodes += 1
            super().execute(sql, params)

    def fetchone(self):
        return self.result[0] if self.result else None


def ongoing(shikimori_id, seasons, **extra):
    return {**rec(shikimori_id, seasons), "status": "ongoing", "kodikId": "k1", "updatedAt": "2026-10-10T10:00:00Z", **extra}


def run(cur, record, voiceover_id=2, features=ALL, **kwargs):
    stats = sources.SourceStats()
    sources.sync_record(cur, NEW_COLS, voiceover_id, record, sources.load_content_index(cur), stats,
                        features=features, **kwargs)
    return stats


def test_new_ongoing_source_gets_first_seen_and_touches_title():
    cur = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001}})
    run(cur, ongoing("10", [season(1, {1: "//a/1"})]))
    assert cur.stamped_sources == 1 and cur.touched == [100]
    assert cur.refreshed == [[5001]]  # sources_count / first_available_at пересчитаны


def test_backfill_and_released_titles_are_not_stamped():
    cur = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001}})
    run(cur, ongoing("10", [season(1, {1: "//a/1"})]), features=Features(first_seen=True, episode_dates=True, backfill=True))
    cur2 = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001}})
    run(cur2, ongoing("10", [season(1, {1: "//a/1"})], status="released"))
    assert cur.stamped_sources == 0 and cur2.stamped_sources == 0
    assert cur.touched == [] and cur2.touched == []


def test_new_episode_is_stamped_only_for_stamped_records():
    cur = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001}})
    run(cur, ongoing("10", [season(1, {1: "//a/1", 2: "//a/2"})]), create_episodes=True)
    assert cur.stamped_episodes == 1
    cur2 = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001}})
    run(cur2, ongoing("10", [season(1, {1: "//a/1", 2: "//a/2"})], status="released"), create_episodes=True)
    assert cur2.stamped_episodes == 0


def test_unchanged_dub_is_skipped_until_full():
    cur = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001}})
    record = ongoing("10", [season(1, {1: "//a/1"})])
    first = run(cur, record)
    assert first.created == 1 and (100, 2) in cur.title_dubs
    queries_before = len(cur.log)
    again = run(cur, record)
    assert again.unchanged_dubs == 1 and again.created == 0
    assert not any("episode_sources" in q for q in cur.log[queries_before:])  # серии не разбирались
    forced = run(cur, record, features=Features(first_seen=True, episode_dates=True, title_dubs=True, full=True))
    assert forced.unchanged_dubs == 0 and forced.unchanged == 1


def test_new_episode_of_known_dub_is_not_skipped():
    cur = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001, 2: 5002}})
    run(cur, ongoing("10", [season(1, {1: "//a/1"})]))
    stats = run(cur, ongoing("10", [season(1, {1: "//a/1", 2: "//a/2"})], updatedAt="2026-10-10T11:00:00Z"))
    assert stats.created == 1 and stats.unchanged_dubs == 0


def test_features_off_keeps_old_behaviour():
    cur = db()
    stats = sources.SourceStats()
    sources.sync_record(cur, COLS, 2, ongoing("10", [season(1, {1: "//a/1"})]),
                        sources.load_content_index(cur), stats)
    assert stats.created == 1 and cur.touched == []


def test_ongoing_episode_number_is_capped_by_episodes_aired():
    cur = FeatureCursor(content={"10": 100}, seasons={100: [(1000, 1)]}, episodes={1000: {1: 5001, 2: 5002}})
    links = {3: "//a/3", 4: "//a/4", 5: "//a/5", 6: "//a/6"}
    stats = run(cur, ongoing("10", [season(1, links)], episodesAired=2), features=sources.NO_FEATURES, create_episodes=True)
    assert stats.episodes_created == 3 and stats.far_episodes == 1  # 3, 4, 5 (= 2 + 3); 6 — дальше


def test_cap_ignores_multi_season_records_and_finished_titles():
    assert sources.aired_cap({"status": "ongoing", "episodesAired": 5, "seasons": [{}]}) == 5 + sources.AIRED_SLACK
    assert sources.aired_cap({"status": "ongoing", "episodesAired": 5, "seasons": [{}, {}]}) is None
    assert sources.aired_cap({"status": "released", "episodesAired": 5, "seasons": [{}]}) is None
    assert sources.aired_cap({"status": "ongoing", "episodesAired": None, "seasons": [{}]}) is None


def test_dub_summary_counts_only_episodes_with_links():
    record = rec("10", [season(1, {1: "//a/1", 2: None, 7: "//a/7"})])
    record["kodikId"] = "k9"
    assert sources.dub_summary(record) == ("k9", 7, 2)


def test_pick_dub_records_filters_orders_and_prefers_larger_record():
    targets = [sources.Target(1, "AniLibria", "anilibria", 609), sources.Target(2, "AniDub", "anidub", 610)]

    def item(translation_id, episodes):
        return {**rec("10", [season(1, {n: f"//x/{n}" for n in range(1, episodes + 1)})]), "translation": {"id": translation_id}}

    picked = sources.pick_dub_records([item(610, 2), item(999, 9), item(609, 1), item(610, 5)], targets)
    assert [(t.slug, sources.dub_summary(r)[2]) for t, r in picked] == [("anilibria", 1), ("anidub", 5)]


def test_cli_has_by_title_and_full_flags():
    import pipeline
    args = pipeline.build_parser().parse_args(["sync-dubs", "--by-title", "--full", "--max-titles", "5"])
    assert args.by_title and args.full and args.max_titles == 5
    args = pipeline.build_parser().parse_args(["sync-dubs"])
    assert not args.by_title and not args.full and args.max_titles is None


def test_fetch_by_shikimori_id_returns_series_only_without_duplicates(monkeypatch):
    from kodik_pipeline import api

    items = [
        {"id": "a", "type": "anime-serial", "shikimori_id": "10", "translation": {"id": 609}, "seasons": {"1": {"episodes": {"1": "//x/1"}}}},
        {"id": "a", "type": "anime-serial", "shikimori_id": "10", "translation": {"id": 609}, "seasons": {"1": {"episodes": {"1": "//x/1"}}}},
        {"id": "b", "type": "anime-serial", "shikimori_id": "10", "translation": {"id": 610}, "seasons": {"1": {"episodes": {"1": "//y/1"}}}},
        {"id": "c", "type": "anime", "shikimori_id": "10", "translation": {"id": 609}, "link": "//m"},
    ]
    seen = {}

    def fake_request(url, timeout=None, retries=None):
        seen["url"] = url
        return {"results": items}

    monkeypatch.setattr(api, "_request_url", fake_request)
    records = api.fetch_by_shikimori_id("10", token="t")
    assert [r["kodikId"] for r in records] == ["a", "b"]
    assert "shikimori_id=10" in seen["url"] and "with_episodes=true" in seen["url"]


def test_update_ongoing_stamps_new_episodes_only_when_column_exists():
    from kodik_pipeline import ongoing

    class Cur:
        def __init__(self):
            self.inserts = []

        def execute(self, sql, params=None):
            sql = " ".join(sql.split())
            if sql.startswith("INSERT INTO episodes"):
                self.inserts.append(sql)

        def fetchall(self):
            return [(1, 1, "//a/1")]

    for stamp in (True, False):
        cur = Cur()
        ongoing.sync_episodes(cur, 7, [{"number": 1, "playerLink": "//a/1"}, {"number": 2, "playerLink": "//a/2"}], stamp=stamp)
        assert len(cur.inserts) == 1 and ("first_available_at" in cur.inserts[0]) is stamp
