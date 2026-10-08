"""
Команда sync-schedule: расписание выхода серий из AniList → Postgres.

Что делает (тайтл находится по content.shikimori_id = idMal в AniList):
  1. episodes.airing_at — дата эфира каждой серии (Unix-секунды, как поле airingAt
     в CMS; сайт уже умеет её показывать).
  2. content.next_episode_number / next_episode_at — ближайшая невышедшая серия
     (для бейджа «Серия 15 — сегодня в 18:26» и страницы расписания). Колонки
     добавляет CMS (cms/ContentScheduleFields.ts); если их нет, этот шаг
     пропускается, а серии всё равно обновляются.

Правила записи:
  - серии НЕ создаются (без ссылки на плеер это были бы пустые серии на сайте);
  - дата серии заполняется, если её не было; пока серия не вышла, дата следует за
    AniList (эфир переносят); уже вышедшие серии с датой не перезаписываются
    (--overwrite — перезаписать всё);
  - если у content несколько строк сезонов (многосезонный сериал или дубли), серии
    не трогаются: по одному расписанию нельзя понять, какому сезону оно относится;
  - время — это ЭФИР в Японии; озвучка на сайте появляется позже.

Кого проверять:
  - онгоинги (release_status = ongoing) — при каждом запуске;
  - остальные — один раз (запоминается в файле состояния), дальше только с --all;
  - исключение: у уже проверенного тайтла появились серии без даты (их создал
    sync-dubs уже ПОСЛЕ проверки) — он проверяется повторно не чаще, чем раз в
    --recheck-days дней. Состояние помнит, сколько серий осталось без даты после
    прошлой проверки, поэтому тайтл, которого нет в расписании AniList, не
    опрашивается бесконечно.

Работа кусками по 50 тайтлов: сеть → короткая транзакция → сохранение состояния.
Сбой на середине не теряет уже записанное.

Запуск: python pipeline.py sync-schedule [--only-ongoing] [--all] [--overwrite]
        [--mal-id N] [--limit N] [--delay 0.8] [--recheck-days 3] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field

from . import anilist
from .config import database_url
from .db import table_columns, transaction, try_advisory_lock
from .revalidate import notify_frontend
from .status import ONGOING, detect_status_support

DEFAULT_STATE = "data/schedule-state.json"
CHUNK_SIZE = 50
_DIGITS = re.compile(r"^[0-9]+$")

# Серии тайтла без даты эфира (airing_at пусто или 0).
MISSING_SQL = (
    "SELECT count(*) FROM episodes e JOIN seasons s ON s.id = e.season_id "
    "WHERE s.content_id = content.id AND (e.airing_at IS NULL OR e.airing_at = 0)"
)


@dataclass
class Target:
    content_id: int
    mal_id: int
    label: str
    ongoing: bool
    missing: int = 0  # серий без даты эфира сейчас


@dataclass(frozen=True)
class ScheduleSupport:
    number: bool  # content.next_episode_number
    at: bool  # content.next_episode_at
    version_number: bool  # _content_v.version_next_episode_number
    version_at: bool  # _content_v.version_next_episode_at

    @property
    def content(self) -> bool:
        return self.number and self.at

    @property
    def version(self) -> bool:
        return self.version_number and self.version_at


def detect_schedule_support(cur) -> ScheduleSupport:
    content_cols = table_columns(cur, "content")
    version_cols = table_columns(cur, "_content_v")
    return ScheduleSupport(
        number="next_episode_number" in content_cols,
        at="next_episode_at" in content_cols,
        version_number="version_next_episode_number" in version_cols,
        version_at="version_next_episode_at" in version_cols,
    )


@dataclass
class Totals:
    targets: int = 0
    found: int = 0
    not_found: list[str] = field(default_factory=list)
    episodes_updated: int = 0
    next_updated: int = 0
    skipped_multi_season: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ─── Состояние ───────────────────────────────────────────────────

def load_state(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ─── Выбор тайтлов ───────────────────────────────────────────────

def needs_recheck(entry: dict | None, missing: int, *, now_ts: int, recheck_days: float) -> bool:
    """Нужно ли заново проверить уже проверенный тайтл: у него есть серии без даты,
    их число изменилось с прошлой проверки (появились новые серии), и прошло
    достаточно времени. Тайтлы, которых нет в AniList, не перепроверяются."""
    if not entry or missing <= 0 or entry.get("found") is not True:
        return False
    if missing == entry.get("missing"):
        return False  # без изменений: AniList дат этих серий не знал и не знает
    checked_at = entry.get("checkedAt")
    if not isinstance(checked_at, (int, float)):
        return True
    return now_ts - checked_at >= recheck_days * 86400

def select_targets(
    cur,
    state: dict[str, dict],
    *,
    only_ongoing: bool = False,
    include_all: bool = False,
    mal_id: int | None = None,
    limit: int | None = None,
    now_ts: int | None = None,
    recheck_days: float = 3.0,
) -> list[Target]:
    now_ts = int(time.time()) if now_ts is None else now_ts
    support = detect_status_support(cur)
    ongoing_expr = f"{support.column} = %s" if support.content else "false"
    params: list = [ONGOING] if support.content else []
    cur.execute(
        f"SELECT id, shikimori_id, COALESCE(NULLIF(title_ru, ''), title_en, slug), {ongoing_expr}, "
        f"({MISSING_SQL}) "
        "FROM content WHERE type = 'series' AND shikimori_id IS NOT NULL ORDER BY id",
        params,
    )
    targets: list[Target] = []
    for content_id, shikimori_id, label, ongoing, missing in cur.fetchall():
        raw = str(shikimori_id).strip()
        if not _DIGITS.match(raw):
            continue
        mid = int(raw)
        if mal_id is not None:
            if mid != mal_id:
                continue
        elif ongoing:
            pass  # онгоинги — всегда
        elif only_ongoing:
            continue
        elif not include_all and raw in state:
            if not needs_recheck(state.get(raw), int(missing or 0), now_ts=now_ts, recheck_days=recheck_days):
                continue  # уже проверялся
        targets.append(Target(content_id, mid, str(label), bool(ongoing), int(missing or 0)))
        if limit and len(targets) >= limit:
            break
    return targets


# ─── Запись ──────────────────────────────────────────────────────

def count_missing(cur, content_id: int) -> int:
    """Сколько серий тайтла всё ещё без даты эфира (запоминается в состоянии)."""
    cur.execute(f"SELECT ({MISSING_SQL.replace('content.id', '%s')})", (content_id,))
    return int(cur.fetchone()[0] or 0)


def apply_episode_dates(
    cur, season_id: int, airing: dict[int, int], *, now_ts: int, overwrite: bool, has_updated_at: bool
) -> int:
    """Пишет airing_at серий сезона. Возвращает число изменённых строк."""
    if not airing:
        return 0
    values = ", ".join(["(%s::int, %s::bigint)"] * len(airing))
    flat: list[int] = []
    for number, at in sorted(airing.items()):
        flat += [number, at]
    touch = ", updated_at = now()" if has_updated_at else ""
    cur.execute(
        f"UPDATE episodes e SET airing_at = v.ts{touch} "
        f"FROM (VALUES {values}) AS v(num, ts) "
        "WHERE e.season_id = %s AND e.episode_number = v.num "
        "AND e.airing_at IS DISTINCT FROM v.ts "
        "AND (e.airing_at IS NULL OR e.airing_at = 0 OR e.airing_at > %s OR %s)",
        [*flat, season_id, now_ts, overwrite],
    )
    return cur.rowcount


def apply_next_episode(cur, content_id: int, next_episode: tuple[int, int] | None, support: ScheduleSupport) -> bool:
    """Пишет «следующую серию» в content (+ версии). None очищает поля. True, если изменилось."""
    if not support.content:
        return False
    number, at = next_episode if next_episode else (None, None)
    cur.execute(
        "UPDATE content SET next_episode_number = %s, next_episode_at = %s WHERE id = %s "
        "AND (next_episode_number IS DISTINCT FROM %s OR next_episode_at IS DISTINCT FROM %s)",
        (number, at, content_id, number, at),
    )
    changed = cur.rowcount > 0
    if support.version:
        cur.execute(
            "UPDATE _content_v SET version_next_episode_number = %s, version_next_episode_at = %s "
            "WHERE parent_id = %s",
            (number, at, content_id),
        )
    return changed


def apply_schedule(
    cur,
    target: Target,
    schedule: anilist.AnimeSchedule,
    support: ScheduleSupport,
    totals: Totals,
    *,
    now_ts: int,
    overwrite: bool,
    has_updated_at: bool,
) -> None:
    cur.execute("SELECT id FROM seasons WHERE content_id = %s", (target.content_id,))
    season_ids = [row[0] for row in cur.fetchall()]
    if len(season_ids) == 1:
        totals.episodes_updated += apply_episode_dates(
            cur, season_ids[0], schedule.airing,
            now_ts=now_ts, overwrite=overwrite, has_updated_at=has_updated_at,
        )
    elif schedule.airing:
        totals.skipped_multi_season.append(f"{target.label} (сезонов в БД: {len(season_ids)})")

    if apply_next_episode(cur, target.content_id, schedule.next_episode, support):
        totals.next_updated += 1


# ─── CLI ─────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    state = load_state(args.state)

    with transaction(database_url()) as conn:
        with conn.cursor() as cur:
            episode_cols = table_columns(cur, "episodes")
            if "airing_at" not in episode_cols:
                raise SystemExit(
                    "В таблице episodes нет колонки airing_at (поле airingAt в CMS) — "
                    "записывать даты некуда."
                )
            has_updated_at = "updated_at" in episode_cols
            targets = select_targets(
                cur, state,
                only_ongoing=args.only_ongoing, include_all=args.all,
                mal_id=args.mal_id, limit=args.limit, recheck_days=args.recheck_days,
            )
            support = detect_schedule_support(cur)

    totals = Totals(targets=len(targets))
    if not support.content:
        print(
            "Колонок content.next_episode_number / next_episode_at нет — «следующая серия» "
            "не сохраняется (добавьте поля в CMS: cms/ContentScheduleFields.ts)."
        )
    ongoing_count = sum(t.ongoing for t in targets)
    print(f"К проверке: {len(targets)} (онгоингов: {ongoing_count})")

    cache: dict[int, anilist.AnimeSchedule | None] = {}
    changed_any = False
    now_ts = int(time.time())

    for start in range(0, len(targets), CHUNK_SIZE):
        chunk = targets[start:start + CHUNK_SIZE]

        # 1. Сеть — до открытия транзакции.
        fetched: list[tuple[Target, anilist.AnimeSchedule | None]] = []
        for target in chunk:
            if target.mal_id not in cache:
                try:
                    cache[target.mal_id] = anilist.fetch_schedule(target.mal_id)
                except anilist.AniListError as exc:
                    totals.errors.append(f"{target.label} (MAL {target.mal_id}): {exc}")
                    print(f"  [ERR] {target.label}: {exc}")
                    continue
                if args.delay:
                    time.sleep(args.delay)
            fetched.append((target, cache[target.mal_id]))

        # 2. Запись куска одной транзакцией, каждый тайтл — в своём SAVEPOINT.
        checked_at = int(time.time())
        with transaction(database_url(), dry_run=args.dry_run) as conn:
            with conn.cursor() as cur:
                if not try_advisory_lock(cur):
                    raise SystemExit("Другая задача пайплайна уже пишет в БД — запуск пропущен.")
                for target, schedule in fetched:
                    state[str(target.mal_id)] = {"checkedAt": checked_at, "found": schedule is not None}
                    if schedule is None:
                        totals.not_found.append(f"{target.label} (MAL {target.mal_id})")
                        continue
                    totals.found += 1
                    before = (totals.episodes_updated, totals.next_updated)
                    cur.execute("SAVEPOINT sched")
                    try:
                        apply_schedule(
                            cur, target, schedule, support, totals,
                            now_ts=now_ts, overwrite=args.overwrite, has_updated_at=has_updated_at,
                        )
                    except Exception as exc:  # noqa: BLE001
                        cur.execute("ROLLBACK TO SAVEPOINT sched")
                        totals.errors.append(f"{target.label}: {exc}")
                        print(f"  [ERR] {target.label}: {exc}")
                        state.pop(str(target.mal_id), None)
                        continue
                    cur.execute("RELEASE SAVEPOINT sched")
                    state[str(target.mal_id)]["missing"] = count_missing(cur, target.content_id)
                    if (totals.episodes_updated, totals.next_updated) != before:
                        changed_any = True
                        print(f"  [OK] {target.label}: дат серий {totals.episodes_updated - before[0]}")

        if not args.dry_run:
            save_state(args.state, state)  # после коммита куска
        print(f"  … обработано {min(start + CHUNK_SIZE, len(targets))} из {len(targets)}")

    if not args.dry_run and changed_any:
        notify_frontend()

    prefix = "[DRY-RUN, изменения откатены, состояние не сохранено] " if args.dry_run else ""
    print(f"\n{prefix}--- Сводка sync-schedule ---")
    print(f"Тайтлов к проверке: {totals.targets}; найдено в AniList: {totals.found}")
    print(f"Дат серий записано: {totals.episodes_updated}")
    print(f"«Следующая серия» обновлена у тайтлов: {totals.next_updated}")
    if totals.not_found:
        shown = ", ".join(totals.not_found[:15])
        more = f" … и ещё {len(totals.not_found) - 15}" if len(totals.not_found) > 15 else ""
        print(f"Нет в AniList ({len(totals.not_found)}): {shown}{more}")
    if totals.skipped_multi_season:
        print(f"Пропущено (несколько сезонов в БД): {len(totals.skipped_multi_season)} — {', '.join(totals.skipped_multi_season[:10])}")
    if totals.errors:
        raise SystemExit(f"sync-schedule завершён с ошибками: {len(totals.errors)} (запустите ещё раз — состояние сохранено)")
