"""
Команда update-ongoing: быстро обновляет серии у сериалов со статусом
«выходит» (ongoing), не гоняя весь каталог.

Что делает:
  1. Запрашивает у Kodik только онгоинги (anime_status / all_status = ongoing).
  2. Находит уже загруженный content по kodik_id (франшизы не пересчитываются,
     нумерация сезонов в БД не ломается).
  3. Добавляет новые серии, обновляет изменившиеся ссылки на плеер.
  4. Обновляет статус выхода (content.release_status), если колонка есть.
  5. Проверяет тайтлы, которые в БД помечены как ongoing, но пропали из
     ответа Kodik (обычно — сериал завершился), и фиксирует их новый статус
     вместе с финальными сериями.

Тайтлы, которых ещё нет в БД, не создаются — они попадут при обычном
`sync` (иначе нумерация сезонов франшизы была бы неверной). Команда о них
сообщает.

Запуск: python pipeline.py update-ongoing [--dry-run] [--no-recheck]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from . import api
from .config import database_url
from .db import transaction, try_advisory_lock
from .load import INSERT_SEASON_SQL, insert_episode
from .status import ONGOING, StatusSupport, detect_status_support, set_release_status


@dataclass
class UpdateStats:
    checked: int = 0
    titles_changed: int = 0
    new_episodes: int = 0
    changed_links: int = 0
    new_seasons: int = 0
    status_changed: int = 0
    finished: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ─── Серии и сезоны ──────────────────────────────────────────────

def sync_episodes(cur, season_id: int, episodes: list[dict[str, Any]]) -> tuple[int, int]:
    """Сверяет серии сезона с Kodik одним SELECT-ом.
    Возвращает (создано, ссылок обновлено). Существующие серии без изменений
    ссылки не трогаются (и их updated_at не меняется)."""
    cur.execute(
        "SELECT id, episode_number, player_link FROM episodes WHERE season_id = %s",
        (season_id,),
    )
    existing = {number: (ep_id, link) for ep_id, number, link in cur.fetchall()}

    created = changed = 0
    for ep in episodes:
        number = ep.get("number")
        link = ep.get("playerLink")
        if number is None:
            continue
        if number in existing:
            ep_id, old_link = existing[number]
            if link and link != old_link:
                cur.execute(
                    "UPDATE episodes SET player_link = %s, updated_at = now() WHERE id = %s",
                    (link, ep_id),
                )
                changed += 1
        else:
            insert_episode(cur, season_id, number, link)
            created += 1
    return created, changed


def resolve_seasons(
    cur,
    content_id: int,
    rec_seasons: list[dict[str, Any]],
    stats: UpdateStats,
) -> list[tuple[int, dict[str, Any]]]:
    """Сопоставляет сезоны Kodik с сезонами в БД.

    Запись Kodik обычно содержит один сезон, а его номер в БД мог быть
    перенумерован при группировке франшизы (S1/S2/S3...). Поэтому единственный
    сезон сопоставляется с единственным сезоном content напрямую, иначе — по номеру.
    """
    cur.execute(
        "SELECT id, season_number FROM seasons WHERE content_id = %s ORDER BY season_number",
        (content_id,),
    )
    db_seasons = cur.fetchall()

    if len(rec_seasons) == 1 and len(db_seasons) == 1:
        return [(db_seasons[0][0], rec_seasons[0])]

    by_number = {number: season_id for season_id, number in db_seasons}
    result: list[tuple[int, dict[str, Any]]] = []
    for season in rec_seasons:
        number = season.get("seasonNumber")
        if number is None:
            continue
        season_id = by_number.get(number)
        if season_id is None:
            cur.execute(
                INSERT_SEASON_SQL,
                (content_id, number, season.get("title"), season.get("releaseYear")),
            )
            season_id = cur.fetchone()[0]
            by_number[number] = season_id
            stats.new_seasons += 1
        result.append((season_id, season))
    return result


def touch_content(cur, content_id: int, updated_at: str | None) -> None:
    """Обновляет updated_at у content и его версий (новая серия = обновление тайтла)."""
    cur.execute(
        "UPDATE content SET updated_at = COALESCE(%s::timestamptz, now()) WHERE id = %s",
        (updated_at, content_id),
    )
    cur.execute(
        "UPDATE _content_v SET version_updated_at = COALESCE(%s::timestamptz, now()) "
        "WHERE parent_id = %s",
        (updated_at, content_id),
    )


# ─── Обновление одной записи ─────────────────────────────────────

def update_record(cur, rec: dict[str, Any], support: StatusSupport, stats: UpdateStats) -> bool:
    """Обновляет один тайтл. False — тайтла нет в БД."""
    kodik_id = rec.get("kodikId")
    cur.execute(
        "SELECT id FROM content WHERE kodik_id = %s AND type = 'series' LIMIT 1",
        (str(kodik_id),),
    )
    row = cur.fetchone()
    if not row:
        return False
    content_id = row[0]

    new_eps = changed_links = 0
    rec_seasons = rec.get("seasons") or []
    if rec_seasons:
        for season_id, season in resolve_seasons(cur, content_id, rec_seasons, stats):
            created, changed = sync_episodes(cur, season_id, season.get("episodes") or [])
            new_eps += created
            changed_links += changed

    status_changed = set_release_status(cur, content_id, rec.get("status"), support)

    if new_eps or changed_links:
        touch_content(cur, content_id, rec.get("updatedAt"))

    stats.new_episodes += new_eps
    stats.changed_links += changed_links
    stats.status_changed += int(status_changed)
    if new_eps or changed_links or status_changed:
        stats.titles_changed += 1
        label = rec.get("titleRu") or rec.get("titleEn") or kodik_id
        parts = []
        if new_eps:
            parts.append(f"+{new_eps} сер.")
        if changed_links:
            parts.append(f"ссылок: {changed_links}")
        if status_changed:
            parts.append(f"статус → {rec.get('status')}")
        print(f"  [UPD] {label}: {', '.join(parts)}")
    return True


def find_stale_ongoing(cur, seen_kodik_ids: set[str], support: StatusSupport) -> list[str]:
    """Kodik ID тайтлов, помеченных в БД как ongoing, но не пришедших в
    актуальном списке онгоингов. Без колонки статуса определить их нельзя."""
    if not support.content:
        return []
    cur.execute(
        f"SELECT kodik_id FROM content "
        f"WHERE type = 'series' AND {support.column} = %s AND kodik_id IS NOT NULL",
        (ONGOING,),
    )
    return [str(row[0]) for row in cur.fetchall() if str(row[0]) not in seen_kodik_ids]


# ─── CLI ─────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    from .config import kodik_token, kodik_translation_id

    token = args.token or kodik_token()
    translation_id = args.translation_id or kodik_translation_id()

    # 1. Онгоинги из Kodik (сеть — до открытия транзакции).
    records = api.fetch_ongoing(
        token=token,
        translation_id=translation_id,
        limit=args.limit,
        delay=args.delay,
        max_pages=args.max_pages,
    )
    print(f"\nОнгоингов получено из Kodik: {len(records)}")
    if not records:
        raise SystemExit(
            "Kodik не вернул ни одного онгоинга — похоже на сбой API или фильтра. "
            "БД не тронута."
        )
    seen_ids = {str(r["kodikId"]) for r in records}

    # 2. Кого проверить дополнительно (были ongoing, но пропали из списка).
    stale_records: list[dict[str, Any]] = []
    stale_unreachable: list[str] = []
    if args.max_pages and not args.no_recheck:
        print("--max-pages задан: список онгоингов неполный, перепроверка пропавших отключена.")
        args.no_recheck = True
    if not args.no_recheck:
        with transaction(database_url()) as conn:
            with conn.cursor() as cur:
                support = detect_status_support(cur)
                stale_ids = find_stale_ongoing(cur, seen_ids, support)
        if not support.content:
            print(
                f"Колонки content.{support.column} нет — проверка завершившихся "
                "тайтлов пропущена, статус не сохраняется."
            )
        if stale_ids:
            if len(stale_ids) > args.recheck_limit:
                print(
                    f"Пропавших онгоингов {len(stale_ids)}, проверяю первые "
                    f"{args.recheck_limit} (--recheck-limit)."
                )
                stale_ids = stale_ids[: args.recheck_limit]
            print(f"Проверяю тайтлы, пропавшие из онгоингов: {len(stale_ids)}")
            for kodik_id in stale_ids:
                try:
                    rec = api.fetch_by_kodik_id(kodik_id, token=token)
                except RuntimeError as exc:
                    print(f"  [WARN] {kodik_id}: {exc}")
                    stale_unreachable.append(kodik_id)
                    continue
                if rec is None:
                    print(f"  [WARN] {kodik_id}: Kodik не нашёл запись, статус не меняю")
                    stale_unreachable.append(kodik_id)
                else:
                    stale_records.append(rec)

    # 3. Запись в БД одной транзакцией, каждая запись — в своём SAVEPOINT.
    stale_ids_seen = {id(r) for r in stale_records}
    stats = UpdateStats()
    with transaction(database_url(), dry_run=args.dry_run) as conn:
        with conn.cursor() as cur:
            if not try_advisory_lock(cur):
                raise SystemExit("Другая задача пайплайна уже пишет в БД — запуск пропущен.")
            support = detect_status_support(cur)

            for rec in records + stale_records:
                stats.checked += 1
                label = rec.get("titleRu") or rec.get("titleEn") or rec.get("kodikId")
                cur.execute("SAVEPOINT upd")
                try:
                    found = update_record(cur, rec, support, stats)
                except Exception as exc:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT upd")
                    stats.errors.append(f"{label}: {exc}")
                    print(f"  [ERR] {label}: {exc}")
                    continue
                cur.execute("RELEASE SAVEPOINT upd")
                if not found:
                    stats.missing.append(str(label))
                elif id(rec) in stale_ids_seen:
                    stats.finished.append(f"{label} → {rec.get('status')}")

    prefix = "[DRY-RUN, изменения откатены] " if args.dry_run else ""
    print(f"\n{prefix}--- Сводка update-ongoing ---")
    print(f"Проверено тайтлов: {stats.checked}")
    print(f"Изменилось тайтлов: {stats.titles_changed}")
    print(f"Новых серий: {stats.new_episodes}")
    print(f"Обновлено ссылок: {stats.changed_links}")
    print(f"Новых сезонов: {stats.new_seasons}")
    print(f"Смен статуса: {stats.status_changed}")
    if stats.finished:
        print(f"Пропали из онгоингов: {len(stats.finished)}")
        for line in stats.finished:
            print(f"  ✓ {line}")
    if stale_unreachable:
        print(f"Не удалось перепроверить: {len(stale_unreachable)} (повторится при следующем запуске)")
    if stats.missing:
        print(f"Нет в БД (появятся после `sync`): {len(stats.missing)}")
        for label in stats.missing[:20]:
            print(f"  ? {label}")
        if len(stats.missing) > 20:
            print(f"  ...и ещё {len(stats.missing) - 20}")
    if stats.errors:
        raise SystemExit(f"Обновление завершено с ошибками: {len(stats.errors)}")
