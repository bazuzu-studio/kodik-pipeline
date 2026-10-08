"""
Сопоставление сезонов Kodik со строками таблицы `seasons` — без дублей.

Раньше сезон искался по (content_id, season_number). Номера сезонов франшизы
вычисляются заново при каждом `fetch` (см. franchise.py): если у сезона
изменился номер (в франшизу добавили запись с более ранним годом, «часть»
получила тот же номер, что и сезон), старая строка не находилась и создавалась
НОВАЯ — у одного content оказывалось два сезона с одними и теми же сериями.

Теперь для каждого content:
  1. сезон ищется по номеру (для update-ongoing — нет, см. exact_first);
  2. не найденный сезон сопоставляется с оставшейся строкой content и при
     renumber=True получает новый номер, а не новую строку;
  3. новая строка создаётся, только если подходящей нет;
  4. после загрузки серий лишние строки-дубли удаляются (см. _collapse).

Удаляются только ДУБЛИ — строки, которые ничего не теряют:
  - строки с тем же номером сезона, что у сопоставленной (серии переносятся);
  - если запись Kodik — ровно один сезон: другие строки content, все серии
    которых уже есть в сопоставленном сезоне (или серий нет совсем).
Всё остальное остаётся на месте и попадает в stats.kept_extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

FETCH_SEASONS_SQL = """
SELECT s.id, s.season_number, count(e.id)
FROM seasons s
LEFT JOIN episodes e ON e.season_id = s.id
WHERE s.content_id = %s
GROUP BY s.id, s.season_number
ORDER BY s.season_number, s.id
"""

INSERT_SEASON_SQL = """
INSERT INTO seasons (content_id, season_number, title, release_year)
VALUES (%s, %s, %s, %s)
RETURNING id
"""

UPDATE_SEASON_SQL = """
UPDATE seasons
SET season_number = %s,
    title = COALESCE(%s, title),
    release_year = COALESCE(%s, release_year),
    updated_at = now()
