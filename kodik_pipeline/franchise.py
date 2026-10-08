"""
Нумерация сезонов внутри франшизы (записи Kodik с общим kinopoisk_id / imdb_id).

У Kodik каждый сезон (а для аниме — и каждая «часть») обычно отдельная запись.
Здесь из группы таких записей получается ровная нумерация:

  Сезон 1, Сезон 2, Сезон 3 · Часть 1, Сезон 3 · Часть 2, Сезон 4 ...

Правила:
  - записи сортируются по (год, shikimori_id как ЧИСЛО, kodik id) — раньше
    shikimori_id сравнивался как строка, и «9999» шло после «10000»;
  - записи с одним и тем же shikimori_id — это один и тот же сезон: остаётся
    лучшая (больше серий, затем свежее обновление), остальные пропускаются;
  - «Часть 2», «Part 2», «Cour 2» в названии → тот же номер сезона, что у
    предыдущей записи, а не новый сезон;
  - если в названиях есть явный номер сезона («2 сезон», «Season 3»), части
    склеиваются только при совпадении номеров;
  - запись Kodik с несколькими сезонами (обычный сериал) продолжает общую
    нумерацию: её сезоны получают последовательные номера;
  - сезон «0» (спецвыпуски) всегда остаётся нулевым и в нумерации не участвует.

Функция работает только с данными из API, БД не трогает: нумерация
детерминирована и не зависит от порядка выдачи Kodik.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_PART_PATTERNS = (
    re.compile(r"(?:част[ьи]|part|cour|кур)\s*[-:№#]?\s*(\d{1,2})", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*-?\s*(?:я\s+|-?й\s+)?(?:част[ьи]|part|cour)", re.IGNORECASE),
)

_SEASON_PATTERNS = (
    re.compile(r"(?:сезон|season)\s*[-:№#]?\s*(\d{1,2})(?!\d)", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*-?\s*(?:й|ой|ый|я|st|nd|rd|th)?\s*(?:сезон|season)", re.IGNORECASE),
)


def _first_int(patterns: tuple[re.Pattern[str], ...], titles: list[str]) -> int | None:
    for title in titles:
        for pattern in patterns:
            match = pattern.search(title)
            if match:
                value = int(match.group(1))
                if value > 0:
                    return value
    return None


def detect_part(*titles: Any) -> int | None:
    """Номер части из названия: «Часть 2», «2 часть», «Part 2», «Cour 2»."""
    return _first_int(_PART_PATTERNS, [str(t) for t in titles if t])


def detect_season_marker(*titles: Any) -> int | None:
    """Явный номер сезона из названия: «2 сезон», «Сезон 3», «Season 4», «2nd Season»."""
    return _first_int(_SEASON_PATTERNS, [str(t) for t in titles if t])


def _shikimori_number(rec: dict[str, Any]) -> float:
    try:
        return float(int(str(rec.get("shikimoriId") or "").strip()))
    except ValueError:
        return float("inf")


def _year(rec: dict[str, Any]) -> int:
    year = rec.get("releaseYear")
    if isinstance(year, int):
        return year
    try:
        return int(str(year))
    except (TypeError, ValueError):
        return 9999


def record_sort_key(rec: dict[str, Any]) -> tuple[int, float, str]:
    return (_year(rec), _shikimori_number(rec), str(rec.get("kodikId") or ""))


def _episode_count(rec: dict[str, Any]) -> int:
    return sum(len(season.get("episodes") or []) for season in rec.get("seasons") or [])


def drop_duplicate_records(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Убирает записи с одинаковым shikimori_id (один и тот же сезон).
    Возвращает (оставшиеся, сообщения о пропущенных)."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in items:
        sid = str(rec.get("shikimoriId") or "").strip()
        if not sid:
            continue
        key = (str(rec.get("type") or ""), sid)
        current = best.get(key)
        if current is None:
            best[key] = rec
            continue
        rank = (_episode_count(rec), str(rec.get("updatedAt") or ""))
        if rank > (_episode_count(current), str(current.get("updatedAt") or "")):
            best[key] = rec

    kept: list[dict[str, Any]] = []
    messages: list[str] = []
    for rec in items:
        sid = str(rec.get("shikimoriId") or "").strip()
        key = (str(rec.get("type") or ""), sid)
        if sid and best[key] is not rec:
            # Запоминаем Kodik ID пропущенного дубля: загрузчик по нему понимает,
            # что запись франшизы в БД «известна» и номера пересчитывать можно.
            best[key].setdefault("duplicateKodikIds", []).append(rec.get("kodikId"))
            messages.append(
                f"дубль shikimori_id={sid}: пропущена запись {rec.get('kodikId')} "
                f"(оставлена {best[key].get('kodikId')})"
            )
            continue
        kept.append(rec)
    return kept, messages


@dataclass
class _Unit:
    rec: dict[str, Any]
    season: dict[str, Any] | None
    marker: int | None
    part: int | None
    number: int = 0


def _unit_titles(rec: dict[str, Any], season: dict[str, Any] | None) -> list[str]:
    values = [
        (season or {}).get("title"),
        rec.get("titleRu"),
        rec.get("titleEn"),
        rec.get("originalTitle"),
    ]
    return [str(v) for v in values if v]


def _build_units(items: list[dict[str, Any]]) -> list[_Unit]:
    units: list[_Unit] = []
    for rec in sorted(items, key=record_sort_key):
        seasons = sorted(rec.get("seasons") or [], key=lambda s: s.get("seasonNumber") or 0)
        if not seasons:
            seasons = [None]  # type: ignore[list-item]
        for season in seasons:
            titles = _unit_titles(rec, season)
            units.append(_Unit(
                rec=rec,
                season=season,
                marker=detect_season_marker(*titles),
                part=detect_part(*titles),
            ))
    return units


def _same_season_as_previous(unit: _Unit, prev: _Unit) -> bool:
    """«Часть N» этой записи относится к тому же сезону, что и предыдущая?"""
    if unit.part is None or unit.part < 2:
        return False
    # Явный номер сезона у «части» должен совпадать с предыдущим; если его нет
    # («Re:Zero 2. Часть 2» после «Re:Zero 2 сезон. Часть 1») — не мешает.
    if unit.marker is not None and unit.marker != prev.marker:
        return False
    if prev.part is None:
        return unit.part == 2
    return prev.part == unit.part - 1


def _with_part_in_title(title: str, part: int) -> str:
    return title if detect_part(title) == part else f"{title}. Часть {part}".strip(". ")


def assign_franchise_numbers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Проставляет seasonNumber / seasonPart записям одной франшизы.

    Меняет записи на месте и возвращает список в порядке нумерации (без
    дублей по shikimori_id). Для каждого сезона записи пишет seasons[i].seasonNumber;
    записи — rec["seasonNumber"] / rec["seasonPart"] (номер первого сезона записи).
    """
    items, _ = drop_duplicate_records(items)
    units = _build_units(items)

    number = 0
    prev: _Unit | None = None
    for unit in units:
        raw = (unit.season or {}).get("seasonNumber")
        if raw == 0:
            unit.number = 0
            continue
        if prev is not None and _same_season_as_previous(unit, prev):
            unit.number = prev.number
            if prev.part is None:
                prev.part = 1  # «Часть 1» явно не названа, но часть 2 после неё есть
        else:
            number += 1
            unit.number = number
        prev = unit

    for unit in units:
        if unit.season is not None:
            unit.season["seasonNumber"] = unit.number
            if unit.part:
                unit.season["part"] = unit.part
                unit.season["title"] = _with_part_in_title(unit.season.get("title") or "", unit.part)

    ordered: list[dict[str, Any]] = []
    for rec in {id(u.rec): u.rec for u in units}.values():
        # Номер записи — номер её первого настоящего сезона (не «0»-спецвыпуска).
        own = [u for u in units if u.rec is rec]
        first = next((u for u in own if u.number > 0), own[0])
        rec["seasonNumber"] = first.number
        rec["seasonPart"] = first.part
        ordered.append(rec)
    return ordered


