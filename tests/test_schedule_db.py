"""sync-schedule на реальном Postgres (TEST_DATABASE_URL)."""
import argparse
import json
import os

import psycopg2
import pytest

from kodik_pipeline import anilist, schedule

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL не задан")

NOW = 1_800_000_000  # «сейчас» в тестах


def sched(mal=100, airing=None, next_episode=None):
    return anilist.AnimeSchedule(anilist_id=mal + 1, mal_id=mal, status="RELEASING", episodes_total=12,
                                 title="T", next_episode=next_episode, airing=airing or {})


@pytest.fixture()
def cur(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("TRUNCATE episodes, seasons, _content_v_rels, _content_v, content_rels, content, genres RESTART IDENTITY CASCADE")
    # Колонки расписания в тестовой БД может не быть — добавляем и потом убираем.
    wanted = {"episodes": ["airing_at numeric"], "content": ["next_episode_number numeric", "next_episode_at numeric"],
              "_content_v": ["version_next_episode_number numeric", "version_next_episode_at numeric"]}
    added = []
    for table, cols in wanted.items():
        for col in cols:
            name = col.split()[0]
            c.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, name))
            if not c.fetchone():
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                added.append((table, name))
    yield c
    for table, name in added:
        c.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}")
    conn.close()


def make_title(c, mal=100, status="ongoing", seasons=1, episodes=(1, 2, 3), slug=None):
    c.execute("INSERT INTO content (type, title_en, title_ru, slug, shikimori_id, release_status, status, _status) "
              "VALUES ('series', %s, %s, %s, %s, %s, 'published', 'published') RETURNING id",
              (f"Show {mal}", f"Шоу {mal}", slug or f"show-{mal}", str(mal), status))
    content_id = c.fetchone()[0]
    c.execute("INSERT INTO _content_v (parent_id, latest) VALUES (%s, true)", (content_id,))
    season_ids = []
    for n in range(1, seasons + 1):
        c.execute("INSERT INTO seasons (content_id, season_number) VALUES (%s, %s) RETURNING id", (content_id, n))
        season_ids.append(c.fetchone()[0])
    for number in episodes:
        c.execute("INSERT INTO episodes (season_id, episode_number, title) VALUES (%s, %s, 'e')", (season_ids[0], number))
    return content_id, season_ids[0]


def dates(c, season_id):
    c.execute("SELECT episode_number, airing_at FROM episodes WHERE season_id = %s ORDER BY episode_number", (season_id,))
    return [(n, None if a is None else int(a)) for n, a in c.fetchall()]


def apply(c, content_id, schedule_obj, *, overwrite=False, totals=None, mal=100):
    totals = totals or schedule.Totals()
    target = schedule.Target(content_id, mal, f"Шоу {mal}", True)
    schedule.apply_schedule(c, target, schedule_obj, schedule.detect_schedule_support(c), totals,
                            now_ts=NOW, overwrite=overwrite, has_updated_at=True)
    return totals


def test_dates_are_written_and_second_run_changes_nothing(cur):
    cid, sid = make_title(cur)
    airing = {1: 1000, 2: 2000, 3: 2000}  # одинаковое время у двух серий — двойной эфир
    first = apply(cur, cid, sched(airing=airing))
    assert first.episodes_updated == 3 and dates(cur, sid) == [(1, 1000), (2, 2000), (3, 2000)]
    cur.execute("SELECT max(updated_at) FROM episodes"); stamp = cur.fetchone()[0]
    second = apply(cur, cid, sched(airing=airing))
    assert second.episodes_updated == 0
    cur.execute("SELECT max(updated_at) FROM episodes"); assert cur.fetchone()[0] == stamp  # строки не трогались


def test_aired_episodes_are_kept_but_upcoming_follow_anilist(cur):
    cid, sid = make_title(cur, episodes=(1, 2))
    cur.execute("UPDATE episodes SET airing_at = %s WHERE season_id = %s AND episode_number = 1", (500, sid))        # вышла давно
    cur.execute("UPDATE episodes SET airing_at = %s WHERE season_id = %s AND episode_number = 2", (NOW + 100, sid))  # ещё не вышла
    apply(cur, cid, sched(airing={1: 999, 2: NOW + 5000}))
    assert dates(cur, sid) == [(1, 500), (2, NOW + 5000)]  # прошлое не тронуто, эфир перенесён
    apply(cur, cid, sched(airing={1: 999}), overwrite=True)
    assert dates(cur, sid)[0] == (1, 999)


def test_episodes_missing_in_db_are_not_created(cur):
    cid, sid = make_title(cur, episodes=(1,))
    apply(cur, cid, sched(airing={1: 10, 2: 20, 3: 30}))
    assert dates(cur, sid) == [(1, 10)]


def test_multi_season_content_is_skipped_for_episodes(cur):
    cid, sid = make_title(cur, seasons=2)
    totals = apply(cur, cid, sched(airing={1: 10}, next_episode=(2, NOW + 1)))
    assert dates(cur, sid) == [(1, None), (2, None), (3, None)]
    assert len(totals.skipped_multi_season) == 1 and totals.next_updated == 1  # «следующая серия» всё равно пишется


