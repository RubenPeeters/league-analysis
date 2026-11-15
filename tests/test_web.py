"""Tests for src/web.py with Discord's endpoints mocked by httpx.MockTransport; no network."""

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import web

ENV = {
    "DISCORD_CLIENT_ID": "client-1",
    "DISCORD_CLIENT_SECRET": "client-secret",
    "DISCORD_GUILD_ID": "guild-1",
    "DISCORD_ROLE_ID": "role-1",
    "SESSION_SECRET": "session-secret",
    "PUBLIC_URL": "https://scout.example/",
}
USER = {"id": "42", "username": "ruben", "global_name": "Ruben"}


def discord(member_status=200, roles=("role-1",), calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if request.url.path == "/api/v10/oauth2/token":
            assert parse_qs(request.content.decode())["code"] == ["the-code"]
            return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer"})
        assert request.url.path == "/api/v10/users/@me/guilds/guild-1/member"
        assert request.headers["Authorization"] == "Bearer tok"
        if member_status != 200:
            return httpx.Response(member_status, json={"message": "Unknown Guild", "code": 10004})
        return httpx.Response(200, json={"user": USER, "roles": list(roles)})

    return httpx.MockTransport(handler)


def client(transport=None):
    # https, so the Secure cookies are sent back
    app = web.create_app(ENV, transport=transport or discord())
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def login(c, state=None):
    """Start the login, then come back from Discord with state (default: the one sent)."""
    start = c.get("/auth/discord")
    assert start.status_code == 303
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["scope"] == ["identify guilds.members.read"]
    assert query["redirect_uri"] == ["https://scout.example/auth/discord/callback"]
    sent = query["state"][0]
    return c.get("/auth/discord/callback", params={"code": "the-code", "state": state or sent})


def test_member_with_role_gets_a_session():
    c = client()
    response = login(c)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = next(h for h in response.headers.get_list("set-cookie") if h.startswith("session="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=86400" in cookie
    assert "tok" not in c.cookies.get("session")
    home = c.get("/")
    assert home.status_code == 200 and "Ruben" in home.text
    assert c.get("/login").headers["location"] == "/"
    c.get("/logout")
    assert c.get("/").headers["location"] == "/login"


def test_member_without_role_is_refused():
    c = client(discord(roles=("other-role",)))
    response = login(c)
    assert response.status_code == 403 and "invitation" in response.text
    assert "session" not in c.cookies
    assert c.get("/").headers["location"] == "/login"


def test_not_a_member_is_refused():
    c = client(discord(member_status=404))
    response = login(c)
    assert response.status_code == 403 and "invitation" in response.text
    assert "session" not in c.cookies


def test_wrong_state_is_refused_before_calling_discord():
    calls = []
    c = client(discord(calls=calls))
    response = login(c, state="forged")
    assert response.status_code == 400
    assert calls == [] and "session" not in c.cookies


def test_callback_without_state_cookie_is_refused():
    calls = []
    c = client(discord(calls=calls))
    response = c.get("/auth/discord/callback", params={"code": "the-code", "state": "x"})
    assert response.status_code == 400 and calls == []


def test_expired_session_is_sent_to_login(monkeypatch):
    c = client()
    sessions = web.serializer(ENV["SESSION_SECRET"], "session")
    now = time.time()
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: now - web.SESSION_MAX_AGE - 1)
        old = sessions.dumps({"id": "42", "name": "Ruben"})
    fresh = sessions.dumps({"id": "42", "name": "Ruben"})
    for cookie, expected in ((old, 303), (fresh, 200), ("garbage", 303)):
        c.cookies.set("session", cookie)
        assert c.get("/").status_code == expected


@pytest.mark.parametrize("key", web.CONFIG_KEYS)
def test_missing_configuration_refuses_to_start(key):
    with pytest.raises(RuntimeError, match=key):
        web.create_app({k: v for k, v in ENV.items() if k != key})


def test_healthz_and_style_need_no_login():
    c = client()
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/static/style.css").status_code == 200
