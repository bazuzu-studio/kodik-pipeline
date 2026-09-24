"""Мелкие текстовые утилиты, общие для нескольких шагов пайплайна."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def slugify(text: str) -> str:
    """Создаёт стабильный slug, корректно работающий с кириллицей.

    Раньше ASCII-нормализация превращала большинство русских названий
    жанров в пустую строку, после чего несколько жанров получали slug
    ``unknown`` и конфликтовали по уникальному индексу БД.
    """
    text = str(text or "").strip().casefold()
    slug = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-")
    return slug or "unknown"


def to_float(val: Any) -> float | None:
    """Приводит рейтинг (Decimal/int/float/str из JSON или БД) к float.
    0 и отрицательные значения трактуются как "рейтинга нет" -> None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
    elif isinstance(val, str):
        try:
            f = float(val.replace(",", ".").strip())
        except ValueError:
            return None
    else:
        try:
            f = float(val)  # покрывает Decimal и подобные типы
        except (TypeError, ValueError):
            return None
    return round(f, 1) if f > 0 else None
