"""Перепроверка уже проверенных тайтлов в sync-schedule (без БД)."""
import pipeline
from kodik_pipeline.schedule import needs_recheck

DAY = 86400
NOW = 1_800_000_000


def entry(**kw):
    base = {"checkedAt": NOW - 10 * DAY, "found": True, "missing": 0}
    base.update(kw)
    return base


def test_new_undated_episodes_trigger_recheck():
    # прошлый раз без даты оставалось 0, теперь 4 (sync-dubs создал серии)
    assert needs_recheck(entry(missing=0), 4, now_ts=NOW, recheck_days=3)


def test_unchanged_missing_is_not_rechecked_forever():
    # AniList этих серий не знал и не знает: число серий без даты не менялось
    assert not needs_recheck(entry(missing=4), 4, now_ts=NOW, recheck_days=3)


def test_no_missing_or_not_found_or_unknown():
    assert not needs_recheck(entry(), 0, now_ts=NOW, recheck_days=3)
    assert not needs_recheck(entry(found=False), 4, now_ts=NOW, recheck_days=3)
    assert not needs_recheck(None, 4, now_ts=NOW, recheck_days=3)


def test_not_more_often_than_recheck_days():
    fresh = entry(checkedAt=NOW - 1 * DAY)
    assert not needs_recheck(fresh, 4, now_ts=NOW, recheck_days=3)
    assert needs_recheck(fresh, 4, now_ts=NOW, recheck_days=0.5)


def test_old_state_without_missing_key_is_rechecked_once():
    old = {"checkedAt": NOW - 10 * DAY, "found": True}
    assert needs_recheck(old, 4, now_ts=NOW, recheck_days=3)


def test_cli_flag():
    args = pipeline.build_parser().parse_args(["sync-schedule", "--recheck-days", "1.5"])
    assert args.recheck_days == 1.5
    assert pipeline.build_parser().parse_args(["sync-schedule"]).recheck_days == 3.0
