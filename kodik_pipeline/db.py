"""Общий помощник для работы с Postgres-соединением."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as PgConnection


@contextmanager
def transaction(database_url: str) -> Iterator[PgConnection]:
    """Открывает соединение, коммитит при успехе, откатывает при ошибке.

    Используется во всех трёх шагах пайплайна вместо повторяющегося
    try/except/finally с conn.commit()/rollback()/close().
    """
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_columns(cur, table_name: str) -> set[str]:
    """Список колонок таблицы — используется, чтобы INSERT собирался
    только из реально существующих полей (schema-tolerant insert)."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %(t)s AND table_schema = 'public'
        """,
        {"t": table_name},
    )
    return {row[0] for row in cur.fetchall()}
