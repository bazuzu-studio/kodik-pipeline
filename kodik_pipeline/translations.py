"""
Сопоставление озвучек справочника (voiceovers.json) с озвучками Kodik.

Источник — https://kodik-api.com/translations/v2?types=anime-serial,anime:
{"results": [{"id": 609, "title": "...", "count": 123}, ...]}.
Найденный id пишется в voiceovers.json (kodikTranslationId), а дальше его в БД
переносит `sync-voiceovers` (колонка voiceovers.kodik_translation_id).

Сопоставление по названию, осторожное — угадывать id нельзя:
  1. строгое: названия совпадают без учёта регистра и знаков препинания
     («SHIZA Project» = «Shiza Project»; «Re: Voice» = «Re:Voice»);
  2. нестрогое: то же, но без окончания «.TV» («AniLibria» = «AniLibria.TV»);
  3. при совпадении названия с несколькими озвучками Kodik (разные id) — НЕ
     выбираем, а показываем список с числом тайтлов (count). Автовыбор самой
     крупной — только по явному флагу --pick-largest;
  4. похожие названия (одно содержит другое) — только подсказки в отчёте,
     в файл не пишутся.

Дополнительные имена для сопоставления: текст в скобках в названии
(«DeliciousDub (ChillDUB)» → DeliciousDub, ChillDUB) и необязательное поле
`aliases` у записи в voiceovers.json — если в Kodik студия называется иначе.
"""

from __future__ import annotations

import argparse
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .api import _request_url
from .config import kodik_token
from .json_io import load_json, save_json

TRANSLATIONS_URL = "https://kodik-api.com/translations/v2"
DEFAULT_TYPES = "anime-serial,anime"
MIN_FUZZY_LEN = 5


def normalize(name: Any) -> str:
    """Нижний регистр, только буквы и цифры: «Re: Voice» → «revoice»."""
    return re.sub(r"[\W_]+", "", str(name or "").casefold(), flags=re.UNICODE)


def loose(name: Any) -> str:
    """normalize без окончания «tv» («AniLibria.TV» → «anilibria»)."""
    key = normalize(name)
    return key[:-2] if key.endswith("tv") and len(key) - 2 >= 4 else key


def candidate_names(entry: dict[str, Any]) -> list[str]:
    """Названия, по которым ищем озвучку: заголовок, части из скобок, aliases."""
    title = str(entry.get("title") or "")
    names = [title]
    names += [m.strip() for m in re.findall(r"\(([^)]+)\)", title)]
    outside = re.sub(r"\([^)]*\)", " ", title).strip()
    if outside and outside != title:
        names.append(outside)
    names += [str(a) for a in entry.get("aliases") or []]
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name and normalize(name) not in seen:
            seen.add(normalize(name))
            result.append(name)
    return result


@dataclass
class MatchReport:
    # slug -> (id озвучки Kodik, название в Kodik, count, как найдено)
    matched: dict[str, tuple[int, str, int, str]] = field(default_factory=dict)
    # slug -> [(id, название, count), ...] — несколько вариантов, не выбрано
    ambiguous: dict[str, list[tuple[int, str, int]]] = field(default_factory=dict)
    # slug -> [(id, название, count), ...] — только подсказки
    suggestions: dict[str, list[tuple[int, str, int]]] = field(default_factory=dict)
    # slug -> id: расхождение с уже записанным в файле значением
    mismatches: dict[str, tuple[int, int]] = field(default_factory=dict)
    # slug -> [slug, ...]: один id Kodik претендует на несколько озвучек
    conflicts: dict[int, list[str]] = field(default_factory=dict)
    not_found: list[str] = field(default_factory=list)


def _entry(t: dict[str, Any]) -> tuple[int, str, int]:
    return int(t["id"]), str(t.get("title") or ""), int(t.get("count") or 0)


def match_voiceovers(
    voiceovers: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    *,
    pick_largest: bool = False,
) -> MatchReport:
    report = MatchReport()
    usable = [t for t in translations if isinstance(t, dict) and t.get("id") is not None]

    for entry in voiceovers:
        slug = entry["slug"]
        names = candidate_names(entry)
        strict_keys = {normalize(n) for n in names}
        loose_keys = {loose(n) for n in names}

        tiers = (
            ("строгое", [t for t in usable if normalize(t.get("title")) in strict_keys]),
            ("без «.TV»", [t for t in usable if loose(t.get("title")) in loose_keys]),
        )
        found: list[tuple[int, str, int]] = []
        how = ""
        for label, hits in tiers:
            hits = list({int(t["id"]): t for t in hits}.values())
            if hits:
                found = [_entry(t) for t in hits]
                how = label
                break

        if len(found) == 1:
            report.matched[slug] = (*found[0], how)
        elif len(found) > 1:
            if pick_largest:
                best = max(found, key=lambda e: e[2])
                report.matched[slug] = (*best, f"{how}, самая крупная из {len(found)}")
            else:
                report.ambiguous[slug] = sorted(found, key=lambda e: -e[2])
        else:
            fuzzy = [
                _entry(t) for t in usable
                if any(
                    len(k) >= MIN_FUZZY_LEN and len(normalize(t.get("title"))) >= MIN_FUZZY_LEN
                    and (k in normalize(t.get("title")) or normalize(t.get("title")) in k)
                    for k in strict_keys
                )
            ]
            if fuzzy:
                report.suggestions[slug] = sorted(fuzzy, key=lambda e: -e[2])[:5]
            report.not_found.append(slug)

    # Один id Kodik не может быть двумя озвучками одновременно.
    by_id: dict[int, list[str]] = {}
    for slug, (tid, *_rest) in report.matched.items():
        by_id.setdefault(tid, []).append(slug)
    for tid, slugs in by_id.items():
        if len(slugs) > 1:
            report.conflicts[tid] = slugs
            for slug in slugs:
                report.matched.pop(slug, None)
                report.not_found.append(slug)

    # Расхождение с уже записанным в файле id — не перезаписываем молча.
    for entry in voiceovers:
        slug, current = entry["slug"], entry.get("kodikTranslationId")
        if slug in report.matched and current is not None and current != report.matched[slug][0]:
            report.mismatches[slug] = (current, report.matched[slug][0])
            report.matched.pop(slug)
    return report


