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

Запуск:
  python pipeline.py sync-dubs --dry-run
  python pipeline.py sync-dubs --only anidub shiza-project
  python pipeline.py sync-dubs --ongoing-only      # быстрый режим для расписания
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from . import api, franchise
from .config import database_url, kodik_token, kodik_translation_id
from .db import table_columns, transaction, try_advisory_lock
from .ongoing import touch_content
from .revalidate import notify_frontend
from .status import is_ongoing

TABLE = "episode_sources"
REQUIRED_COLUMNS = {"episode_id", "voiceover_id", "player_link"}
DEFAULT_MAX_AHEAD = 50


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

    def changed(self) -> bool:
        return bool(self.created or self.updated or self.episodes_created or self.episodes_linked)

    def add(self, other: "SourceStats") -> None:
        for name in (
            "created", "updated", "unchanged", "no_content", "ambiguous", "no_episode",
            "episodes_created", "episodes_linked", "far_episodes",
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


def _insert_source(cur, columns: set[str], episode_id: int, voiceover_id: int, link: str) -> None:
    names = ["episode_id", "voiceover_id", "player_link"]
    placeholders = ["%s", "%s", "%s"]
    for ts in ("created_at", "updated_at"):
        if ts in columns:
            names.append(ts)
            placeholders.append("now()")
    cur.execute(
        f"INSERT INTO {TABLE} ({', '.join(names)}) VALUES ({', '.join(placeholders)})",
        [episode_id, voiceover_id, link],
    )


def _insert_episode(cur, season_id: int, number: Any, link: str) -> int:
    """Новая серия (как load.insert_episode), но с возвратом id — он нужен для источника."""
    cur.execute(
        "INSERT INTO episodes (season_id, episode_number, title, player_link) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (season_id, number, f"Серия {number}", link),
    )
    return cur.fetchone()[0]


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
    ahead_limit = max(episode_ids, default=0) + max_ahead

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
            ep_id = _insert_episode(cur, season_id, number, link)
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

    for ep_id, link in wanted:
        current = existing.get(ep_id)
        if current is None:
            _insert_source(cur, columns, ep_id, voiceover_id, link)
            stats.created += 1
        elif current[1] != link:
            assignments = "player_link = %s" + (", updated_at = now()" if "updated_at" in columns else "")
            cur.execute(f"UPDATE {TABLE} SET {assignments} WHERE id = %s", (link, current[0]))
            stats.updated += 1
        else:
            stats.unchanged += 1


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
) -> None:
    shikimori_id = str(rec.get("shikimoriId") or "").strip()
    content_id = content_index.get(shikimori_id) if shikimori_id else None
    if content_id is None:
        stats.no_content += 1
        return

    rec_seasons = rec.get("seasons") or []
    if not rec_seasons:
        return
    cur.execute("SELECT id, season_number FROM seasons WHERE content_id = %s", (content_id,))
    pairs = pair_seasons(cur.fetchall(), rec_seasons)
    if not pairs:
        stats.ambiguous += 1
        return
    new_episodes_before = stats.episodes_created + stats.episodes_linked
    for season_id, season in pairs:
        sync_season_sources(
            cur, columns, voiceover_id, season_id, season, stats,
            create_episodes=create_episodes, max_ahead=max_ahead,
        )
    if stats.episodes_created + stats.episodes_linked > new_episodes_before:
        touch_content(cur, content_id, rec.get("updatedAt"))  # новая серия = обновление тайтла


def write_records(
    cur,
    columns: set[str],
    voiceover_id: int,
    records: list[dict[str, Any]],
    failed: list[str],
    *,
    create_episodes: bool = False,
    max_ahead: int = DEFAULT_MAX_AHEAD,
) -> SourceStats:
    stats = SourceStats()
    content_index = load_content_index(cur)
    for rec in records:
        cur.execute("SAVEPOINT src")
        record_stats = SourceStats()
        try:
            sync_record(
                cur, columns, voiceover_id, rec, content_index, record_stats,
                create_episodes=create_episodes, max_ahead=max_ahead,
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

def run(args: argparse.Namespace) -> None:
    token = args.token or kodik_token()
    url = database_url()

    with transaction(url, dry_run=True) as conn:  # только чтение
        with conn.cursor() as cur:
            require_sources_table(cur)
            targets = load_targets(cur, args.only)
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
                stats = write_records(
                    cur, columns, target.voiceover_id, records, failed,
                    create_episodes=create_episodes, max_ahead=max_ahead,
                )

        total.add(stats)
        print(
            f"  записей Kodik: {len(records)}; +{stats.created} новых, "
            f"~{stats.updated} ссылок обновлено, без изменений {stats.unchanged}"
            + (f"; новых серий: {stats.episodes_created}" if stats.episodes_created else "")
        )

    if total.changed() and not args.dry_run:
        notify_frontend()

    print(f"\n{prefix}--- Сводка sync-dubs ---")
    print(f"Серий создано по ссылкам других озвучек: {total.episodes_created}")
    print(f"Серий без ссылки — ссылка проставлена: {total.episodes_linked}")
    print(f"Источников создано: {total.created}")
    print(f"Ссылок обновлено: {total.updated}")
    print(f"Без изменений: {total.unchanged}")
    print(f"Пропущено — тайтла нет в БД: {total.no_content}")
    print(f"Пропущено — серии нет в БД (создание выключено): {total.no_episode}")
    print(f"Пропущено — номер серии слишком далеко от существующих (--max-ahead): {total.far_episodes}")
    print(f"Пропущено — сезоны не сопоставляются однозначно: {total.ambiguous}")
    if skipped:
        print(f"Пропущены из-за блокировки: {', '.join(skipped)}")
    if failed:
        print(f"Ошибок: {len(failed)}")
        raise SystemExit(f"sync-dubs завершён с ошибками ({len(failed)}).")
    if skipped:
        raise SystemExit("sync-dubs: часть озвучек пропущена из-за блокировки — повторите запуск.")
