"""
Источники серий: ссылка на плеер по каждой озвучке (таблица `episode_sources`).

Зачем отдельная таблица, а не `episodes.voiceover_id`
  Серия остаётся одной строкой на (season_id, episode_number) — на этот ключ
  опираются load, update-ongoing и fix-seasons, а сайт читает episodes.player_link.
  Другие озвучки добавляются отдельными строками (episode_id, voiceover_id,
  player_link): существующий код и сайт не ломаются, дублей серий нет.

Откуда берутся данные
  Для каждой озвучки справочника с заполненным kodik_translation_id (см.
  match-voiceovers) идёт запрос Kodik /list?translation_id=<id>. Записи других
  озвучек имеют другой Kodik ID, поэтому тайтл ищется по shikimori_id — тому же
  ключу, по которому load отличает сезоны друг от друга.

Список серий — по всем озвучкам, а не только по основной
  Каталог строится по основной озвучке (KODIK_TRANSLATION_ID), а у неё серий
  может быть меньше, чем у других студий. Поэтому серия, которой нет в БД, но
  которая есть у другой озвучки, СОЗДАЁТСЯ (title «Серия N»; player_link — ссылка
  первой озвучки, у которой она есть; основная озвучка обрабатывается первой, так
  что при её серии именно она станет ссылкой по умолчанию). Защита от мусора:
  номер не может уходить дальше чем на --max-ahead (50) серий за самую
  большую уже существующую, а у существующей серии без ссылки она проставляется.
  Отключается флагом --no-create-episodes.

Принципы (как в voiceovers.py: осторожно и без потерь)
  - создаются только серии существующих сезонов и строки episode_sources: нет
    тайтла или сезона в БД — запись пропускается и попадает в счётчики (тайтлы
    и сезоны создаёт `sync`, а `update-ongoing` — сезоны онгоингов);
  - сезоны сопоставляются только когда это однозначно: число сезонов записи
    Kodik равно числу сезонов тайтла в БД (номера сверяются по порядку, т.к.
    нумерация франшизы сдвигает «сырые» номера Kodik). Иначе — пропуск со
    счётчиком «неоднозначно», а не ссылка не на ту серию;
  - ничего не удаляется: пропавшая у Kodik серия озвучки остаётся в БД;
  - существующая ссылка меняется только если она реально изменилась;
  - повторный запуск безопасен; --dry-run откатывает запись;
  - каждая озвучка пишется отдельной транзакцией (сеть не держит блокировку),
    каждая запись — в своём SAVEPOINT;
  - таблицу создаёт CMS (cms/EpisodeSources.ts + миграция Payload), пайплайн её
    не создаёт.

Новая схема CMS (миграция 20261010_120000; без неё всё ниже тихо отключено)
  - episode_sources.first_seen_at — когда пайплайн впервые увидел ссылку озвучки. Ставится
    только новым ссылкам онгоингов и не при первичной загрузке озвучки (пока у неё нет ни
    одного источника): у вышедших тайтлов реальное время неизвестно, и «сейчас» было бы
    неправдой. Из него считается скорость озвучек: first_seen_at − episodes.airing_at.
  - episodes.first_available_at / sources_count — когда серия впервые появилась на сайте
    (у любой озвучки) и сколько озвучек у неё есть.
  - title_dubs — сводка «тайтл ↔ озвучка» (последняя серия, число серий, updated_at Kodik).
    Если ничего не изменилось, озвучка тайтла пропускается без разбора серий; --full это
    отключает (нужен после fix-seasons, удаления серий или смены --no-create-episodes).
  - у онгоинга номер новой серии ограничен episodes_aired Kodik + 3 (кроме --max-ahead).

Режим --by-title
  Берёт онгоинги из БД и на каждый делает один запрос Kodik /search?shikimori_id=… — в ответе
  сразу все озвучки тайтла. Вместо ~90 проходов каталога (озвучек × 2 фильтра статуса) —
  один запрос на онгоинг. Для частого запуска (каждые 10–15 минут).

Запуск:
  python pipeline.py sync-dubs --dry-run
  python pipeline.py sync-dubs --only anidub shiza-project
  python pipeline.py sync-dubs --ongoing-only      # быстрый режим для расписания
  python pipeline.py sync-dubs --by-title          # онгоинги по одному запросу на тайтл (быстрее)
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, replace
from typing import Any

from . import api, franchise
from .config import database_url, kodik_delay, kodik_token, kodik_translation_id
from .db import table_columns, transaction, try_advisory_lock
from .ongoing import touch_content
from .revalidate import notify_frontend
from .status import detect_status_support, is_ongoing

TABLE = "episode_sources"
REQUIRED_COLUMNS = {"episode_id", "voiceover_id", "player_link"}
DEFAULT_MAX_AHEAD = 50
AIRED_SLACK = 3  # у онгоинга серия не может быть дальше episodes_aired Kodik больше чем на столько
TITLE_DUBS = "title_dubs"
TITLE_CHUNK = 20  # тайтлов за одну транзакцию в режиме --by-title


@dataclass(frozen=True)
class Features:
    """Необязательные возможности новой схемы CMS. Без колонок/таблицы пайплайн
    работает как раньше (см. миграцию CMS 20261010_120000).

    first_seen    — episode_sources.first_seen_at (когда впервые увидели ссылку озвучки);
    episode_dates — episodes.first_available_at / sources_count (нужна и first_seen);
    title_dubs    — таблица title_dubs (сводка по озвучке тайтла, пропуск неизменившихся);
    backfill      — у озвучки ещё нет ни одного источника: это первичная загрузка, реальное
                    время появления ссылок неизвестно, поэтому first_seen_at остаётся пустым;
    full          — не пропускать озвучки, у которых по title_dubs ничего не изменилось.
    """

    first_seen: bool = False
    episode_dates: bool = False
    title_dubs: bool = False
    backfill: bool = False
    full: bool = False


NO_FEATURES = Features()


def detect_features(cur, source_columns: set[str], *, full: bool = False) -> Features:
    episode_columns = table_columns(cur, "episodes")
    first_seen = "first_seen_at" in source_columns
    return Features(
        first_seen=first_seen,
        episode_dates=first_seen and {"first_available_at", "sources_count"} <= episode_columns,
        title_dubs={"content_id", "voiceover_id", "kodik_id", "last_episode", "episodes_count", "kodik_updated_at"}
        <= table_columns(cur, TITLE_DUBS),
        full=full,
    )


def voiceovers_with_sources(cur) -> set[int]:
    cur.execute(f"SELECT DISTINCT voiceover_id FROM {TABLE}")
    return {row[0] for row in cur.fetchall()}


def stamp_new(features: Features, rec: dict[str, Any]) -> bool:
    """Ставить ли «время появления» новым ссылкам записи. Только у онгоингов и не при
    первичной загрузке озвучки: у вышедших тайтлов ссылки появились давно, и «сейчас»
    исказило бы статистику скорости озвучек."""
    return features.first_seen and not features.backfill and is_ongoing(rec.get("status"))


@dataclass
class Target:
    voiceover_id: int
    title: str
    slug: str
    kodik_id: int


@dataclass
class SourceStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    no_content: int = 0   # тайтла с таким shikimori_id нет в БД
    ambiguous: int = 0    # сезоны записи нельзя однозначно сопоставить
    no_episode: int = 0   # серии с таким номером нет в БД (и создавать её нельзя)
    episodes_created: int = 0  # серии, созданные по ссылке другой озвучки
    episodes_linked: int = 0   # у существующей серии без ссылки проставлена ссылка
    far_episodes: int = 0      # номер слишком далеко от существующих — не создана
    unchanged_dubs: int = 0    # озвучка тайтла не менялась с прошлого запуска (title_dubs) — пропущена

    def changed(self) -> bool:
        return bool(self.created or self.updated or self.episodes_created or self.episodes_linked)

    def add(self, other: "SourceStats") -> None:
        for name in (
            "created", "updated", "unchanged", "no_content", "ambiguous", "no_episode",
            "episodes_created", "episodes_linked", "far_episodes", "unchanged_dubs",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))


# ─── Выбор озвучек ───────────────────────────────────────────────

def select_targets(rows: list[tuple[Any, ...]], only: list[str] | None = None) -> list[Target]:
    """rows — (id, title, slug, kodik_translation_id). Фильтр `only` — slug или id Kodik."""
    targets = [
        Target(int(row[0]), str(row[1]), str(row[2]), int(row[3]))
        for row in rows
        if row[3] is not None
    ]
    if not only:
        return targets

    wanted = {str(token).strip().casefold() for token in only}
    chosen = [t for t in targets if t.slug.casefold() in wanted or str(t.kodik_id) in wanted]
    known = {t.slug.casefold() for t in chosen} | {str(t.kodik_id) for t in chosen}
    unknown = sorted(token for token in wanted if token not in known)
    if unknown:
        raise SystemExit(
            "Не найдены озвучки (нет в справочнике или у них не заполнен kodik_translation_id): "
            + ", ".join(unknown)
        )
    return chosen


def load_targets(cur, only: list[str] | None = None) -> list[Target]:
    columns = table_columns(cur, "voiceovers")
    if not columns:
        raise SystemExit("Таблица «voiceovers» не найдена. Сначала выполните sync-voiceovers.")
    if "kodik_translation_id" not in columns:
        raise SystemExit("В таблице «voiceovers» нет колонки kodik_translation_id (обновите коллекцию Voiceovers в CMS).")
    cur.execute(
        "SELECT id, title, slug, kodik_translation_id FROM voiceovers "
        "WHERE kodik_translation_id IS NOT NULL ORDER BY id"
    )
    return select_targets(cur.fetchall(), only)


def require_sources_table(cur) -> set[str]:
    columns = table_columns(cur, TABLE)
    if not columns:
        raise SystemExit(
            f"Таблица «{TABLE}» не найдена. Добавьте коллекцию EpisodeSources в CMS "
            "(cms/EpisodeSources.ts), примените миграцию Payload и повторите команду."
        )
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise SystemExit(f"В таблице «{TABLE}» нет колонок: {', '.join(sorted(missing))}")
    return columns


# ─── Данные Kodik ────────────────────────────────────────────────

def fetch_records(
    token: str,
    translation_id: int,
    *,
    limit: int | None = None,
    delay: float | None = None,
    max_pages: int | None = None,
    ongoing_only: bool = False,
) -> list[dict[str, Any]]:
    """Сериалы Kodik для одной озвучки (с сериями), без дублей по shikimori_id."""
    found: dict[str, dict[str, Any]] = {}
    fields: tuple[str | None, ...] = api.ONGOING_FILTER_FIELDS if ongoing_only else (None,)
    for field in fields:
        extra: dict[str, Any] = {"types": "anime-serial"}
        if field:
            extra[field] = "ongoing"
        for item in api.iter_items(
            token=token, translation_id=translation_id, limit=limit,
            delay=delay, max_pages=max_pages, extra_params=extra,
        ):
            rec = api.normalize_item(item)
            if rec.get("type") != "series":
                continue
            if ongoing_only and not is_ongoing(rec.get("status")):
                continue
            key = str(rec.get("kodikId") or "")
            if key:
                found.setdefault(key, rec)

    kept, skipped = franchise.drop_duplicate_records(list(found.values()))
    for message in skipped:
        print(f"    [WARN] {message}")
    return kept


# ─── Запись в БД ─────────────────────────────────────────────────

def pair_seasons(
    db_rows: list[tuple[int, int]],
    rec_seasons: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """Сопоставляет сезоны записи Kodik со строками seasons: (season_id, сезон Kodik).

    Однозначно только при равном числе сезонов: тогда сезоны идут парами по
    порядку номеров (нумерация франшизы лишь сдвигает номера, порядок сохраняет).
    Иначе — пустой список («неоднозначно»).
    """
    wanted = sorted(
        (s for s in rec_seasons if s.get("seasonNumber") is not None),
        key=lambda s: s["seasonNumber"],
    )
    rows = sorted(db_rows, key=lambda r: (r[1] if r[1] is not None else -1, r[0]))
    if not wanted or len(wanted) != len(rows):
        return []
    return [(row[0], season) for row, season in zip(rows, wanted)]


def load_content_index(cur) -> dict[str, int]:
    """shikimori_id → content.id для сериалов (при дублях — самая старая запись)."""
    cur.execute(
        "SELECT shikimori_id, id FROM content "
        "WHERE type = 'series' AND shikimori_id IS NOT NULL ORDER BY id"
    )
    index: dict[str, int] = {}
    for shikimori_id, content_id in cur.fetchall():
        key = str(shikimori_id).strip()
        if key:
            index.setdefault(key, content_id)
    return index


def _insert_source(
    cur, columns: set[str], episode_id: int, voiceover_id: int, link: str, *, stamp: bool = False
) -> None:
    names = ["episode_id", "voiceover_id", "player_link"]
    placeholders = ["%s", "%s", "%s"]
    for ts in ("created_at", "updated_at"):
        if ts in columns:
            names.append(ts)
            placeholders.append("now()")
    if stamp and "first_seen_at" in columns:
        names.append("first_seen_at")
        placeholders.append("now()")
    cur.execute(
        f"INSERT INTO {TABLE} ({', '.join(names)}) VALUES ({', '.join(placeholders)})",
        [episode_id, voiceover_id, link],
    )


def _insert_episode(cur, season_id: int, number: Any, link: str, *, stamp: bool = False) -> int:
    """Новая серия (как load.insert_episode), но с возвратом id — он нужен для источника.
    stamp — записать в episodes.first_available_at время обнаружения серии."""
    extra_name = ", first_available_at" if stamp else ""
    extra_value = ", now()" if stamp else ""
    cur.execute(
        f"INSERT INTO episodes (season_id, episode_number, title, player_link{extra_name}) "
        f"VALUES (%s, %s, %s, %s{extra_value}) RETURNING id",
        (season_id, number, f"Серия {number}", link),
    )
    return cur.fetchone()[0]


def refresh_episode_stats(cur, episode_ids: list[int]) -> None:
    """Пересчитывает episodes.sources_count и first_available_at у серий, у которых
    добавились источники. first_available_at только уменьшается (LEAST игнорирует NULL),
    так что время, поставленное при создании серии, не затирается."""
    if not episode_ids:
        return
    cur.execute(
        "UPDATE episodes e SET sources_count = c.cnt, "
        "first_available_at = LEAST(e.first_available_at, c.first_seen) "
        f"FROM (SELECT episode_id, count(*) AS cnt, min(first_seen_at) AS first_seen FROM {TABLE} "
        "WHERE episode_id = ANY(%s) GROUP BY episode_id) c "
        "WHERE e.id = c.episode_id AND (e.sources_count IS DISTINCT FROM c.cnt "
        "OR e.first_available_at IS DISTINCT FROM LEAST(e.first_available_at, c.first_seen))",
        (episode_ids,),
    )


def aired_cap(rec: dict[str, Any]) -> int | None:
    """Верхняя граница номера серии у онгоинга: episodes_aired Kodik + небольшой запас
    (Kodik/Shikimori обновляют счётчик с задержкой). Только для записи из одного сезона:
    у многосезонной записи счётчик относится не к одному сезону. Иначе — None."""
    if not is_ongoing(rec.get("status")) or len(rec.get("seasons") or []) != 1:
        return None
    try:
        aired = int(rec.get("episodesAired") or 0)
    except (TypeError, ValueError):
        return None
    return aired + AIRED_SLACK if aired > 0 else None


def sync_season_sources(
    cur,
    columns: set[str],
    voiceover_id: int,
    season_id: int,
    season: dict[str, Any],
    stats: SourceStats,
    *,
    create_episodes: bool = False,
    max_ahead: int = DEFAULT_MAX_AHEAD,
    features: Features = NO_FEATURES,
    stamp: bool = False,
    cap: int | None = None,
) -> None:
    cur.execute(
        "SELECT id, episode_number, player_link FROM episodes WHERE season_id = %s",
        (season_id,),
    )
    episode_ids: dict[Any, int] = {}
    episode_links: dict[int, str | None] = {}
    for ep_id, number, old_link in cur.fetchall():
        if number is not None:
            episode_ids[number] = ep_id
            episode_links[ep_id] = old_link
    highest = max(episode_ids, default=0)
    ahead_limit = highest + max_ahead
    if cap is not None:
        ahead_limit = min(ahead_limit, max(highest, cap))

    wanted: list[tuple[int, str]] = []
    for ep in sorted(season.get("episodes") or [], key=lambda e: (e.get("number") is None, e.get("number") or 0)):
        number, link = ep.get("number"), ep.get("playerLink")
        if number is None or not link:
            continue
        ep_id = episode_ids.get(number)
        if ep_id is None:
            if not create_episodes:
                stats.no_episode += 1
                continue
            if number < 0 or number > ahead_limit:
                stats.far_episodes += 1
                continue
            ep_id = _insert_episode(
                cur, season_id, number, link, stamp=stamp and features.episode_dates
            )
            episode_ids[number] = ep_id
            episode_links[ep_id] = link
            stats.episodes_created += 1
        elif create_episodes and not episode_links.get(ep_id):
            cur.execute(
                "UPDATE episodes SET player_link = %s, updated_at = now() WHERE id = %s",
                (link, ep_id),
            )
            episode_links[ep_id] = link
            stats.episodes_linked += 1
        wanted.append((ep_id, link))
    if not wanted:
        return

    cur.execute(
        f"SELECT episode_id, id, player_link FROM {TABLE} "
        "WHERE voiceover_id = %s AND episode_id = ANY(%s) ORDER BY id",
        (voiceover_id, [ep_id for ep_id, _ in wanted]),
    )
    existing: dict[int, tuple[int, str | None]] = {}
    for ep_id, source_id, link in cur.fetchall():
        existing.setdefault(ep_id, (source_id, link))

    inserted: list[int] = []
    for ep_id, link in wanted:
        current = existing.get(ep_id)
        if current is None:
            _insert_source(cur, columns, ep_id, voiceover_id, link, stamp=stamp)
            stats.created += 1
            inserted.append(ep_id)
        elif current[1] != link:
            assignments = "player_link = %s" + (", updated_at = now()" if "updated_at" in columns else "")
            cur.execute(f"UPDATE {TABLE} SET {assignments} WHERE id = %s", (link, current[0]))
            stats.updated += 1
        else:
            stats.unchanged += 1
    if features.episode_dates:
        refresh_episode_stats(cur, inserted)


# ─── Сводка по озвучке тайтла (title_dubs) ───────────────────────

def dub_summary(rec: dict[str, Any]) -> tuple[str, int | None, int]:
    """(kodik_id записи, последняя серия, число серий со ссылкой) по записи Kodik."""
    numbers = [
        ep["number"]
        for season in rec.get("seasons") or []
        for ep in season.get("episodes") or []
        if isinstance(ep, dict) and ep.get("number") is not None and ep.get("playerLink")
    ]
    return str(rec.get("kodikId") or ""), (max(numbers) if numbers else None), len(numbers)


def dub_unchanged(cur, content_id: int, voiceover_id: int, rec: dict[str, Any]) -> bool:
    """True, если title_dubs уже хранит эту же запись Kodik в том же состоянии
    (тот же id, время обновления, последняя серия и число серий)."""
    kodik_id, last_episode, count = dub_summary(rec)
    cur.execute(
        f"SELECT (kodik_id = %s AND last_episode IS NOT DISTINCT FROM %s AND episodes_count = %s "
        f"AND kodik_updated_at IS NOT DISTINCT FROM %s::timestamptz) FROM {TITLE_DUBS} "
        "WHERE content_id = %s AND voiceover_id = %s",
        (kodik_id, last_episode, count, rec.get("updatedAt"), content_id, voiceover_id),
    )
    row = cur.fetchone()
    return bool(row and row[0])


def save_dub_summary(cur, content_id: int, voiceover_id: int, rec: dict[str, Any]) -> None:
    kodik_id, last_episode, count = dub_summary(rec)
    if not kodik_id:
        return
    cur.execute(
        f"INSERT INTO {TITLE_DUBS} (content_id, voiceover_id, kodik_id, last_episode, episodes_count, "
        "kodik_updated_at, updated_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s::timestamptz, now(), now()) "
        "ON CONFLICT (content_id, voiceover_id) DO UPDATE SET kodik_id = EXCLUDED.kodik_id, "
        "last_episode = EXCLUDED.last_episode, episodes_count = EXCLUDED.episodes_count, "
        "kodik_updated_at = EXCLUDED.kodik_updated_at, updated_at = now()",
        (content_id, voiceover_id, kodik_id, last_episode, count, rec.get("updatedAt")),
    )


def sync_record(
    cur,
    columns: set[str],
    voiceover_id: int,
    rec: dict[str, Any],
    content_index: dict[str, int],
    stats: SourceStats,
    *,
    create_episodes: bool = False,
    max_ahead: int = DEFAULT_MAX_AHEAD,
    features: Features = NO_FEATURES,
) -> None:
    shikimori_id = str(rec.get("shikimoriId") or "").strip()
    content_id = content_index.get(shikimori_id) if shikimori_id else None
    if content_id is None:
        stats.no_content += 1
        return

    rec_seasons = rec.get("seasons") or []
    if not rec_seasons:
        return
    if features.title_dubs and not features.full and dub_unchanged(cur, content_id, voiceover_id, rec):
        stats.unchanged_dubs += 1
        return
    cur.execute("SELECT id, season_number FROM seasons WHERE content_id = %s", (content_id,))
    pairs = pair_seasons(cur.fetchall(), rec_seasons)
    if not pairs:
        stats.ambiguous += 1
        return
    stamp = stamp_new(features, rec)
    cap = aired_cap(rec)
    new_episodes_before = stats.episodes_created + stats.episodes_linked
    created_before = stats.created
    for season_id, season in pairs:
        sync_season_sources(
            cur, columns, voiceover_id, season_id, season, stats,
            create_episodes=create_episodes, max_ahead=max_ahead,
            features=features, stamp=stamp, cap=cap,
        )
    if features.title_dubs:
        save_dub_summary(cur, content_id, voiceover_id, rec)
    new_episode = stats.episodes_created + stats.episodes_linked > new_episodes_before
    new_dub_for_known_episode = stamp and stats.created > created_before
    if new_episode or new_dub_for_known_episode:
        touch_content(cur, content_id, rec.get("updatedAt"))  # новая серия / озвучка = обновление тайтла


def write_records(
    cur,
    columns: set[str],
    voiceover_id: int,
    records: list[dict[str, Any]],
    failed: list[str],
    *,
    create_episodes: bool = False,
    max_ahead: int = DEFAULT_MAX_AHEAD,
    features: Features = NO_FEATURES,
) -> SourceStats:
    stats = SourceStats()
    content_index = load_content_index(cur)
    for rec in records:
        cur.execute("SAVEPOINT src")
        record_stats = SourceStats()
        try:
            sync_record(
                cur, columns, voiceover_id, rec, content_index, record_stats,
                create_episodes=create_episodes, max_ahead=max_ahead, features=features,
            )
        except Exception as exc:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT src")
            label = rec.get("titleEn") or rec.get("kodikId")
            failed.append(f"{label}: {exc}")
            print(f"    [ERR] {label}: {exc}")
            continue
        cur.execute("RELEASE SAVEPOINT src")
        stats.add(record_stats)
    return stats


# ─── Команда ─────────────────────────────────────────────────────

# ─── Режим «по тайтлу» ───────────────────────────────────────────

def ongoing_shikimori_ids(cur) -> list[str]:
    """Shikimori ID сериалов, помеченных в БД как ongoing."""
    support = detect_status_support(cur)
    if not support.content:
        raise SystemExit(
            f"--by-title: в content нет колонки {support.column} (статус выхода) — "
            "список онгоингов взять неоткуда. Примените миграцию CMS и выполните `sync`."
        )
    cur.execute(
        f"SELECT DISTINCT shikimori_id FROM content WHERE type = 'series' "
        f"AND shikimori_id IS NOT NULL AND {support.column} = 'ongoing' ORDER BY shikimori_id"
    )
    return [str(row[0]).strip() for row in cur.fetchall() if str(row[0]).strip()]


def pick_dub_records(
    records: list[dict[str, Any]], targets: list[Target]
) -> list[tuple[Target, dict[str, Any]]]:
    """Из записей Kodik одного тайтла (по записи на озвучку) оставляет записи озвучек
    справочника, в порядке targets (основная — первой). Если у озвучки несколько записей
    (например, разные части), берётся с большим числом серий."""
    by_kodik = {t.kodik_id: t for t in targets}
    best: dict[int, dict[str, Any]] = {}
    for rec in records:
        translation = rec.get("translation") or {}
        try:
            kodik_id = int(translation.get("id"))
        except (TypeError, ValueError):
            continue
        if kodik_id not in by_kodik:
            continue
        if kodik_id not in best or dub_summary(rec)[2] > dub_summary(best[kodik_id])[2]:
            best[kodik_id] = rec
    return [(t, best[t.kodik_id]) for t in targets if t.kodik_id in best]


def run_by_title(
    args: argparse.Namespace,
    token: str,
    url: str,
    targets: list[Target],
    columns: set[str],
    known_voiceovers: set[int],
    create_episodes: bool,
    max_ahead: int,
) -> tuple[SourceStats, list[str]]:
    """Онгоинги из БД, по одному запросу Kodik /search?shikimori_id=… на тайтл — вместо
    полного прохода каталога по каждой из озвучек: в ответе сразу все озвучки тайтла."""
    with transaction(url, dry_run=True) as conn:
        with conn.cursor() as cur:
            shikimori_ids = ongoing_shikimori_ids(cur)
    max_titles = getattr(args, "max_titles", None)
    if max_titles:
        shikimori_ids = shikimori_ids[:max_titles]
    print(f"Онгоингов в БД: {len(shikimori_ids)}; запрос Kodik — по одному на тайтл")

    total = SourceStats()
    failed: list[str] = []
    changed = False
    delay = args.delay if args.delay is not None else kodik_delay()

    for start in range(0, len(shikimori_ids), TITLE_CHUNK):
        chunk = shikimori_ids[start:start + TITLE_CHUNK]

        # 1. Сеть — до открытия транзакции.
        fetched: list[tuple[str, list[tuple[Target, dict[str, Any]]]]] = []
        for shikimori_id in chunk:
            try:
                records = api.fetch_by_shikimori_id(shikimori_id, token=token)
            except RuntimeError as exc:
                failed.append(f"shikimori {shikimori_id}: {exc}")
                print(f"  [ERR] shikimori {shikimori_id}: {exc}")
                continue
            fetched.append((shikimori_id, pick_dub_records(records, targets)))
            if delay:
                time.sleep(delay)

        # 2. Запись куска одной транзакцией, каждая озвучка тайтла — в своём SAVEPOINT.
        with transaction(url, dry_run=args.dry_run) as conn:
            with conn.cursor() as cur:
                if not try_advisory_lock(cur):
                    raise SystemExit("Другая задача пайплайна уже пишет в БД — запуск пропущен, повторите позже.")
                base = detect_features(cur, columns, full=getattr(args, "full", False))
                content_index = load_content_index(cur)
                for shikimori_id, dubs in fetched:
                    for target, rec in dubs:
                        features = replace(base, backfill=target.voiceover_id not in known_voiceovers)
                        record_stats = SourceStats()
                        cur.execute("SAVEPOINT src")
                        try:
                            sync_record(
                                cur, columns, target.voiceover_id, rec, content_index, record_stats,
                                create_episodes=create_episodes, max_ahead=max_ahead, features=features,
                            )
                        except Exception as exc:  # noqa: BLE001
                            cur.execute("ROLLBACK TO SAVEPOINT src")
                            failed.append(f"{target.title} / shikimori {shikimori_id}: {exc}")
                            print(f"    [ERR] {target.title} / shikimori {shikimori_id}: {exc}")
                            continue
                        cur.execute("RELEASE SAVEPOINT src")
                        total.add(record_stats)
                        changed = changed or record_stats.changed()
        print(f"  … обработано {min(start + TITLE_CHUNK, len(shikimori_ids))} из {len(shikimori_ids)}")

    if changed and not args.dry_run:
        notify_frontend()
    return total, failed


# ─── Команда ─────────────────────────────────────────────────────

def print_summary(total: SourceStats, prefix: str = "") -> None:
    print(f"\n{prefix}--- Сводка sync-dubs ---")
    print(f"Серий создано по ссылкам других озвучек: {total.episodes_created}")
    print(f"Серий без ссылки — ссылка проставлена: {total.episodes_linked}")
    print(f"Источников создано: {total.created}")
    print(f"Ссылок обновлено: {total.updated}")
    print(f"Без изменений: {total.unchanged}")
    print(f"Озвучек тайтлов без изменений (пропущены по title_dubs): {total.unchanged_dubs}")
    print(f"Пропущено — тайтла нет в БД: {total.no_content}")
    print(f"Пропущено — серии нет в БД (создание выключено): {total.no_episode}")
    print(f"Пропущено — номер серии слишком далеко от существующих (--max-ahead / episodes_aired): {total.far_episodes}")
    print(f"Пропущено — сезоны не сопоставляются однозначно: {total.ambiguous}")


def run(args: argparse.Namespace) -> None:
    token = args.token or kodik_token()
    url = database_url()

    with transaction(url, dry_run=True) as conn:  # только чтение
        with conn.cursor() as cur:
            columns = require_sources_table(cur)
            targets = load_targets(cur, args.only)
            known_voiceovers = voiceovers_with_sources(cur)
            base_features = detect_features(cur, columns, full=getattr(args, "full", False))
    if not targets:
        raise SystemExit(
            "Нет озвучек с kodik_translation_id. Выполните `match-voiceovers --write`, затем `sync-voiceovers`."
        )

    # Основная озвучка — первой: если у серии есть её ссылка, она станет ссылкой по умолчанию.
    main_id = int(kodik_translation_id())
    targets.sort(key=lambda t: (t.kodik_id != main_id, t.voiceover_id))
    create_episodes = not getattr(args, "no_create_episodes", False)
    max_ahead = getattr(args, "max_ahead", DEFAULT_MAX_AHEAD)

    prefix = "[DRY-RUN, изменения откатены] " if args.dry_run else ""
    print(
        f"Озвучек: {len(targets)}; first_seen_at: {'да' if base_features.first_seen else 'нет'}; "
        f"даты серий: {'да' if base_features.episode_dates else 'нет'}; "
        f"title_dubs: {'да' if base_features.title_dubs else 'нет'}"
    )
    if not base_features.first_seen:
        print("  (колонок новой схемы нет — применяйте миграцию CMS 20261010_120000, чтобы копить даты появления)")

    if getattr(args, "by_title", False):
        total, failed = run_by_title(
            args, token, url, targets, columns, known_voiceovers, create_episodes, max_ahead,
        )
        print_summary(total, prefix)
        if failed:
            print(f"Ошибок: {len(failed)}")
            raise SystemExit(f"sync-dubs завершён с ошибками ({len(failed)}).")
        return

    total = SourceStats()
    failed: list[str] = []
    skipped: list[str] = []
    print(f"Озвучек к загрузке: {len(targets)}{' (только онгоинги)' if args.ongoing_only else ''}")

    for target in targets:
        print(f"\n▶ {target.title} (Kodik {target.kodik_id})")
        try:
            records = fetch_records(
                token, target.kodik_id, limit=args.limit, delay=args.delay,
                max_pages=args.max_pages, ongoing_only=args.ongoing_only,
            )
        except RuntimeError as exc:
            failed.append(f"{target.title}: {exc}")
            print(f"  [ERR] {exc}")
            continue
        if not records:
            print("  Kodik не вернул сериалов для этой озвучки.")
            continue

        with transaction(url, dry_run=args.dry_run) as conn:
            with conn.cursor() as cur:
                if not try_advisory_lock(cur):
                    skipped.append(target.title)
                    print("  Другая задача пайплайна пишет в БД — озвучка пропущена.")
                    continue
                columns = require_sources_table(cur)
                features = replace(
                    detect_features(cur, columns, full=getattr(args, "full", False)),
                    backfill=target.voiceover_id not in voiceovers_with_sources(cur),
                )
                stats = write_records(
                    cur, columns, target.voiceover_id, records, failed,
                    create_episodes=create_episodes, max_ahead=max_ahead, features=features,
                )

        total.add(stats)
        print(
            f"  записей Kodik: {len(records)}; +{stats.created} новых, "
            f"~{stats.updated} ссылок обновлено, без изменений {stats.unchanged}"
            + (f"; новых серий: {stats.episodes_created}" if stats.episodes_created else "")
            + (f"; пропущено по title_dubs: {stats.unchanged_dubs}" if stats.unchanged_dubs else "")
        )

    if total.changed() and not args.dry_run:
        notify_frontend()

    print_summary(total, prefix)
    if skipped:
        print(f"Пропущены из-за блокировки: {', '.join(skipped)}")
    if failed:
        print(f"Ошибок: {len(failed)}")
        raise SystemExit(f"sync-dubs завершён с ошибками ({len(failed)}).")
    if skipped:
        raise SystemExit("sync-dubs: часть озвучек пропущена из-за блокировки — повторите запуск.")