WHERE id = %s
"""


@dataclass
class SeasonStats:
    created: int = 0       # новых строк seasons
    renumbered: int = 0    # сезонов, получивших новый номер (вместо дубля)
    merged: int = 0        # удалённых строк-дублей
    kept_extra: int = 0    # лишних строк, которые НЕ удалены (в них есть уникальные серии)

    def changed(self) -> bool:
        return bool(self.created or self.renumbered or self.merged)


@dataclass
class _Row:
    id: int
    number: int
    episodes: int


def _episode_numbers(cur, season_id: int) -> set[int]:
    cur.execute("SELECT episode_number FROM episodes WHERE season_id = %s", (season_id,))
    return {row[0] for row in cur.fetchall() if row[0] is not None}


def _delete_season(cur, season_id: int) -> None:
    cur.execute("DELETE FROM episodes WHERE season_id = %s", (season_id,))
    cur.execute("DELETE FROM seasons WHERE id = %s", (season_id,))


def _merge_same_number(cur, keep_id: int, extra_id: int) -> None:
    """Сезон-дубль с тем же номером: недостающие серии переносим, остальное удаляем."""
    cur.execute(
        """
        UPDATE episodes e SET season_id = %s
        WHERE e.season_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM episodes k
              WHERE k.season_id = %s AND k.episode_number = e.episode_number
          )
        """,
        (keep_id, extra_id, keep_id),
    )
    _delete_season(cur, extra_id)


def _best_row(rows: list[_Row]) -> _Row:
    """Самая «настоящая» строка: больше серий, затем больший номер."""
    return max(rows, key=lambda r: (r.episodes, r.number, r.id))


def sync_content_seasons(
    cur,
    content_id: int,
    rec_seasons: list[dict[str, Any]],
    on_season: Callable[[int, dict[str, Any]], None],
    *,
    exact_first: bool,
    renumber: bool,
    stats: SeasonStats,
) -> None:
    """Приводит сезоны content в соответствие с записью Kodik.

    on_season(season_id, season) вызывается для каждого сезона записи и должен
    загрузить/обновить его серии.

    exact_first — сначала искать строку по номеру сезона. Для load: номера
        посчитаны для всей франшизы и совпадают с БД. Для update-ongoing —
        False: там номер «сырой» (у Kodik каждый сезон «1»), и для записи из
        одного сезона берётся подходящая строка content независимо от номера.
    renumber — при сопоставлении «не по номеру» обновлять season_number.
        False, если номера франшизы нельзя считать надёжными (см.
        renumber_allowed): строка всё равно переиспользуется, но номер не меняется.
    """
    wanted = [s for s in rec_seasons if s.get("seasonNumber") is not None]
    if not wanted:
        return
    single = len(wanted) == 1

    cur.execute(FETCH_SEASONS_SQL, (content_id,))
    rows = [_Row(r[0], r[1], r[2]) for r in cur.fetchall()]
    by_number: dict[int, list[_Row]] = {}
    for row in rows:
        by_number.setdefault(row.number, []).append(row)

    paired: dict[int, _Row] = {}              # индекс в wanted -> строка БД
    used: set[int] = set()
    pending: list[int] = []

    # 1. Точное совпадение номера.
    for idx, season in enumerate(wanted):
        match = None
        if exact_first or not single:
            candidates = [r for r in by_number.get(season["seasonNumber"], []) if r.id not in used]
            match = candidates[0] if candidates else None
        if match:
            paired[idx] = match
            used.add(match.id)
        else:
            pending.append(idx)

    # 2. Оставшиеся сезоны — на оставшиеся строки (вместо создания дубля).
    free = [r for r in rows if r.id not in used]
    if pending and free:
        if single:
            row = _best_row(free)
            paired[pending[0]] = row
            used.add(row.id)
            pending = []
        elif renumber and len(pending) == len(free):
            for idx, row in zip(pending, sorted(free, key=lambda r: (r.number, r.id))):
                paired[idx] = row
                used.add(row.id)
            pending = []

    # 3. Запись в БД. Порядок — как в данных: серии каждого сезона грузятся сразу.
    season_ids: dict[int, int] = {}
    for idx, season in enumerate(wanted):
        number = season["seasonNumber"]
        row = paired.get(idx)
        if row is None:
            cur.execute(INSERT_SEASON_SQL, (content_id, number, season.get("title"), season.get("releaseYear")))
            season_id = cur.fetchone()[0]
            stats.created += 1
        else:
            season_id = row.id
            new_number = number if (renumber and row.number != number) else row.number
            if new_number != row.number:
                stats.renumbered += 1
            cur.execute(
                UPDATE_SEASON_SQL,
                (new_number, season.get("title"), season.get("releaseYear"), season_id),
            )
        season_ids[idx] = season_id
        on_season(season_id, season)

    # 4. Дубли.
    _collapse(cur, rows, paired, season_ids, wanted, single, stats)


def _collapse(
    cur,
    rows: list[_Row],
    paired: dict[int, _Row],
    season_ids: dict[int, int],
    wanted: list[dict[str, Any]],
    single: bool,
    stats: SeasonStats,
) -> None:
    used_ids = {row.id for row in paired.values()}
    keep_by_number = {row.number: season_ids[idx] for idx, row in paired.items()}

    for row in rows:
        if row.id in used_ids:
            continue

        # Тот же номер, что у сопоставленной строки → это точный дубль.
        keep_id = keep_by_number.get(row.number)
        if keep_id is not None:
            _merge_same_number(cur, keep_id, row.id)
            stats.merged += 1
            continue

        # Запись Kodik = один сезон, значит других сезонов у content быть не должно.
        # Удаляем только если ничего не теряем.
        if single:
            kept_id = season_ids[0]
            if _episode_numbers(cur, row.id) <= _episode_numbers(cur, kept_id):
                _delete_season(cur, row.id)
                stats.merged += 1
                continue

        stats.kept_extra += 1


# ─── Защита номеров франшизы ─────────────────────────────────────

def franchise_members(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    """franchiseId -> Kodik ID всех записей франшизы из загружаемых данных
    (вместе с пропущенными дублями по shikimori_id)."""
    members: dict[str, set[str]] = {}
    for rec in records:
        fid = rec.get("franchiseId")
        if not fid or rec.get("type") != "series":
            continue
        ids = members.setdefault(str(fid), set())
        if rec.get("kodikId") not in (None, ""):
            ids.add(str(rec["kodikId"]))
        ids.update(str(x) for x in rec.get("duplicateKodikIds") or [])
    return members


def renumber_allowed(
    cur,
    rec: dict[str, Any],
    members: dict[str, set[str]],
    cache: dict[str, bool],
    has_franchise_column: bool,
) -> bool:
    """Можно ли менять season_number у уже загруженных сезонов этой записи.

    Номер сезона франшизы считается по записям, которые пришли в ЭТОМ запуске.
    Если в БД есть записи франшизы, которых в данных нет (неполная выдача,
    --max-pages, сезон пропал у Kodik), номера посчитаны неверно — тогда
    существующие строки переиспользуются, но не перенумеровываются.
    """
    fid = rec.get("franchiseId")
    if not fid:
        return True
    if not has_franchise_column:
        return False
    fid = str(fid)
    if fid not in cache:
        cur.execute(
            "SELECT kodik_id FROM content WHERE franchise_id = %s AND type = 'series'",
            (fid,),
        )
        db_ids = {str(row[0]) if row[0] not in (None, "") else None for row in cur.fetchall()}
        cache[fid] = None not in db_ids and db_ids <= members.get(fid, set())
        if not cache[fid]:
            print(
                f"  [WARN] франшиза {fid}: в БД есть записи, которых нет в данных "
                "(или без kodik_id) — номера сезонов существующих записей не меняются"
            )
    return cache[fid]
