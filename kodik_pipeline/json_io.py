"""
Чтение/запись JSON с понятными сообщениями об ошибках.

Дамп data/kodik.json — большой файл, который легко обрезать при
копировании или прерванной записи. Обычный json.JSONDecodeError не говорит
явно "файл обрезан", поэтому здесь это проверяется отдельно. Запись
атомарная (tmp + rename), чтобы упавший fetch не оставил битый файл.
"""

from __future__ import annotations

import json
import os
import tempfile
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
    """Атомарная запись: пишем во временный файл рядом и делаем os.replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def iter_json_array(path: str) -> Iterator[dict[str, Any]]:
    """Потоковое чтение большого JSON-массива объектов (ijson), чтобы не
    держать в памяти весь дамп целиком."""
    import ijson

    try:
        with open(path, "rb") as f:
            yield from ijson.items(f, "item")
    except FileNotFoundError as e:
        raise SystemExit(f"Ошибка: файл не найден: {path}") from e
    except ijson.JSONError as e:
        raise SystemExit(
            f"Ошибка: файл {path} повреждён или обрезан на середине "
            f"записи — перезапустите fetch ({e})."
        ) from e
