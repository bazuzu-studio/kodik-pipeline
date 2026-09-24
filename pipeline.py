#!/usr/bin/env python3
"""Единая точка входа.

Главное изменение: контент больше НЕ собирается из base/movies/series.
Источник — Kodik API /list.

Команды:
    python pipeline.py fetch
    python pipeline.py load
    python pipeline.py posters
    python pipeline.py sync
    python pipeline.py check-s3

`sync` = fetch API -> load в Postgres.
Постеры запускаются отдельно, чтобы падение S3 не откатывало импорт каталога.
"""
from __future__ import annotations

import argparse
import os


DEFAULT_DATA = "data/kodik.json"
DEFAULT_GENRES = None


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
    out = getattr(args, "out", None) or getattr(args, "data", DEFAULT_DATA)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
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
    poster_argv = [args.data, "--timeout", str(args.timeout), "--retries", str(args.retries), "--max-bytes", str(args.max_bytes)]
    posters_main(poster_argv)


def sync_command(args) -> None:
    fetch_command(args)
    load_command(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Импорт каталога Kodik API в Payload CMS",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="Kodik API /list -> JSON")
    p.add_argument("--out", default=DEFAULT_DATA)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--delay", type=float, default=None)
    p.add_argument("--max-pages", type=int)
    p.add_argument("--token")
    p.add_argument("--translation-id")
    p.set_defaults(func=fetch_command)

    p = sub.add_parser("load", help="JSON -> Postgres")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--genres", default=DEFAULT_GENRES)
    p.set_defaults(func=load_command)

    p = sub.add_parser("posters", help="постеры из API-данных -> S3/MinIO")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    p.set_defaults(func=posters_command)

    p = sub.add_parser("sync", help="API -> JSON -> Postgres")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--genres", default=DEFAULT_GENRES)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--delay", type=float, default=None)
    p.add_argument("--max-pages", type=int)
    p.add_argument("--token")
    p.add_argument("--translation-id")
    p.set_defaults(func=sync_command)

    sub.add_parser("check-s3", help="проверить S3/MinIO").set_defaults(func=None)

    args = parser.parse_args()

    if args.command == "check-s3":
        from kodik_pipeline.config import S3Config
        from kodik_pipeline.s3_client import check_connection
        check_connection(S3Config.from_env())
        return

    args.func(args)


if __name__ == "__main__":
    main()
