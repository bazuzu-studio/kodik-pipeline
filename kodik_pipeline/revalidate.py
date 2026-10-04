"""Сброс кэша сайта (apps/web) после изменения каталога.

Прямые SQL-вставки не вызывают хуки Payload, поэтому CMS не знает, что контент
изменился, а сайт кэширует ответы CMS на 60 секунд. После успешного `load` /
`update-ongoing` пайплайн сам дёргает `POST <REVALIDATE_URL>/api/revalidate`.

Переменные окружения (обе обязательны, иначе шаг тихо пропускается):
    REVALIDATE_URL     — адрес сайта, доступный из контейнера пайплайна,
                         например http://movhub-web:3000 (alias в dokploy-network)
                         или https://otakuum.ru
    REVALIDATE_SECRET  — тот же секрет, что у сайта и CMS

Ошибки сброса кэша НИКОГДА не роняют пайплайн: данные уже в БД, сайт
в худшем случае обновится по таймеру через минуту.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request


def notify_frontend(*, timeout: float = 10.0) -> bool:
    base = os.environ.get("REVALIDATE_URL", "").strip().rstrip("/")
    secret = os.environ.get("REVALIDATE_SECRET", "").strip()
    if not base or not secret:
        print("Сброс кэша сайта пропущен: REVALIDATE_URL / REVALIDATE_SECRET не заданы.")
        return False
    if not base.startswith(("http://", "https://")):
        print(f"Сброс кэша сайта пропущен: REVALIDATE_URL должен начинаться с http(s)://, получено {base!r}")
        return False

    request = urllib.request.Request(
        f"{base}/api/revalidate",
        data=b'{"tag":"content"}',
        method="POST",
        headers={"content-type": "application/json", "x-revalidate-secret": secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (схема проверена выше)
            ok = 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        print(f"Сброс кэша сайта: сайт ответил {exc.code} (проверьте REVALIDATE_SECRET).")
        return False
    except (urllib.error.URLError, OSError) as exc:
        print(f"Сброс кэша сайта: сайт недоступен ({exc}).")
        return False

    print("Кэш сайта сброшен." if ok else "Сброс кэша сайта: неожиданный ответ.")
    return ok
