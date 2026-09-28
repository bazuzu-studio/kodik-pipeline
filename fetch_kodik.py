#!/usr/bin/env python3
"""Совместимая обёртка: `python fetch_kodik.py [out.json] [опции]`
== `python pipeline.py fetch --out out.json [опции]`."""
from __future__ import annotations

import sys

from pipeline import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and not argv[0].startswith("-"):
        argv = ["--out", argv[0], *argv[1:]]
    main(["fetch", *argv])
