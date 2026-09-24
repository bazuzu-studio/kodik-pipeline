"""
Чтение/запись JSON с понятными сообщениями об ошибках.

Важно: дампы Kodik (movies.json, series.json) и base.json — это большие
файлы, которые легко случайно обрезать при экспорте/копировании. Обычный
json.JSONDecodeError не говорит явно "файл обрезан", поэтому здесь мы
проверяем это отдельно и подсказываем, что делать.
"""

from __future__ import annotations

import json
from typing import Any, Iterator


def load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError as e:
        raise SystemExit(f"Ошибка: файл не найден: {path}") from e
    except UnicodeDecodeError as e:
        raise SystemExit(f"Ошибка: файл {path} не в кодировке UTF-8: {e}") from e

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        looks_truncated = not text.rstrip().endswith(("]", "}"))
        hint = (
            "похоже, файл обрезан на середине записи — переэкспортируйте "
            "исходные данные и убедитесь, что копирование/скачивание "
            "завершилось полностью"
            if looks_truncated
            else "проверьте синтаксис вручную рядом с указанной позицией"
        )
        raise SystemExit(
            f"Ошибка: файл {path} повреждён (строка {e.lineno}, "
            f"символ {e.colno}): {e.msg}. {hint}."
        ) from e


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def iter_json_array(path: str) -> Iterator[dict[str, Any]]:
    """Потоковое чтение большого JSON-массива объектов (ijson), чтобы не
    держать в памяти весь дамп movies.json / series.json целиком."""
    import ijson

    try:
        with open(path, "rb") as f:
            yield from ijson.items(f, "item")
    except FileNotFoundError as e:
        raise SystemExit(f"Ошибка: файл не найден: {path}") from e
    except ijson.JSONError as e:
        raise SystemExit(
            f"Ошибка: файл {path} повреждён или обрезан на середине "
            f"записи — переэкспортируйте исходные данные ({e})."
        ) from e
