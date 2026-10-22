"""The login in front of the web UI and its API, and CORS (murdock/api/auth.py)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from murdock.api.app import create_app
from murdock.config import Settings

PASSWORD = "correct horse battery staple"
UI = "https://om.example.ts.net"


def _client(**env) -> TestClient:
    settings = Settings(**env)
    speakers = SimpleNamespace(list_speakers=lambda: [])
    context = SimpleNamespace(settings=settings, speakers=speakers)
    return TestClient(create_app(context), base_url="https://testserver")


def test_without_a_password_every_route_is_open_as_upstream():
    client = _client()
    assert client.get("/api/health").status_code == 200


def test_an_api_route_without_the_login_is_a_401():
    client = _client(web_password=PASSWORD)
    for path in ("/api/health", "/api/backup", "/api/settings"):
        res = client.get(path)
        assert res.status_code == 401, path
        assert res.json() == {"detail": "login required"}
    assert client.put("/api/stt-services/roles", json={}).status_code == 401
    assert client.patch("/api/settings", json={}).status_code == 401


def test_the_ui_without_the_login_goes_to_the_login_page():
    client = _client(web_password=PASSWORD)
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/login"
    page = client.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.text


def test_a_wrong_password_sets_no_cookie():
    client = _client(web_password=PASSWORD)
    res = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert res.status_code == 401
    assert "set-cookie" not in res.headers
    assert client.get("/api/health").status_code == 401


def test_the_login_sets_a_strict_session_cookie_that_opens_the_api():
    client = _client(web_password=PASSWORD)
    res = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert res.status_code == 303
    cookie = res.headers["set-cookie"].lower()
    for part in ("httponly", "samesite=strict", "secure", "path=/"):
        assert part in cookie
    assert "max-age" not in cookie and "expires" not in cookie
    assert client.get("/api/health").status_code == 200


def test_a_cookie_made_with_another_password_is_refused():
    other = _client(web_password="the old one")
    other.post("/login", data={"password": "the old one"})
    client = _client(web_password=PASSWORD)
    client.cookies = other.cookies
    assert client.get("/api/health").status_code == 401


def test_the_password_as_a_bearer_token_opens_the_api():
    client = _client(web_password=PASSWORD)
    assert client.get("/api/health", headers={"Authorization": f"Bearer {PASSWORD}"}).status_code == 200
    assert client.get("/api/health", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_logout_clears_the_cookie():
    client = _client(web_password=PASSWORD)
    client.post("/login", data={"password": PASSWORD})
    client.get("/logout")
    assert client.get("/api/health").status_code == 401


def test_no_origin_is_allowed_by_default():
    client = _client(web_password=PASSWORD)
    res = client.get("/api/health", headers={"Origin": "https://evil.example", "Authorization": f"Bearer {PASSWORD}"})
    assert "access-control-allow-origin" not in res.headers


def test_another_origin_is_refused_and_the_listed_one_allowed():
    client = _client(web_password=PASSWORD, web_cors_origins=UI)
    pre = {"Access-Control-Request-Method": "GET"}
    evil = client.options("/api/backup", headers={"Origin": "https://evil.example", **pre})
    assert evil.status_code == 400
    assert "access-control-allow-origin" not in evil.headers
    ok = client.options("/api/backup", headers={"Origin": UI, **pre})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == UI
    simple = client.get("/api/backup", headers={"Origin": "https://evil.example"})
    assert simple.status_code == 401
    assert "access-control-allow-origin" not in simple.headers
