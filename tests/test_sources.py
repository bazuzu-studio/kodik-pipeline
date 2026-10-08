"""episode_sources: сопоставление сезонов, запись ссылок по озвучкам (без БД, на фейковом курсоре)."""
import argparse

import pytest

from kodik_pipeline import sources


class FakeCursor:
    """Минимальная in-memory БД под запросы sources.py."""

    def __init__(self, seasons=None, episodes=None, content=None):
        self.content = content or {}          # shikimori_id -> content_id
        self.seasons = seasons or {}          # content_id -> [(season_id, number)]
        self.episodes = episodes or {}        # season_id -> {number: episode_id}
        self.episode_links = {}               # episode_id -> player_link
        self.touched = []                     # content_id, у которых обновлён updated_at
        self.next_episode_id = 9000
        self.sources = {}                     # (episode_id, voiceover_id) -> [id, link]
        self.next_id = 1
        self.result = []
        self.log = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.log.append(sql)
        if sql.startswith("SELECT shikimori_id, id FROM content"):
            self.result = list(self.content.items())
        elif sql.startswith("SELECT id, season_number FROM seasons"):
            self.result = list(self.seasons.get(params[0], []))
        elif sql.startswith("SELECT id, episode_number, player_link FROM episodes"):
            self.result = [
                (eid, n, self.episode_links.get(eid)) for n, eid in self.episodes.get(params[0], {}).items()
            ]
        elif sql.startswith("INSERT INTO episodes"):
            season_id, number, title, link = params
            assert title == f"Серия {number}"
            self.next_episode_id += 1
            self.episodes.setdefault(season_id, {})[number] = self.next_episode_id
            self.episode_links[self.next_episode_id] = link
            self.result = [(self.next_episode_id,)]
        elif sql.startswith("UPDATE episodes SET player_link"):
            link, episode_id = params
            self.episode_links[episode_id] = link
        elif sql.startswith("UPDATE content SET updated_at"):
            self.touched.append(params[1])
        elif sql.startswith("UPDATE _content_v"):
            pass
        elif sql.startswith("SELECT episode_id, id, player_link FROM episode_sources"):
            voiceover_id, ep_ids = params
            self.result = [
                (ep, row[0], row[1]) for (ep, vid), row in sorted(self.sources.items())
                if vid == voiceover_id and ep in ep_ids
            ]
        elif sql.startswith("INSERT INTO episode_sources"):
            ep, vid, link = params
            assert (ep, vid) not in self.sources, "дубль (episode_id, voiceover_id)"
            self.sources[(ep, vid)] = [self.next_id, link]
            self.next_id += 1
        elif sql.startswith("UPDATE episode_sources"):
            link, source_id = params
            for row in self.sources.values():
                if row[0] == source_id:
                    row[1] = link
        elif sql.startswith(("SAVEPOINT", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT")):
            pass
        else:
            raise AssertionError(f"неожиданный запрос: {sql}")

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0]


COLS = {"episode_id", "voiceover_id", "player_link", "created_at", "updated_at"}


def rec(shikimori_id, seasons):
    return {"shikimoriId": shikimori_id, "titleEn": f"T{shikimori_id}", "seasons": seasons}


def season(number, links):
    return {"seasonNumber": number, "episodes": [{"number": n, "playerLink": l} for n, l in links.items()]}


def db():
    return FakeCursor(
        content={"10": 100},
        seasons={100: [(1000, 1)]},
        episodes={1000: {1: 5001, 2: 5002}},
    )


# ─── выбор озвучек ────────────────────────────────────────────────

ROWS = [(1, "AniLibria", "anilibria", 609), (2, "AniDub", "anidub", 610), (3, "Нет id", "no-id", None)]


def test_targets_skip_voiceovers_without_kodik_id():
    assert [t.slug for t in sources.select_targets(ROWS)] == ["anilibria", "anidub"]


def test_targets_filter_by_slug_or_kodik_id():
    assert [t.slug for t in sources.select_targets(ROWS, ["ANIDUB"])] == ["anidub"]
    assert [t.slug for t in sources.select_targets(ROWS, ["609"])] == ["anilibria"]


def test_targets_unknown_filter_is_an_error():
    with pytest.raises(SystemExit):
        sources.select_targets(ROWS, ["anidub", "nope"])
    with pytest.raises(SystemExit):
        sources.select_targets(ROWS, ["no-id"])  # студия есть, но id Kodik не заполнен


# ─── сопоставление сезонов ────────────────────────────────────────

def test_pair_single_season_ignores_renumbering():
    # у Kodik сезон «1», в БД франшиза пересчитала его в «3»
    pairs = sources.pair_seasons([(7, 3)], [{"seasonNumber": 1}])
    assert [(sid, s["seasonNumber"]) for sid, s in pairs] == [(7, 1)]


def test_pair_equal_counts_pairs_by_order():
    pairs = sources.pair_seasons([(8, 3), (7, 2)], [{"seasonNumber": 2}, {"seasonNumber": 1}])
    assert [(sid, s["seasonNumber"]) for sid, s in pairs] == [(7, 1), (8, 2)]


@pytest.mark.parametrize("db_rows, rec_seasons", [
    ([(7, 1), (8, 2)], [{"seasonNumber": 1}]),   # у тайтла больше сезонов, чем у озвучки
    ([(7, 1)], [{"seasonNumber": 1}, {"seasonNumber": 2}]),
    ([], [{"seasonNumber": 1}]),
    ([(7, 1)], []),
])
def test_pair_ambiguous_returns_nothing(db_rows, rec_seasons):
    assert sources.pair_seasons(db_rows, rec_seasons) == []


# ─── запись ───────────────────────────────────────────────────────

def test_creates_sources_for_existing_episodes_only():
    cur, stats = db(), sources.SourceStats()
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {1: "//a/1", 2: "//a/2", 3: "//a/3"})]),
                        sources.load_content_index(cur), stats)
    assert (stats.created, stats.no_episode) == (2, 1)  # серии 3 в БД нет — не создаётся
    assert cur.sources == {(5001, 2): [1, "//a/1"], (5002, 2): [2, "//a/2"]}


