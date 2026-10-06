"""SEO-метаданные (плагин @payloadcms/plugin-seo в CMS).

Плагин добавляет в content группу `meta`: колонки content.meta_title /
meta_description / meta_image_id и их копии в _content_v (version_meta_*).
Колонки создаёт миграция CMS 20261006_120000_add_seo_meta.

Пайплайн сам заполняет SEO-поля данными из Kodik API:
  - meta_title       ← titleRu (+ год) + «смотреть онлайн», до 60 символов;
  - meta_description ← описание из API (очищенное, до 155 символов), а если
                       описания нет — шаблон из названия, года и жанров;
  - meta_image_id    ← постер (ставит шаг `posters`).
Значения пишутся только в ПУСТЫЕ поля — отредактированное вручную в админке
не затирается. Кроме того, `load` пересоздаёт строки _content_v, поэтому после
этого значения копируются из content в версии (админка показывает именно
последнюю версию). Если колонок нет (миграция CMS не применена) — шаг
пропускается.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .db import table_columns

_FIELDS = ("meta_title", "meta_description", "meta_image_id")

TITLE_MAX = 60  # рекомендуемый предел плагина для title
DESCRIPTION_MAX = 155  # рекомендуемый предел для description
TITLE_SUFFIX = " — смотреть онлайн"

_TAG_RE = re.compile(r"<[^>]+>")
_BBCODE_RE = re.compile(r"\[/?[a-zA-Z_]+(?:=[^\]]*)?\]")  # [character=1]..[/character]
_SOURCE_RE = re.compile(r"\(?\s*(?:Источник|Source)\s*:[^)]*\)?\s*$", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def clean_text(text: Any) -> str:
    """Убирает HTML/BBCode-разметку, пометку об источнике и лишние пробелы."""
    if not text:
        return ""
    value = _TAG_RE.sub(" ", str(text))
    value = _BBCODE_RE.sub("", value)
    value = _SPACE_RE.sub(" ", value).strip()
    return _SOURCE_RE.sub("", value).strip()


def truncate(text: str, limit: int) -> str:
    """Обрезает по границе слова, добавляя «…», чтобы длина не превышала limit."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return (cut or text[: limit - 1]) + "…"


def build_meta_title(rec: dict[str, Any]) -> str | None:
    name = clean_text(rec.get("titleRu") or rec.get("titleEn"))
    if not name:
        return None
    year = rec.get("releaseYear")
    for candidate in (
        f"{name} ({year}){TITLE_SUFFIX}" if year else None,
        f"{name}{TITLE_SUFFIX}",
    ):
        if candidate and len(candidate) <= TITLE_MAX:
            return candidate
    return truncate(name, TITLE_MAX)


def build_meta_description(rec: dict[str, Any]) -> str | None:
    text = clean_text(rec.get("description"))
    if text:
        return truncate(text, DESCRIPTION_MAX)
    name = clean_text(rec.get("titleRu") or rec.get("titleEn"))
    if not name:
        return None
    year = f" ({rec['releaseYear']})" if rec.get("releaseYear") else ""
    kind = "фильм" if rec.get("type") == "movie" else "сериал"
    genres = [g for g in (rec.get("genres") or []) if isinstance(g, str) and g.strip()][:3]
    genre_part = f" Жанр: {', '.join(g.strip().lower() for g in genres)}." if genres else ""
    return truncate(f"Смотрите {kind} «{name}»{year} онлайн в хорошем качестве.{genre_part}", DESCRIPTION_MAX)


@dataclass(frozen=True)
class SeoSupport:
    content: bool
    version: bool

    @property
    def enabled(self) -> bool:
        return self.content and self.version


def detect_seo_support(cur) -> SeoSupport:
    content_cols = table_columns(cur, "content")
    version_cols = table_columns(cur, "_content_v")
    return SeoSupport(
        content=all(f in content_cols for f in _FIELDS),
        version=all(f"version_{f}" in version_cols for f in _FIELDS),
    )


def copy_seo_to_versions(cur, content_id: int, support: SeoSupport) -> None:
    """Копирует content.meta_* в новые строки _content_v этой записи."""
    if not support.enabled:
        return
    cur.execute(
        """
        UPDATE _content_v v SET
            version_meta_title = c.meta_title,
            version_meta_description = c.meta_description,
            version_meta_image_id = c.meta_image_id
        FROM content c
        WHERE c.id = %s AND v.parent_id = c.id
        """,
        (content_id,),
    )


def set_seo_meta(cur, content_id: int, rec: dict[str, Any], support: SeoSupport) -> None:
    """Заполняет ПУСТЫЕ meta_title / meta_description в content данными из API."""
    if not support.content:
        return
    cur.execute(
        """
        UPDATE content SET
            meta_title = COALESCE(NULLIF(meta_title, ''), %s),
            meta_description = COALESCE(NULLIF(meta_description, ''), %s)
        WHERE id = %s
        """,
        (build_meta_title(rec), build_meta_description(rec), content_id),
    )


def set_seo_image(cur, content_id: int, media_id: int, support: SeoSupport) -> None:
    """Ставит постер как meta-картинку, если она ещё не задана (content и версии)."""
    if not support.enabled:
        return
    cur.execute(
        "UPDATE content SET meta_image_id = COALESCE(meta_image_id, %s) WHERE id = %s",
        (media_id, content_id),
    )
    cur.execute(
        "UPDATE _content_v SET version_meta_image_id = COALESCE(version_meta_image_id, %s) "
        "WHERE parent_id = %s",
        (media_id, content_id),
    )
