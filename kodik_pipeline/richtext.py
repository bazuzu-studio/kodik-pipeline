"""Построение минимальной Lexical richText-структуры для поля
content.description (jsonb), которое ожидает Payload."""

from __future__ import annotations

from typing import Any


def build_richtext(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    return {
        "root": {
            "type": "root",
            "format": "",
            "indent": 0,
            "version": 1,
            "direction": "ltr",
            "children": [
                {
                    "type": "paragraph",
                    "format": "",
                    "indent": 0,
                    "version": 1,
                    "children": [
                        {
                            "type": "text",
                            "detail": 0,
                            "format": 0,
                            "mode": "normal",
                            "style": "",
                            "text": text,
                            "version": 1,
                        }
                    ],
                }
            ],
        }
    }
