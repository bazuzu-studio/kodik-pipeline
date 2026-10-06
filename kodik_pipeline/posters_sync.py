"""Перенос картинок (poster / backdrop) из content в пересозданные _content_v.

`load` удаляет и заново создаёт версии, а poster_id / backdrop_id в INSERT версий
не входят — без этого шага админка (показывает последнюю версию) теряла бы
картинки до следующего запуска `posters`.
"""
from __future__ import annotations

from .db import table_columns

_PAIRS = (("poster_id", "version_poster_id"), ("backdrop_id", "version_backdrop_id"))


def copy_images_to_versions(cur, content_id: int) -> None:
    content_cols = table_columns(cur, "content")
    version_cols = table_columns(cur, "_content_v")
    pairs = [(c, v) for c, v in _PAIRS if c in content_cols and v in version_cols]
    if not pairs:
        return
    assignments = ", ".join(f"{v} = c.{c}" for c, v in pairs)
    cur.execute(
        f"UPDATE _content_v v SET {assignments} FROM content c "
        "WHERE c.id = %s AND v.parent_id = c.id",
        (content_id,),
    )
