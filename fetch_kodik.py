#!/usr/bin/env python3
"""Получение каталога Kodik напрямую из API.

Пример:
    python fetch_kodik.py data/kodik.json
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from kodik_pipeline.api import fetch_normalized
from kodik_pipeline.config import kodik_token, kodik_translation_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузить каталог Kodik напрямую из API")
    parser.add_argument("out", nargs="?", default="data/kodik.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--token", default=None, help="если не задан, берётся KODIK_TOKEN из .env")
    parser.add_argument("--translation-id", default=None)
    args = parser.parse_args()

    records = fetch_normalized(
        token=args.token or kodik_token(),
        translation_id=args.translation_id or kodik_translation_id(),
        limit=args.limit,
        delay=args.delay,
        max_pages=args.max_pages,
    )

    parent = os.path.dirname(args.out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)

    print(f"\nГотово: {len(records)} записей сохранено в {args.out}")


if __name__ == "__main__":
    main()
