"""A login in front of the web UI and its API.

Off unless WEB_PASSWORD is set, so upstream's behaviour (and the add-on behind
HA's ingress) is unchanged. With it set, every path but /login and /logout
needs either the session cookie the login sets, or the password as a bearer
token (``Authorization: Bearer <password>``) for scripts.

The cookie is ``<nonce>.<HMAC-SHA256(password, nonce)>``: nothing is stored on
the server, a restart keeps a browser logged in, and a new password logs every
browser out. It is HttpOnly, SameSite=Strict (another site's page never sends
it), Secure when the request came over HTTPS, and has no Max-Age, so it lasts
one browser session.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SESSION_COOKIE = "murdock_session"
OPEN_PATHS = frozenset({"/login", "/logout"})


def _sign(password: str, nonce: str) -> str:
    return hmac.new(password.encode(), b"murdock-session:" + nonce.encode(), hashlib.sha256).hexdigest()


def new_session(password: str) -> str:
    nonce = secrets.token_urlsafe(16)
    return f"{nonce}.{_sign(password, nonce)}"


def valid_session(password: str, cookie: Optional[str]) -> bool:
    if not cookie or "." not in cookie:
        return False
    nonce, _, sig = cookie.partition(".")
    return hmac.compare_digest(sig.encode(), _sign(password, nonce).encode())


def valid_password(password: str, given: Optional[str]) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), password.encode())


def authorized(request: Request, password: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and valid_password(password, token.strip()):
        return True
    return valid_session(password, request.cookies.get(SESSION_COOKIE))


def _secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Murdock: log in</title>
<style>
body{{font-family:system-ui,sans-serif;display:flex;min-height:100vh;margin:0;align-items:center;justify-content:center;background:#111;color:#eee}}
form{{display:flex;flex-direction:column;gap:.75rem;width:18rem}}
input,button{{font:inherit;padding:.5rem;border-radius:.4rem;border:1px solid #555;background:#222;color:#eee}}
button{{cursor:pointer}}
p{{margin:0;color:#f88}}
</style></head>
<body><form method="post" action="login">
<h1>Murdock</h1>
{error}
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
<button type="submit">Log in</button>
</form></body></html>
"""


def login_page(error: str = "", status_code: int = 200) -> HTMLResponse:
    err = f"<p>{html.escape(error)}</p>" if error else ""
    return HTMLResponse(_LOGIN_PAGE.format(error=err), status_code=status_code)


def install(app, password: str) -> None:
    """Puts the login in front of every route of ``app``."""

    @app.get("/login", include_in_schema=False)
    async def login_form(request: Request) -> Response:
        if authorized(request, password):
            return RedirectResponse("./", status_code=303)
        return login_page()

    @app.post("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        form = await request.form()
        given = form.get("password")
        if not isinstance(given, str) or not valid_password(password, given):
            return login_page("Wrong password.", status_code=401)
        res = RedirectResponse("./", status_code=303)
        res.set_cookie(
            SESSION_COOKIE, new_session(password),
            httponly=True, samesite="strict", secure=_secure(request), path="/",
        )
        return res

    @app.get("/logout", include_in_schema=False)
    async def logout() -> Response:
        res = RedirectResponse("login", status_code=303)
        res.delete_cookie(SESSION_COOKIE, path="/")
        return res

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS or authorized(request, password):
            return await call_next(request)
        if path.startswith("/api/") or request.method != "GET":
            return JSONResponse(
                {"detail": "login required"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return RedirectResponse("/login", status_code=303)