def test_next_episode_is_written_cleared_and_versioned(cur):
    cid, _ = make_title(cur)
    apply(cur, cid, sched(next_episode=(4, NOW + 3600)))
    cur.execute("SELECT next_episode_number, next_episode_at FROM content WHERE id=%s", (cid,))
    assert tuple(int(x) for x in cur.fetchone()) == (4, NOW + 3600)
    cur.execute("SELECT version_next_episode_number FROM _content_v WHERE parent_id=%s", (cid,))
    assert int(cur.fetchone()[0]) == 4
    totals = apply(cur, cid, sched(next_episode=None))   # тайтл закончился
    cur.execute("SELECT next_episode_number, next_episode_at FROM content WHERE id=%s", (cid,))
    assert cur.fetchone() == (None, None) and totals.next_updated == 1


def test_works_without_next_episode_columns(cur):
    cid, sid = make_title(cur)
    cur.execute("ALTER TABLE content DROP COLUMN next_episode_number, DROP COLUMN next_episode_at")
    cur.execute("ALTER TABLE _content_v DROP COLUMN version_next_episode_number, DROP COLUMN version_next_episode_at")
    assert not schedule.detect_schedule_support(cur).content
    totals = apply(cur, cid, sched(airing={1: 10}, next_episode=(2, 99)))
    assert totals.episodes_updated == 1 and totals.next_updated == 0
    cur.execute("ALTER TABLE content ADD COLUMN next_episode_number numeric, ADD COLUMN next_episode_at numeric")
    cur.execute("ALTER TABLE _content_v ADD COLUMN version_next_episode_number numeric, ADD COLUMN version_next_episode_at numeric")


# ─── select_targets и сквозной запуск ───────────────────────────

def test_select_targets_rules(cur):
    make_title(cur, mal=1, status="ongoing")
    make_title(cur, mal=2, status="released")
    make_title(cur, mal=3, status="released")
    cur.execute("UPDATE content SET shikimori_id = 'z39587' WHERE shikimori_id = '3'")   # не число — пропускаем
    state = {"2": {"checkedAt": 1, "found": True}}
    ids = lambda **kw: sorted(t.mal_id for t in schedule.select_targets(cur, state, **kw))
    assert ids() == [1]                       # онгоинг — всегда; проверенный released — нет
    assert ids(include_all=True) == [1, 2]
    assert ids(only_ongoing=True) == [1]
    assert ids(mal_id=2) == [2]               # конкретный тайтл — игнорируя состояние
    assert ids(limit=1) == [1]


def run_args(tmp_path, **kw):
    base = dict(state=str(tmp_path / "state.json"), only_ongoing=False, all=False, overwrite=False,
                mal_id=None, limit=None, delay=0, dry_run=False)
    return argparse.Namespace(**{**base, **kw})


def test_run_end_to_end_state_and_dry_run(cur, monkeypatch, tmp_path):
    ong_c, ong_s = make_title(cur, mal=1, status="ongoing")
    old_c, old_s = make_title(cur, mal=2, status="released")
    gone_c, _ = make_title(cur, mal=3, status="released")
    calls = []

    def fake_fetch(mal_id):
        calls.append(mal_id)
        if mal_id == 3:
            return None  # нет в AniList
        return sched(mal=mal_id, airing={1: 100 * mal_id}, next_episode=(2, NOW + 1) if mal_id == 1 else None)

    monkeypatch.setattr(schedule.anilist, "fetch_schedule", fake_fetch)
    notified = []
    monkeypatch.setattr(schedule, "notify_frontend", lambda: notified.append(1) or True)

    # dry-run: ничего не пишет и не сохраняет состояние
    schedule.run(run_args(tmp_path, dry_run=True))
    assert dates(cur, ong_s)[0][1] is None and not (tmp_path / "state.json").exists() and not notified

    calls.clear()
    schedule.run(run_args(tmp_path))
    assert sorted(calls) == [1, 2, 3]
    assert dates(cur, ong_s)[0] == (1, 100) and dates(cur, old_s)[0] == (1, 200)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["3"]["found"] is False and state["2"]["found"] is True and notified == [1]

    calls.clear()
    schedule.run(run_args(tmp_path))            # второй запуск: проверенные released и «нет в AniList» не трогаем
    assert calls == [1]


def test_run_keeps_progress_when_anilist_fails(cur, monkeypatch, tmp_path):
    make_title(cur, mal=1, status="released")
    make_title(cur, mal=2, status="released")
    monkeypatch.setattr(schedule, "notify_frontend", lambda: True)

    def flaky(mal_id):
        if mal_id == 2:
            raise anilist.AniListError("boom")
        return sched(mal=mal_id, airing={1: 5})

    monkeypatch.setattr(schedule.anilist, "fetch_schedule", flaky)
    with pytest.raises(SystemExit):
        schedule.run(run_args(tmp_path))
    state = json.loads((tmp_path / "state.json").read_text())
    assert "1" in state and "2" not in state     # упавший тайтл будет повторён в следующий раз


def test_run_requires_airing_at_column(cur, monkeypatch, tmp_path):
    cur.execute("ALTER TABLE episodes DROP COLUMN airing_at")
    with pytest.raises(SystemExit, match="airing_at"):
        schedule.run(run_args(tmp_path))
    cur.execute("ALTER TABLE episodes ADD COLUMN airing_at numeric")


def test_episodes_created_by_sync_dubs_get_dates_too(cur):
    """Серия, которой не было у основной озвучки и которую создал sync-dubs, получает дату эфира."""
    from kodik_pipeline import sources

    cid, sid = make_title(cur, episodes=(1,))
    sources._insert_episode(cur, sid, 2, "//kodikplayer.com/seria/2/x")
    apply(cur, cid, sched(airing={1: 10, 2: 20}))
    assert dates(cur, sid) == [(1, 10), (2, 20)]
