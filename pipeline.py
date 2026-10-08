#!/usr/bin/env python3
"""Единая точка входа. Источник данных — Kodik API /list.

Команды:
    python pipeline.py fetch            API -> data/kodik.json
    python pipeline.py load             data/kodik.json -> Postgres
    python pipeline.py sync             fetch + load (весь каталог)
    python pipeline.py update-ongoing   обновить серии/статус у онгоингов
    python pipeline.py fix-seasons      убрать дубли сезонов в БД (после fetch)
    python pipeline.py sync-voiceovers  справочник озвучек (voiceovers.json) -> Postgres
    python pipeline.py match-voiceovers найти id озвучек Kodik для voiceovers.json
    python pipeline.py sync-dubs        ссылки на плеер по каждой озвучке -> episode_sources
    python pipeline.py sync-schedule    даты серий и «следующая серия» из AniList
    python pipeline.py posters          постеры -> S3/MinIO
    python pipeline.py check-s3         проверка S3/MinIO
    python pipeline.py revalidate       сбросить кэш сайта вручную

`sync` = fetch API -> load в Postgres. Постеры запускаются отдельно, чтобы
падение S3 не откатывало импорт каталога. `update-ongoing` не трогает
остальной каталог и рассчитан на частый запуск (например, раз в час).
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_DATA = "data/kodik.json"
DEFAULT_GENRES = None


def _add_api_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=None, help="записей на страницу (по умолчанию KODIK_LIMIT)")
    p.add_argument("--delay", type=float, default=None, help="пауза между страницами, сек (KODIK_DELAY)")
    p.add_argument("--max-pages", type=int, help="ограничить число страниц (для проверок)")
    p.add_argument("--token", help="по умолчанию KODIK_TOKEN")
    p.add_argument("--translation-id", help="по умолчанию KODIK_TRANSLATION_ID")


def fetch_command(args) -> None:
    from kodik_pipeline.api import fetch_normalized
    from kodik_pipeline.config import kodik_token, kodik_translation_id
    from kodik_pipeline.json_io import save_json

    records = fetch_normalized(
        token=args.token or kodik_token(),
        translation_id=args.translation_id or kodik_translation_id(),
        limit=args.limit,
        delay=args.delay,
        max_pages=args.max_pages,
    )
    if not records:
        # Не затираем прошлый дамп пустым результатом.
        raise SystemExit("Kodik API вернул 0 записей — файл не перезаписан. Проверьте токен и translation_id.")

    out = getattr(args, "out", None) or getattr(args, "data", DEFAULT_DATA)
    save_json(out, records)
    print(f"\nAPI -> {out}: {len(records)} записей")


def load_command(args) -> None:
    from kodik_pipeline.load import main as load_main
    argv = [args.data]
    if args.genres:
        argv.append(args.genres)
    load_main(argv)


def posters_command(args) -> None:
    from kodik_pipeline.posters import main as posters_main
    posters_main([args.data, "--timeout", str(args.timeout), "--retries", str(args.retries), "--max-bytes", str(args.max_bytes)])


def sync_command(args) -> None:
    fetch_command(args)
    load_command(args)


def update_ongoing_command(args) -> None:
    from kodik_pipeline.ongoing import run
    run(args)


def fix_seasons_command(args) -> None:
    from kodik_pipeline.fix_seasons import run
    run(args)


def sync_voiceovers_command(args) -> None:
    from kodik_pipeline.voiceovers import run
    run(args)


def match_voiceovers_command(args) -> None:
    from kodik_pipeline.translations import run
    run(args)


def sync_dubs_command(args) -> None:
    from kodik_pipeline.sources import run
    run(args)


def sync_schedule_command(args) -> None:
    from kodik_pipeline.schedule import run
    run(args)


def check_s3_command(args) -> None:
    from kodik_pipeline.config import S3Config
    from kodik_pipeline.s3_client import check_connection
    check_connection(S3Config.from_env())


def revalidate_command(args) -> None:
    from kodik_pipeline.revalidate import notify_frontend
    if not notify_frontend():
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Импорт каталога Kodik API в Payload CMS",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="Kodik API /list -> JSON")
    p.add_argument("--out", default=DEFAULT_DATA)
    _add_api_args(p)
    p.set_defaults(func=fetch_command)

    p = sub.add_parser("load", help="JSON -> Postgres")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--genres", default=DEFAULT_GENRES)
    p.set_defaults(func=load_command)

    p = sub.add_parser("sync", help="API -> JSON -> Postgres (весь каталог)")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--genres", default=DEFAULT_GENRES)
    _add_api_args(p)
    p.set_defaults(func=sync_command)

    p = sub.add_parser(
        "update-ongoing",
        help="обновить серии и статус у сериалов со статусом ongoing («выходит»)",
    )
    _add_api_args(p)
    p.add_argument("--dry-run", action="store_true", help="выполнить всё, но откатить изменения в БД")
    p.add_argument("--no-recheck", action="store_true", help="не перепроверять тайтлы, пропавшие из онгоингов")
    p.add_argument("--recheck-limit", type=int, default=200, help="максимум перепроверяемых тайтлов за запуск")
    p.set_defaults(func=update_ongoing_command)

    p = sub.add_parser(
        "fix-seasons",
        help="объединить дубли сезонов и выровнять номера по data/kodik.json (без пересоздания контента)",
    )
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--dry-run", action="store_true", help="выполнить всё, но откатить изменения в БД")
    p.set_defaults(func=fix_seasons_command)

    p = sub.add_parser(
        "sync-voiceovers",
        help="справочник озвучек voiceovers.json -> таблица voiceovers (нужна коллекция Voiceovers в CMS)",
    )
    p.add_argument("--file", default="voiceovers.json")
    p.add_argument("--dry-run", action="store_true", help="выполнить всё, но откатить изменения в БД")
    p.set_defaults(func=sync_voiceovers_command)

    p = sub.add_parser(
        "match-voiceovers",
        help="найти id озвучек Kodik (translations/v2) для voiceovers.json; без --write ничего не меняет",
    )
    p.add_argument("--file", default="voiceovers.json")
    p.add_argument("--token", help="по умолчанию KODIK_TOKEN")
    p.add_argument("--types", default="anime-serial,anime", help="типы материалов Kodik")
    p.add_argument("--write", action="store_true", help="записать найденные id в voiceovers.json")
    p.add_argument(
        "--pick-largest",
        action="store_true",
        help="при нескольких озвучках с одним названием выбрать ту, у которой больше тайтлов",
    )
    p.add_argument(
        "--untracked", type=int, default=15, metavar="N",
        help="показать N самых крупных озвучек Kodik, которых нет в справочнике (0 — не показывать)",
    )
    p.set_defaults(func=match_voiceovers_command)

    p = sub.add_parser(
        "sync-dubs",
        help="ссылки на плеер по каждой озвучке справочника -> episode_sources (нужна коллекция EpisodeSources в CMS)",
    )
    p.add_argument("--only", nargs="+", metavar="SLUG|ID", help="только эти озвучки (slug или id Kodik)")
    p.add_argument("--ongoing-only", action="store_true", help="только онгоинги (быстро, для расписания)")
    p.add_argument(
        "--no-create-episodes", action="store_true",
        help="не создавать серии, которых нет в БД (по умолчанию серии других озвучек добавляются в список)",
    )
    p.add_argument(
        "--max-ahead", type=int, default=50, metavar="N",
        help="не создавать серию с номером дальше N от самой большой существующей (защита от чужой нумерации), по умолчанию 50",
    )
    p.add_argument("--limit", type=int, default=None, help="записей на страницу (KODIK_LIMIT)")
    p.add_argument("--delay", type=float, default=None, help="пауза между страницами, сек (KODIK_DELAY)")
    p.add_argument("--max-pages", type=int, help="ограничить число страниц на озвучку (для проверок)")
    p.add_argument("--token", help="по умолчанию KODIK_TOKEN")
    p.add_argument("--dry-run", action="store_true", help="выполнить всё, но откатить изменения в БД")
    p.set_defaults(func=sync_dubs_command)

    p = sub.add_parser(
        "sync-schedule",
        help="расписание из AniList (по shikimori_id = idMal): episodes.airing_at и «следующая серия»",
    )
    p.add_argument("--state", default="data/schedule-state.json", help="файл состояния: какие тайтлы уже проверены")
    p.add_argument("--only-ongoing", action="store_true", help="только онгоинги (быстро, для крона)")
    p.add_argument("--all", action="store_true", help="перепроверить и уже проверенные тайтлы")
    p.add_argument("--overwrite", action="store_true", help="перезаписывать и уже вышедшие серии с датой")
    p.add_argument("--mal-id", type=int, help="только один тайтл (shikimori_id / idMal)")
    p.add_argument("--limit", type=int, help="не больше N тайтлов (для проверки)")
    p.add_argument("--delay", type=float, default=0.8, help="пауза между запросами к AniList, сек (лимит ~90/мин)")
    p.add_argument("--recheck-days", type=float, default=3.0, help="повторно проверять уже проверенный тайтл с новыми сериями без даты не чаще, чем раз в N дней")
    p.add_argument("--dry-run", action="store_true", help="выполнить всё, но откатить изменения в БД")
    p.set_defaults(func=sync_schedule_command)

    p = sub.add_parser("posters", help="постеры из API-данных -> S3/MinIO")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    p.set_defaults(func=posters_command)

    sub.add_parser("check-s3", help="проверить S3/MinIO").set_defaults(func=check_s3_command)
    sub.add_parser(
        "revalidate", help="сбросить кэш сайта (REVALIDATE_URL / REVALIDATE_SECRET)"
    ).set_defaults(func=revalidate_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