def number_standalone(rec: dict[str, Any]) -> None:
    """Запись без франшизы (нет kinopoisk_id/imdb_id).

    Одна запись — один сезон → номер 1 (как раньше, чтобы не менять уже загруженные
    данные). Запись Kodik с несколькими сезонами сохраняет НАСТОЯЩИЕ номера:
    раньше первому сезону принудительно ставилась «1», и сезон «0»
    (спецвыпуски) сливался с настоящим первым сезоном.
    """
    seasons = rec.get("seasons") or []
    if len(seasons) <= 1:
        rec["seasonNumber"] = 1
        if seasons:
            seasons[0]["seasonNumber"] = 1
    else:
        rec["seasonNumber"] = min(
            (s["seasonNumber"] for s in seasons if s.get("seasonNumber")), default=1
        )
    rec["seasonPart"] = None


def title_suffix(rec: dict[str, Any]) -> str:
    """Суффикс названия для сезона франшизы: ' S3' или ' S3 Part 2'; у первого сезона пусто."""
    number = rec.get("seasonNumber") or 1
    part = rec.get("seasonPart")
    if number <= 1 and (not part or part <= 1):
        return ""
    suffix = f" S{number}"
    if part and part > 1:
        suffix += f" Part {part}"
    return suffix


def slug_suffix(rec: dict[str, Any]) -> str:
    suffix = title_suffix(rec)
    return "-" + suffix.strip().lower().replace(" part ", "-p").replace(" ", "-") if suffix else ""
