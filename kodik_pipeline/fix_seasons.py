"""
Команда fix-seasons: одноразовая (и безопасная для повторов) чистка сезонов,
которые уже лежат в БД.

Старые версии пайплайна при смене номера сезона создавали вторую строку
`seasons` для того же content — на сайте это двойные сезоны с одинаковыми
сериями. Команда перечитывает data/kodik.json и для каждого сериала заново
сопоставляет сезоны с БД теми же правилами, что и `load` (см. seasons.py):
дубли объединяются, номера выравниваются, контент и серии не пересоздаются.

Запуск: python pipeline.py fix-seasons [--data data/kodik.json] [--dry-run]

Рекомендуется: `pipeline.py fetch` → `fix-seasons --dry-run` → `fix-seasons`.
"""

from __future__ import annotations

import argparse
from typing import Any

from .config import database_url
from .db import table_columns, transaction, try_advisory_lock
from .json_io import load_json
from .load import load_seasons_and_episodes
from .revalidate import notify_frontend
from .seasons import SeasonStats, franchise_members, renumber_allowed


def run(args: argparse.Namespace) -> None:
    records = load_json(args.data)
    if not isinstance(records, list):
        raise SystemExit(f"Ошибка: {args.data} должен содержать JSON-массив записей")

    series = [r for r in records if r.get("type") == "series" and r.get("seasons")]
    members = franchise_members(records)
    cache: dict[str, bool] = {}
    stats = SeasonStats()
    checked = missing = 0
    errors: list[str] = []

    with transaction(database_url(), dry_run=args.dry_run) as conn:
        with conn.cursor() as cur:
            if not try_advisory_lock(cur):
                raise SystemExit("Другая задача пайплайна уже пишет в БД — запуск пропущен.")
            has_franchise = "franchise_id" in table_columns(cur, "content")

            for rec in series:
                label = rec.get("titleRu") or rec.get("titleEn") or rec.get("kodikId")
                cur.execute(
                    "SELECT id FROM content WHERE kodik_id = %s AND type = 'series' ORDER BY id LIMIT 1",
                    (str(rec.get("kodikId")),),
                )
                row = cur.fetchone()
                if not row:
                    missing += 1
                    continue
                checked += 1

                before = (stats.created, stats.renumbered, stats.merged)
                cur.execute("SAVEPOINT fix")
                try:
                    renumber = renumber_allowed(cur, rec, members, cache, has_franchise)
                    load_seasons_and_episodes(
                        cur, row[0], rec["seasons"], renumber=renumber, stats=stats,
                    )
                except Exception as exc:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT fix")
                    errors.append(f"{label}: {exc}")
                    print(f"  [ERR] {label}: {exc}")
                    continue
                cur.execute("RELEASE SAVEPOINT fix")
                if (stats.created, stats.renumbered, stats.merged) != before:
                    print(
                        f"  [FIX] {label}: перенумеровано {stats.renumbered - before[1]}, "
                        f"дублей объединено {stats.merged - before[2]}"
                    )

    if not args.dry_run and stats.changed():
        notify_frontend()

    prefix = "[DRY-RUN, изменения откатены] " if args.dry_run else ""
    print(f"\n{prefix}--- Сводка fix-seasons ---")
    print(f"Сериалов проверено: {checked} (нет в БД: {missing})")
    print(f"Сезонов создано: {stats.created}")
    print(f"Сезонов перенумеровано: {stats.renumbered}")
    print(f"Дублей сезонов объединено: {stats.merged}")
    if stats.kept_extra:
        print(f"⚠ Лишних сезонов оставлено: {stats.kept_extra} (в них есть уникальные серии — проверьте вручную)")
    if errors:
        raise SystemExit(f"fix-seasons завершён с ошибками: {len(errors)}")