def test_rerun_is_idempotent_and_updates_changed_links_only():
    cur = db()
    index = sources.load_content_index(cur)
    record = rec("10", [season(1, {1: "//a/1", 2: "//a/2"})])
    sources.sync_record(cur, COLS, 2, record, index, sources.SourceStats())

    again = sources.SourceStats()
    sources.sync_record(cur, COLS, 2, record, index, again)
    assert (again.created, again.updated, again.unchanged) == (0, 0, 2)

    changed = sources.SourceStats()
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {1: "//a/NEW", 2: "//a/2"})]), index, changed)
    assert (changed.created, changed.updated, changed.unchanged) == (0, 1, 1)
    assert cur.sources[(5001, 2)][1] == "//a/NEW"


def test_different_voiceovers_do_not_clash():
    cur = db()
    index = sources.load_content_index(cur)
    sources.sync_record(cur, COLS, 1, rec("10", [season(1, {1: "//x/1"})]), index, sources.SourceStats())
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {1: "//y/1"})]), index, sources.SourceStats())
    assert cur.sources[(5001, 1)][1] == "//x/1" and cur.sources[(5001, 2)][1] == "//y/1"


def test_unknown_title_and_ambiguous_seasons_are_counted_not_written():
    cur = db()
    index = sources.load_content_index(cur)
    stats = sources.SourceStats()
    sources.sync_record(cur, COLS, 2, rec("999", [season(1, {1: "//a/1"})]), index, stats)
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {1: "//a/1"}), season(2, {1: "//a/9"})]), index, stats)
    assert (stats.no_content, stats.ambiguous, stats.created) == (1, 1, 0)
    assert cur.sources == {}


def test_empty_links_are_ignored():
    cur, stats = db(), sources.SourceStats()
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {1: None, 2: ""})]), sources.load_content_index(cur), stats)
    assert cur.sources == {} and stats.created == 0


def test_timestamps_only_when_columns_exist():
    cur = db()
    sources.sync_record(cur, {"episode_id", "voiceover_id", "player_link"}, 2,
                        rec("10", [season(1, {1: "//a/1"})]), sources.load_content_index(cur), sources.SourceStats())
    insert = next(q for q in cur.log if q.startswith("INSERT"))
    assert "created_at" not in insert


