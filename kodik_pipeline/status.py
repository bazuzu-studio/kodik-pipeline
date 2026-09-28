"""Статус выхода тайтла (онгоинг / вышел / анонс).

Kodik отдаёт его в material_data.anime_status (аниме) или
material_data.all_status (остальные сериалы). Это НЕ content.status —
тот служебный (draft/published) и принадлежит Payload.

Для хранения нужна колонка в content (по умолчанию `release_status`,
имя можно поменять через RELEASE_STATUS_COLUMN) и, опционально,
`version_<имя>` в _content_v. Если колонок нет — пайплайн работает как
раньше и просто не пишет статус.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .db import table_columns

ONGOING = "ongoing"
RELEASED = "released"
ANONS = "anons"

_ALIASES = {
    ONGOING: {"ongoing", "онгоинг", "выходит"},
    RELEASED: {"released", "вышел", "вышло", "завершён", "завершен"},
    ANONS: {"anons", "anons ", "announced", "анонс"},
}
_LOOKUP = {alias: canon for canon, aliases in _ALIASES.items() for alias in aliases}
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def normalize_status(value: Any) -> str | None:
    """Приводит статус Kodik к ongoing/released/anons; неизвестное -> как есть."""
    if value in (None, ""):
        return None
    raw = str(value).strip().casefold()
    return _LOOKUP.get(raw, raw or None)


def is_ongoing(value: Any) -> bool:
    return normalize_status(value) == ONGOING


def status_column() -> str:
    name = os.environ.get("RELEASE_STATUS_COLUMN", "").strip() or "release_status"
    if not _IDENT_RE.match(name):
        raise SystemExit(f"Ошибка: RELEASE_STATUS_COLUMN={name!r} — недопустимое имя колонки")
    return name


@dataclass(frozen=True)
class StatusSupport:
    column: str
    content: bool  # есть колонка в content
    version: bool  # есть version_<колонка> в _content_v

    @property
    def version_column(self) -> str:
        return f"version_{self.column}"


def detect_status_support(cur) -> StatusSupport:
    column = status_column()
    content_cols = table_columns(cur, "content")
    version_cols = table_columns(cur, "_content_v")
    return StatusSupport(
        column=column,
        content=column in content_cols,
        version=f"version_{column}" in version_cols,
    )


def set_release_status(cur, content_id: int, status: str | None, support: StatusSupport) -> bool:
    """Пишет статус в content (+ версии). True, если значение в content изменилось."""
    if not status or not support.content:
        return False
    # Имена колонок проверены регуляркой в status_column() — интерполяция безопасна.
    cur.execute(
        f"UPDATE content SET {support.column} = %s WHERE id = %s "
        f"AND {support.column} IS DISTINCT FROM %s",
        (status, content_id, status),
    )
    changed = cur.rowcount > 0
    if support.version:
        cur.execute(
            f"UPDATE _content_v SET {support.version_column} = %s WHERE parent_id = %s",
            (status, content_id),
        )
    return changed