def untracked_translations(
    voiceovers: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    report: MatchReport,
    limit: int = 15,
) -> list[tuple[int, str, int]]:
    """Озвучки Kodik, которых нет в справочнике: (id, название, число тайтлов), крупные первыми.

    Исключаются id, которые уже в файле или найдены/предложены этим запуском
    (в том числе неоднозначные варианты и конфликты) — это известные студии.
    """
    known: set[int] = {
        int(v["kodikTranslationId"]) for v in voiceovers if v.get("kodikTranslationId") is not None
    }
    known |= {found[0] for found in report.matched.values()}
    known |= {found for _current, found in report.mismatches.values()}
    known |= {tid for options in report.ambiguous.values() for tid, _title, _count in options}
    known |= set(report.conflicts)

    rest = [
        _entry(t) for t in translations
        if isinstance(t, dict) and t.get("id") is not None and int(t["id"]) not in known
    ]
    rest = [e for e in rest if e[2] > 0]
    return sorted(rest, key=lambda e: (-e[2], e[0]))[: max(limit, 0)]


def print_untracked(untracked: list[tuple[int, str, int]]) -> None:
    if not untracked:
        return
    print(f"\nОзвучки Kodik, которых нет в справочнике (по числу тайтлов), {len(untracked)}:")
    for tid, title, count in untracked:
        print(f"  + {tid:<6} «{title}» ({count} тайтлов)")
    print('  (чтобы подключить: допишите в voiceovers.json {"title": "...", "kodikTranslationId": ID}, затем sync-voiceovers и sync-dubs)')


def fetch_translations(
    token: str,
    types: str = DEFAULT_TYPES,
    fetch: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Все озвучки Kodik (идёт по next_page, если он есть)."""
    fetch = fetch or _request_url
    url: str | None = f"{TRANSLATIONS_URL}?{urllib.parse.urlencode({'token': token, 'types': types})}"
    results: list[dict[str, Any]] = []
    guard = 0
    while url and guard < 200:
        guard += 1
        data = fetch(url)
        results.extend(data.get("results") or [])
        url = data.get("next_page") or None
    return results


def print_report(voiceovers: list[dict[str, Any]], report: MatchReport) -> None:
    titles = {v["slug"]: v["title"] for v in voiceovers}
    print(f"\nНайдено: {len(report.matched)} из {len(voiceovers)}")
    for slug, (tid, ktitle, count, how) in report.matched.items():
        print(f"  ✓ {titles[slug]:<26} → {tid:<6} «{ktitle}» ({count} тайтлов; {how})")

    if report.ambiguous:
        print(f"\nНеоднозначно ({len(report.ambiguous)}) — выберите id вручную (kodikTranslationId в файле):")
        for slug, options in report.ambiguous.items():
            print(f"  ? {titles[slug]}")
            for tid, ktitle, count in options:
                print(f"      {tid:<6} «{ktitle}» ({count} тайтлов)")

    if report.conflicts:
        print("\nОдин id претендует на несколько озвучек (не записаны):")
        for tid, slugs in report.conflicts.items():
            print(f"  ! {tid}: {', '.join(titles[s] for s in slugs)}")

    if report.mismatches:
        print("\nРасхождение с id, уже записанным в файле (не изменено):")
        for slug, (current, found) in report.mismatches.items():
            print(f"  ≠ {titles[slug]}: в файле {current}, в Kodik найдено {found}")

    missing = report.not_found
    if missing:
        print(f"\nНе найдено ({len(missing)}):")
        for slug in missing:
            hint = report.suggestions.get(slug)
            line = f"  – {titles[slug]}"
            if hint:
                line += "   похожие: " + "; ".join(f"{t} «{n}» ({c})" for t, n, c in hint)
            print(line)
        print("  (добавьте у записи \"aliases\": [\"название в Kodik\"] и повторите)")


def run(args: argparse.Namespace) -> None:
    from .voiceovers import load_voiceovers

    path = args.file
    raw = load_json(path)  # сырые записи — чтобы при записи сохранить все поля (aliases и др.)
    voiceovers = load_voiceovers(path)  # проверенные, в том же порядке
    for original, entry in zip(raw, voiceovers):
        if original.get("aliases"):
            entry["aliases"] = original["aliases"]

    translations = fetch_translations(args.token or kodik_token(), args.types)
    print(f"Озвучек в Kodik ({args.types}): {len(translations)}")
    report = match_voiceovers(voiceovers, translations, pick_largest=args.pick_largest)
    print_report(voiceovers, report)
    print_untracked(untracked_translations(voiceovers, translations, report, getattr(args, "untracked", 15)))

    if not args.write:
        print("\nФайл не изменён (добавьте --write, чтобы записать найденные id в voiceovers.json).")
        return

    written = 0
    for original, entry in zip(raw, voiceovers):
        found = report.matched.get(entry["slug"])
        if found and original.get("kodikTranslationId") != found[0]:
            original["kodikTranslationId"] = found[0]
            written += 1
    save_json(path, raw)
    print(f"\nЗаписано id в {path}: {written}. Дальше: python pipeline.py sync-voiceovers")