def test_broken_record_does_not_stop_the_rest():
    cur = db()
    bad = {"shikimoriId": "10", "titleEn": "Bad", "seasons": [{"seasonNumber": 1, "episodes": "oops"}]}
    good = rec("10", [season(1, {1: "//a/1"})])
    failed = []
    stats = sources.write_records(cur, COLS, 2, [bad, good], failed)
    assert len(failed) == 1 and "Bad" in failed[0]
    assert stats.created == 1


def test_missing_table_is_reported(monkeypatch):
    monkeypatch.setattr(sources, "table_columns", lambda cur, name: set())
    with pytest.raises(SystemExit, match="EpisodeSources"):
        sources.require_sources_table(object())


def test_cli_parses_sync_dubs():
    import pipeline
    args = pipeline.build_parser().parse_args(["sync-dubs", "--only", "anidub", "610", "--ongoing-only", "--dry-run"])
    assert args.only == ["anidub", "610"] and args.ongoing_only and args.dry_run
    assert args.func is pipeline.sync_dubs_command


# ─── список серий по всем озвучкам ────────────────────────────────

def test_missing_episodes_from_other_voiceover_are_created():
    cur = db()
    cur.episode_links = {5001: "//main/1", 5002: "//main/2"}
    stats = sources.SourceStats()
    sources.sync_record(
        cur, COLS, 2, rec("10", [season(1, {1: "//a/1", 2: "//a/2", 3: "//a/3", 4: "//a/4"})]),
        sources.load_content_index(cur), stats, create_episodes=True,
    )
    assert stats.episodes_created == 2 and stats.created == 4
    new_numbers = {n: eid for n, eid in cur.episodes[1000].items() if n in (3, 4)}
    assert [cur.episode_links[new_numbers[n]] for n in (3, 4)] == ["//a/3", "//a/4"]  # ссылка первой озвучки
    assert cur.episode_links[5001] == "//main/1"  # существующая ссылка по умолчанию не тронута
    assert (new_numbers[3], 2) in cur.sources and (new_numbers[4], 2) in cur.sources
    assert cur.touched == [100]  # тайтл помечен обновлённым


def test_second_voiceover_does_not_duplicate_created_episode():
    cur = db()
    index = sources.load_content_index(cur)
    for vid, link in ((1, "//x/3"), (2, "//y/3")):
        sources.sync_record(cur, COLS, vid, rec("10", [season(1, {3: link})]), index,
                            sources.SourceStats(), create_episodes=True)
    assert len([n for n in cur.episodes[1000] if n == 3]) == 1
    third = cur.episodes[1000][3]
    assert cur.episode_links[third] == "//x/3"  # ссылка по умолчанию — у первой озвучки
    assert cur.sources[(third, 1)][1] == "//x/3" and cur.sources[(third, 2)][1] == "//y/3"


def test_far_episode_numbers_are_not_created():
    cur, stats = db(), sources.SourceStats()  # в сезоне серии 1–2, лимит 2 + 50
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {3: "//a/3", 1100: "//a/1100"})]),
                        sources.load_content_index(cur), stats, create_episodes=True)
    assert (stats.episodes_created, stats.far_episodes) == (1, 1)
    assert 1100 not in cur.episodes[1000]


def test_episode_without_link_gets_link_from_voiceover():
    cur, stats = db(), sources.SourceStats()  # у серий 5001/5002 ссылок нет
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {1: "//a/1"})]),
                        sources.load_content_index(cur), stats, create_episodes=True)
    assert stats.episodes_linked == 1 and cur.episode_links[5001] == "//a/1"
    assert cur.touched == [100]


def test_creation_can_be_disabled():
    cur, stats = db(), sources.SourceStats()
    sources.sync_record(cur, COLS, 2, rec("10", [season(1, {3: "//a/3"})]),
                        sources.load_content_index(cur), stats)  # create_episodes=False по умолчанию
    assert (stats.episodes_created, stats.no_episode) == (0, 1) and 3 not in cur.episodes[1000]
    assert cur.touched == []


def test_cli_has_episode_creation_flags():
    import pipeline
    args = pipeline.build_parser().parse_args(["sync-dubs"])
    assert args.no_create_episodes is False and args.max_ahead == 50
    args = pipeline.build_parser().parse_args(["sync-dubs", "--no-create-episodes", "--max-ahead", "5"])
    assert args.no_create_episodes is True and args.max_ahead == 5
