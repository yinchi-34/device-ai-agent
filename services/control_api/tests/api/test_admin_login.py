import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.document.admin_router import require_admin


@pytest.fixture
def login_app(monkeypatch):
    monkeypatch.setenv("DEVICE_ADMIN_USERNAME", "reviewer")
    monkeypatch.setenv("DEVICE_ADMIN_PASSWORD", "login-test-secret")
    app = create_app()

    async def auth_check(actor: str = Depends(require_admin)):
        return {"actor": actor}

    app.add_api_route("/admin/auth-check", auth_check)
    return app


@pytest.fixture
async def login_client(login_app):
    async with AsyncClient(
        transport=ASGITransport(app=login_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        yield client


async def _csrf_token(client):
    response = await client.get("/admin/login")
    assert response.status_code == 200
    token = client.cookies.get("admin_csrf")
    assert token
    return token


async def _login(client, **fields):
    token = await _csrf_token(client)
    return await client.post(
        "/admin/login",
        data={
            "username": "reviewer",
            "password": "login-test-secret",
            "next": "/admin/documents",
            "_csrf_token": token,
            **fields,
        },
    )


async def test_unauthenticated_admin_get_redirects_without_browser_challenge(
    login_client,
):
    response = await login_client.get("/admin/documents?status=failed&page=2")

    assert response.status_code == 303
    redirect = urlsplit(response.headers["location"])
    assert redirect.path == "/admin/login"
    assert parse_qs(redirect.query)["next"] == [
        "/admin/documents?status=failed&page=2"
    ]
    assert "www-authenticate" not in response.headers


async def test_login_page_renders_chinese_form_with_signed_csrf(login_client):
    response = await login_client.get(
        "/admin/login", params={"next": "/admin/auth-check"}
    )

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "登录" in response.text
    for field in ("username", "password", "next", "_csrf_token"):
        assert f'name="{field}"' in response.text
    token = login_client.cookies.get("admin_csrf")
    assert token and token in response.text and "." in token
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_valid_login_sets_scoped_session_and_authenticates_actor(
    login_app, scheme
):
    async with AsyncClient(
        transport=ASGITransport(app=login_app), base_url=f"{scheme}://test"
    ) as client:
        response = await _login(client, next="/admin/auth-check?source=login")

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/auth-check?source=login"
        assert client.cookies.get("admin_session")
        session_cookie = next(
            cookie
            for cookie in response.headers.get_list("set-cookie")
            if cookie.startswith("admin_session=")
        )
        assert "HttpOnly" in session_cookie
        assert "SameSite=strict" in session_cookie
        assert "Path=/admin" in session_cookie
        assert "Max-Age=28800" in session_cookie
        assert ("Secure" in session_cookie) is (scheme == "https")
        authenticated = await client.get("/admin/auth-check")
        assert authenticated.status_code == 200
        assert authenticated.json() == {"actor": "reviewer"}


@pytest.mark.parametrize(
    "credentials",
    [
        {"password": "wrong-password-do-not-echo"},
        {"username": ""},
        {"password": ""},
        {"username": "", "password": ""},
    ],
)
async def test_invalid_login_stays_on_html_form_and_can_retry(
    login_client, credentials
):
    response = await _login(login_client, **credentials)

    assert response.status_code == 401
    assert "text/html" in response.headers["content-type"]
    assert 'name="password"' in response.text
    assert "错误" in response.text
    assert "www-authenticate" not in response.headers
    assert login_client.cookies.get("admin_session") is None
    assert "wrong-password-do-not-echo" not in response.text
    assert "login-test-secret" not in response.text
    retry = await _login(login_client)
    assert retry.status_code == 303


@pytest.mark.parametrize("submitted_token", [None, "forged-token", "forged.same"])
async def test_login_rejects_missing_or_forged_csrf(login_client, submitted_token):
    if submitted_token == "forged.same":
        login_client.cookies.set("admin_csrf", "forged.same", path="/admin")
    else:
        await _csrf_token(login_client)
    fields = {"username": "reviewer", "password": "login-test-secret"}
    if submitted_token is not None:
        fields["_csrf_token"] = submitted_token

    response = await login_client.post("/admin/login", data=fields)

    assert response.status_code == 403
    assert login_client.cookies.get("admin_session") is None


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "https://evil.example/admin/documents",
        "//evil.example/admin/documents",
        "/admin/login",
        "/admin/login?next=/admin/documents",
        "/admin/logout",
        "/admin/logout?next=/admin/documents",
        "http://[",
        "/admin/../outside",
        "/admin/%2e%2e/outside",
        "/admin/login/",
    ],
)
async def test_login_rejects_external_or_auth_loop_redirects(
    login_client, unsafe_next
):
    response = await _login(login_client, next=unsafe_next)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/documents"


