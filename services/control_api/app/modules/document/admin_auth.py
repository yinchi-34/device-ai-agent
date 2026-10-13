import base64
import hashlib
import hmac
import json
import posixpath
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.core.config import Settings

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
security = HTTPBasic(auto_error=False)
_CSRF_COOKIE = "admin_csrf"
_SESSION_COOKIE = "admin_session"
_SESSION_SECONDS = 8 * 60 * 60


def _admin_settings() -> Settings:
    settings = Settings()
    if not settings.admin_password:
        raise HTTPException(503, "Admin review console is not configured")
    return settings


def _valid_credentials(username: str, password: str, settings: Settings) -> bool:
    username_matches = secrets.compare_digest(
        username.encode(), settings.admin_username.encode()
    )
    password_matches = secrets.compare_digest(
        password.encode(), settings.admin_password.encode()
    )
    return username_matches and password_matches


def _sign_value(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _new_csrf_token(secret: str) -> str:
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_sign_value(nonce, secret)}"


def _valid_csrf_token(token: str | None, secret: str) -> bool:
    if not token or len(token) > 256:
        return False
    nonce, separator, signature = token.partition(".")
    if not separator or not nonce or not signature:
        return False
    return secrets.compare_digest(
        signature.encode(), _sign_value(nonce, secret).encode()
    )


def _new_session_token(settings: Settings) -> str:
    payload = {
        "username": settings.admin_username,
        "expires_at": int(time.time()) + _SESSION_SECONDS,
        "nonce": secrets.token_urlsafe(24),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    signature = _sign_value(f"admin-session:{encoded}", settings.admin_password)
    return f"{encoded}.{signature}"


def _valid_session_token(token: str | None, settings: Settings) -> bool:
    if not token or len(token) > 2048:
        return False
    encoded, separator, signature = token.partition(".")
    expected = _sign_value(f"admin-session:{encoded}", settings.admin_password)
    if not separator or not secrets.compare_digest(signature.encode(), expected.encode()):
        return False
    try:
        payload = json.loads(
            base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError):
        return False
    if not isinstance(payload, dict):
        return False
    username = payload.get("username")
    expires_at = payload.get("expires_at")
    return (
        isinstance(username, str)
        and secrets.compare_digest(username.encode(), settings.admin_username.encode())
        and type(expires_at) is int
        and expires_at > time.time()
    )


def _safe_next_url(value: str) -> str:
    fallback = "/admin/documents"
    if any(ord(character) < 32 for character in value):
        return fallback
    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback
    decoded_path = unquote(parsed.path)
    path = posixpath.normpath(decoded_path)
    if (
        parsed.scheme
        or parsed.netloc
        or not value.startswith("/admin")
        or "\\" in decoded_path
        or not (path == "/admin" or path.startswith("/admin/"))
        or path in {"/admin/login", "/admin/logout"}
    ):
        return fallback
    return value


def _login_required(request: Request) -> HTTPException:
    if request.method not in {"GET", "HEAD"}:
        return HTTPException(401, "Admin login required")
    destination = request.url.path
    if request.url.query:
        destination += f"?{request.url.query}"
    location = "/admin/login?" + urlencode({"next": _safe_next_url(destination)})
    return HTTPException(303, "Admin login required", headers={"Location": location})


async def require_admin(request: Request) -> str:
    settings = _admin_settings()
    if _valid_session_token(request.cookies.get(_SESSION_COOKIE), settings):
        request.state.admin_user = settings.admin_username
        return settings.admin_username
    # Browsers can keep sending cached Basic credentials after logout. HTML
    # navigation uses only the form session; CLI/API clients may still use Basic.
    if "text/html" in request.headers.get("accept", "").lower():
        raise _login_required(request)
    try:
        credentials: HTTPBasicCredentials | None = await security(request)
    except HTTPException:
        raise HTTPException(401, "Invalid admin credentials") from None
    if credentials is not None:
        if _valid_credentials(credentials.username, credentials.password, settings):
            request.state.admin_user = settings.admin_username
            return settings.admin_username
        raise HTTPException(401, "Invalid admin credentials")
    raise _login_required(request)


async def require_admin_csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    settings = _admin_settings()
    cookie_token = request.cookies.get(_CSRF_COOKIE)
    submitted_token = request.headers.get("x-csrf-token")
    if submitted_token is None:
        form = await request.form()
        raw_token = form.get("_csrf_token")
        submitted_token = raw_token if isinstance(raw_token, str) else None
    if (
        not _valid_csrf_token(cookie_token, settings.admin_password)
        or submitted_token is None
        or not secrets.compare_digest(cookie_token.encode(), submitted_token.encode())
    ):
        raise HTTPException(403, "Invalid admin CSRF token")


def _render_admin_template(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    settings = _admin_settings()
    cookie_token = request.cookies.get(_CSRF_COOKIE)
    token = (
        cookie_token
        if _valid_csrf_token(cookie_token, settings.admin_password)
        else _new_csrf_token(settings.admin_password)
    )
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={
            **context,
            "csrf_token": token,
            "admin_user": getattr(request.state, "admin_user", None),
        },
        status_code=status_code,
    )
    if token != cookie_token:
        response.set_cookie(
            _CSRF_COOKIE,
            token,
            max_age=_SESSION_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/admin",
        )
    return response


router = APIRouter(prefix="/admin", include_in_schema=False)


@router.get("/login")
async def login_page(request: Request, next: str = "/admin/documents") -> Response:
    settings = _admin_settings()
    destination = _safe_next_url(next)
    if _valid_session_token(request.cookies.get(_SESSION_COOKIE), settings):
        return RedirectResponse(destination, status_code=303)
    return _render_admin_template(
        request, "admin/login.html", {"next_url": destination, "username": ""}
    )


@router.post("/login", dependencies=[Depends(require_admin_csrf)])
async def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next_url: str = Form(default="/admin/documents", alias="next"),
) -> Response:
    settings = _admin_settings()
    destination = _safe_next_url(next_url)
    if not _valid_credentials(username, password, settings):
        return _render_admin_template(
            request,
            "admin/login.html",
            {
                "next_url": destination,
                "username": username,
                "error": "用户名或密码错误，请重试。",
            },
            status_code=401,
        )
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        _SESSION_COOKIE,
        _new_session_token(settings),
        max_age=_SESSION_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/admin",
    )
    return response


@router.post("/logout", dependencies=[Depends(require_admin_csrf)])
async def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(_SESSION_COOKIE, path="/admin")
    response.delete_cookie(_CSRF_COOKIE, path="/admin")
    return response
