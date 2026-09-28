#!/usr/bin/env python3
"""Единая точка входа. Источник данных — Kodik API /list.

Команды:
    python pipeline.py fetch            API -> data/kodik.json
    python pipeline.py load             data/kodik.json -> Postgres
    python pipeline.py sync             fetch + load (весь каталог)
    python pipeline.py update-ongoing   обновить серии/статус у онгоингов
    python pipeline.py posters          постеры -> S3/MinIO
    python pipeline.py check-s3         проверка S3/MinIO

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


def check_s3_command(args) -> None:
    from kodik_pipeline.config import S3Config
    from kodik_pipeline.s3_client import check_connection
    check_connection(S3Config.from_env())


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

    p = sub.add_parser("posters", help="постеры из API-данных -> S3/MinIO")
    p.add_argument("--data", default=DEFAULT_DATA)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    p.set_defaults(func=posters_command)

    sub.add_parser("check-s3", help="проверить S3/MinIO").set_defaults(func=check_s3_command)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