async def test_logout_clears_session_and_protected_pages_require_login(login_client):
    assert (await _login(login_client)).status_code == 303
    token = login_client.cookies.get("admin_csrf")

    response = await login_client.post(
        "/admin/logout", data={"_csrf_token": token}
    )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"
    assert login_client.cookies.get("admin_session") is None
    assert "Max-Age=0" in response.headers["set-cookie"]
    protected = await login_client.get("/admin/auth-check")
    assert protected.status_code == 303
    assert urlsplit(protected.headers["location"]).path == "/admin/login"


async def test_logout_rejects_missing_csrf_without_ending_session(login_client):
    assert (await _login(login_client)).status_code == 303

    response = await login_client.post("/admin/logout")

    assert response.status_code == 403
    assert (await login_client.get("/admin/auth-check")).status_code == 200


async def test_tampered_session_cannot_authenticate(login_client):
    assert (await _login(login_client)).status_code == 303
    original = login_client.cookies.get("admin_session")
    tampered = ("A" if original[0] != "A" else "B") + original[1:]
    login_client.cookies.clear()
    login_client.cookies.set("admin_session", tampered, path="/admin")

    response = await login_client.get("/admin/auth-check")

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"


async def test_expired_session_cannot_authenticate(login_client, monkeypatch):
    assert (await _login(login_client)).status_code == 303
    session_token = login_client.cookies.get("admin_session")
    future = time.time() + 8 * 60 * 60 + 1
    monkeypatch.setattr(time, "time", lambda: future)
    # Keep sending the expired token to verify server-side expiry, rather than
    # allowing the client's cookie jar to discard it before the request.
    response = await login_client.get(
        "/admin/auth-check", headers={"cookie": f"admin_session={session_token}"}
    )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"


async def test_password_rotation_invalidates_existing_session(login_client, monkeypatch):
    assert (await _login(login_client)).status_code == 303
    monkeypatch.setenv("DEVICE_ADMIN_PASSWORD", "rotated-login-secret")

    response = await login_client.get("/admin/auth-check")

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"


async def test_explicit_basic_auth_remains_compatible_without_browser_challenge(
    login_client,
):
    valid = await login_client.get(
        "/admin/auth-check", auth=("reviewer", "login-test-secret")
    )
    assert valid.status_code == 200
    assert valid.json() == {"actor": "reviewer"}
    invalid = await login_client.get(
        "/admin/auth-check", auth=("reviewer", "wrong-password")
    )
    assert invalid.status_code == 401
    assert "www-authenticate" not in invalid.headers


async def test_valid_session_takes_priority_over_cached_invalid_basic_auth(login_client):
    assert (await _login(login_client)).status_code == 303

    response = await login_client.get(
        "/admin/auth-check", auth=("reviewer", "outdated-browser-password")
    )

    assert response.status_code == 200
    assert response.json() == {"actor": "reviewer"}


async def test_browser_with_cached_invalid_basic_auth_gets_login_page(login_client):
    response = await login_client.get(
        "/admin/auth-check",
        auth=("reviewer", "outdated-browser-password"),
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"
    assert "www-authenticate" not in response.headers


async def test_logout_is_not_undone_by_browser_cached_valid_basic_auth(login_client):
    assert (await _login(login_client)).status_code == 303
    token = login_client.cookies.get("admin_csrf")
    assert (
        await login_client.post("/admin/logout", data={"_csrf_token": token})
    ).status_code == 303

    response = await login_client.get(
        "/admin/auth-check",
        auth=("reviewer", "login-test-secret"),
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/admin/login"


async def test_unconfigured_admin_login_fails_closed(login_client, monkeypatch):
    monkeypatch.setenv("DEVICE_ADMIN_PASSWORD", "")

    for url in ("/admin/login", "/admin/documents"):
        response = await login_client.get(url)
        assert response.status_code == 503
        assert login_client.cookies.get("admin_session") is None
