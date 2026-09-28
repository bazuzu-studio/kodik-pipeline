"""Общие помощники для работы с Postgres."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as PgConnection


@contextmanager
def transaction(database_url: str, *, dry_run: bool = False) -> Iterator[PgConnection]:
    """Открывает соединение, коммитит при успехе, откатывает при ошибке.

    dry_run=True — всё выполняется, но в конце откатывается (для --dry-run).
    """
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        yield conn
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_columns(cur, table_name: str) -> set[str]:
    """Колонки таблицы — чтобы INSERT/UPDATE собирались только из реально
    существующих полей (schema-tolerant)."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %(t)s AND table_schema = 'public'
        """,
        {"t": table_name},
    )
    return {row[0] for row in cur.fetchall()}


def try_advisory_lock(cur, name: str = "kodik-pipeline-write") -> bool:
    """Транзакционный advisory-lock: не даёт двум записывающим задачам
    (sync/load и update-ongoing) работать одновременно. Снимается сам при
    commit/rollback. False — блокировку держит другая задача."""
    cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (name,))
    return bool(cur.fetchone()[0])
