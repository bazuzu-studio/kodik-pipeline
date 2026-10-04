import urllib.error

from kodik_pipeline import revalidate


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_skipped_without_config(monkeypatch, capsys):
    monkeypatch.delenv("REVALIDATE_URL", raising=False)
    monkeypatch.delenv("REVALIDATE_SECRET", raising=False)
    assert revalidate.notify_frontend() is False
    assert "пропущен" in capsys.readouterr().out


def test_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setenv("REVALIDATE_URL", "file:///etc/passwd")
    monkeypatch.setenv("REVALIDATE_SECRET", "s")
    monkeypatch.setattr(revalidate.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен вызываться")))
    assert revalidate.notify_frontend() is False


def test_posts_secret_header(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["secret"] = request.get_header("X-revalidate-secret")
        return FakeResponse(200)

    monkeypatch.setenv("REVALIDATE_URL", "http://movhub-web:3000/")
    monkeypatch.setenv("REVALIDATE_SECRET", "topsecret")
    monkeypatch.setattr(revalidate.urllib.request, "urlopen", fake_urlopen)
    assert revalidate.notify_frontend() is True
    assert seen == {"url": "http://movhub-web:3000/api/revalidate", "method": "POST", "secret": "topsecret"}


def test_errors_never_raise(monkeypatch):
    monkeypatch.setenv("REVALIDATE_URL", "http://movhub-web:3000")
    monkeypatch.setenv("REVALIDATE_SECRET", "s")

    def unauthorized(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(revalidate.urllib.request, "urlopen", unauthorized)
    assert revalidate.notify_frontend() is False

    def down(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(revalidate.urllib.request, "urlopen", down)
    assert revalidate.notify_frontend() is False
