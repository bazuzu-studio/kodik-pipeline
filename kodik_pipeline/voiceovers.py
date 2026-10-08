"""
Справочник озвучек (студий дубляжа) — voiceovers.json → таблица `voiceovers`.

Нужен для связки «серия ↔ озвучка» (таблица episode_sources, см. sources.py и
команду sync-dubs): сначала в БД появляется стабильный список с id, потом на
него ссылаются источники серий.

Принципы (отличаются от genres.py осознанно):
  - ключ записи — slug, id в файле нет: id выдаёт БД по порядку файла
    (на пустой таблице озвучки получат id 1..N в порядке списка; --dry-run
    возвращает счётчик id на место, поэтому пробный запуск id не «сжигает»);
  - ничего не удаляется. На озвучки будут ссылаться серии, а удаление записи
    оборвало бы связи. Записи, которых нет в файле (добавлены в админке),
    остаются, команда лишь сообщает об их количестве;
  - повторный запуск безопасен: существующие записи не дублируются, а строки
    без изменений не трогаются (updated_at не «дёргается»);
  - запись, уже созданная вручную с тем же названием (без учёта регистра),
    но другим slug, приводится к каноническому slug, а не дублируется;
  - таблицу создаёт CMS (коллекция Voiceovers, см. cms/Voiceovers.ts + миграция
    Payload) — пайплайн её не создаёт, чтобы схема не разъезжалась с Payload.

Необязательное поле файла `kodikTranslationId` (число) — id озвучки в Kodik
(translation.id); пишется в колонку kodik_translation_id, если она есть. Нужно,
чтобы позже сопоставлять озвучки Kodik с этим справочником. Его заполняет команда
`match-voiceovers` (translations.py). Необязательное `aliases` (список строк) —
другие названия студии в Kodik; используется только при сопоставлении.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from .config import database_url
from .db import table_columns, transaction, try_advisory_lock
from .json_io import load_json
from .text_utils import slugify

DEFAULT_FILE = "voiceovers.json"
TABLE = "voiceovers"


@dataclass
class VoiceoverStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    outside_file: int = 0  # записи в БД, которых нет в файле (не удаляются)
    changes: list[str] = field(default_factory=list)


def load_voiceovers(path: str) -> list[dict[str, Any]]:
    """Читает и проверяет файл: непустые названия, уникальные slug и названия."""
    raw = load_json(path)
    if not isinstance(raw, list):
        raise SystemExit(f"Ошибка: {path} должен содержать JSON-массив озвучек")

    items: list[dict[str, Any]] = []
    seen_slugs: dict[str, str] = {}
    seen_titles: dict[str, str] = {}

    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise SystemExit(f"Ошибка: {path}, элемент #{index} должен быть объектом")
        title = str(entry.get("title") or "").strip()
        if not title:
            raise SystemExit(f"Ошибка: {path}, элемент #{index} без title")
        slug = str(entry.get("slug") or "").strip() or slugify(title)

        kodik_id = entry.get("kodikTranslationId")
        if kodik_id is not None:
            if isinstance(kodik_id, bool) or not isinstance(kodik_id, int):
                raise SystemExit(f"Ошибка: {path}, «{title}»: kodikTranslationId должен быть целым числом")

        aliases = entry.get("aliases")
        if aliases is not None and not (isinstance(aliases, list) and all(isinstance(a, str) for a in aliases)):
            raise SystemExit(f"Ошибка: {path}, «{title}»: aliases должен быть списком строк")

        if slug in seen_slugs:
            raise SystemExit(f"Ошибка: {path}: slug «{slug}» повторяется («{seen_slugs[slug]}» и «{title}»)")
        if title.casefold() in seen_titles:
            raise SystemExit(f"Ошибка: {path}: название «{title}» повторяется")
        seen_slugs[slug] = title
        seen_titles[title.casefold()] = title

        items.append({"title": title, "slug": slug, "kodikTranslationId": kodik_id})
    return items


def sync_voiceovers_table(cur, path: str = DEFAULT_FILE) -> VoiceoverStats:
    """Приводит таблицу voiceovers в соответствие со списком из файла."""
    items = load_voiceovers(path)

    columns = table_columns(cur, TABLE)
    if not columns:
        raise SystemExit(
            f"Таблица «{TABLE}» не найдена. Сначала добавьте коллекцию Voiceovers в CMS "
            "(cms/Voiceovers.ts) и примените миграцию Payload, затем повторите команду."
        )
    missing = {"title", "slug"} - columns
    if missing:
        raise SystemExit(f"В таблице «{TABLE}» нет колонок: {', '.join(sorted(missing))}")

    has_kodik = "kodik_translation_id" in columns
    kodik_select = ", kodik_translation_id" if has_kodik else ", NULL"
    stats = VoiceoverStats()
    file_slugs: set[str] = set()

    for item in items:
        title, slug, kodik_id = item["title"], item["slug"], item["kodikTranslationId"]
        file_slugs.add(slug)

        cur.execute(
            f"SELECT id, title, slug{kodik_select} FROM {TABLE} WHERE slug = %s ORDER BY id LIMIT 1",
            (slug,),
        )
        row = cur.fetchone()
        if row is None:
            # Создана вручную с другим slug — приводим к каноническому, не плодим дубль.
            cur.execute(
                f"SELECT id, title, slug{kodik_select} FROM {TABLE} "
                "WHERE lower(title) = lower(%s) ORDER BY id LIMIT 1",
                (title,),
            )
            row = cur.fetchone()

        if row is None:
            values: dict[str, Any] = {"title": title, "slug": slug}
            if has_kodik:
                values["kodik_translation_id"] = kodik_id
            names = list(values)
            placeholders = ["%s"] * len(names)
            for ts in ("created_at", "updated_at"):
                if ts in columns:
                    names.append(ts)
                    placeholders.append("now()")
            cur.execute(
                f"INSERT INTO {TABLE} ({', '.join(names)}) VALUES ({', '.join(placeholders)})",
                list(values.values()),
            )
            stats.created += 1
            stats.changes.append(f"+ {title}")
            continue

        row_id, row_title, row_slug, row_kodik = row
        sets: dict[str, Any] = {}
        if row_title != title:
            sets["title"] = title
        if row_slug != slug:
            sets["slug"] = slug
        # Kodik id пишем, только если он задан в файле: ручное значение не затираем пустотой.
        if has_kodik and kodik_id is not None and row_kodik != kodik_id:
            sets["kodik_translation_id"] = kodik_id

        if not sets:
            stats.unchanged += 1
            continue

        assignments = [f"{col} = %s" for col in sets]
        if "updated_at" in columns:
            assignments.append("updated_at = now()")
        cur.execute(
            f"UPDATE {TABLE} SET {', '.join(assignments)} WHERE id = %s",
            [*sets.values(), row_id],
        )
        stats.updated += 1
        stats.changes.append(f"~ {row_title} → {title}" if "title" in sets else f"~ {title}")

    cur.execute(f"SELECT count(*) FROM {TABLE} WHERE slug <> ALL(%s)", (list(file_slugs),))
    stats.outside_file = cur.fetchone()[0]
    return stats


def _sequence_state(cur) -> tuple[str, int, bool] | None:
    """(имя sequence, last_value, is_called) для voiceovers.id или None."""
    cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (TABLE,))
    name = cur.fetchone()[0]
    if not name:
        return None
    cur.execute(f"SELECT last_value, is_called FROM {name}")
    last_value, is_called = cur.fetchone()
    return name, last_value, is_called


def run(args: argparse.Namespace) -> None:
    with transaction(database_url(), dry_run=args.dry_run) as conn:
        with conn.cursor() as cur:
            if not try_advisory_lock(cur):
                raise SystemExit("Другая задача пайплайна уже пишет в БД — запуск пропущен.")
            # Sequence не откатывается вместе с транзакцией: без этого пробный
            # запуск сдвинул бы id (реальные озвучки получили бы 46..90, а не 1..45).
            seq = _sequence_state(cur) if args.dry_run else None
            try:
                stats = sync_voiceovers_table(cur, args.file)
            finally:
                if seq is not None:
                    name, last_value, is_called = seq
                    cur.execute("SELECT setval(%s, %s, %s)", (name, last_value, is_called))

    prefix = "[DRY-RUN, изменения откатены] " if args.dry_run else ""
    for line in stats.changes:
        print(f"  {line}")
    print(f"\n{prefix}--- Сводка sync-voiceovers ---")
    print(f"Создано: {stats.created}")
    print(f"Обновлено: {stats.updated}")
    print(f"Без изменений: {stats.unchanged}")
    if stats.outside_file:
        print(f"Записей в БД вне файла (не тронуты): {stats.outside_file}")
