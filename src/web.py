"""Web service for the player lookup: Discord login for members of one server holding one role.

Run with `uvicorn --factory web:create_app --app-dir src`.
"""

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

CONFIG_KEYS = (
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "DISCORD_GUILD_ID",
    "DISCORD_ROLE_ID",
    "SESSION_SECRET",
    "PUBLIC_URL",
)
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
DISCORD_API = "https://discord.com/api/v10"
SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
# a removed role must stop working within a day, so the session is never extended
SESSION_MAX_AGE = 24 * 60 * 60
STATE_MAX_AGE = 10 * 60
STYLE_CSS = Path(__file__).resolve().parent.parent / "ui" / "style.css"
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def load_config(env) -> dict:
    missing = [key for key in CONFIG_KEYS if not env.get(key)]
    if missing:
        raise RuntimeError(f"missing configuration: {', '.join(missing)}")
    return {key: env[key] for key in CONFIG_KEYS}


def serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def create_app(env=None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """transport replaces the network in tests."""
    config = load_config(os.environ if env is None else env)
    public_url = config["PUBLIC_URL"].rstrip("/")
    redirect_uri = f"{public_url}/auth/discord/callback"
    sessions = serializer(config["SESSION_SECRET"], "session")
    states = serializer(config["SESSION_SECRET"], "oauth-state")
    cookie_flags = {"httponly": True, "secure": True, "samesite": "lax"}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def current_user(request: Request) -> dict | None:
        try:
            return sessions.loads(request.cookies.get(SESSION_COOKIE, ""), max_age=SESSION_MAX_AGE)
        except BadSignature:  # also raised for an expired cookie
            return None

    def page(request: Request, name: str, status: int = 200, **context):
        return templates.TemplateResponse(request, name, context, status_code=status)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/static/style.css")
    def style():
        return FileResponse(STYLE_CSS)

    @app.get("/")
    def home(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        return page(request, "home.html", user=user)

    @app.get("/login")
    def login(request: Request):
        if current_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return page(request, "login.html")

    @app.get("/auth/discord")
    def auth_discord():
        state = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": config["DISCORD_CLIENT_ID"],
                "scope": "identify guilds.members.read",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        response = RedirectResponse(f"{DISCORD_AUTHORIZE}?{query}", status_code=303)
        response.set_cookie(
            STATE_COOKIE, states.dumps(state), max_age=STATE_MAX_AGE, **cookie_flags
        )
        return response

    @app.get("/auth/discord/callback")
    async def auth_discord_callback(request: Request, state: str = "", code: str = ""):
        try:
            expected = states.loads(request.cookies.get(STATE_COOKIE, ""), max_age=STATE_MAX_AGE)
        except BadSignature:
            expected = None
        if not expected or not secrets.compare_digest(expected, state):
            return page(request, "error.html", 400, message="The login could not be verified.")
        if not code:  # the user cancelled on Discord's page
            return page(request, "error.html", 400, message="The login was cancelled.")

        # the access token lives only in this request; it is never stored
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            token = await client.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(config["DISCORD_CLIENT_ID"], config["DISCORD_CLIENT_SECRET"]),
            )
            if token.status_code != 200:
                return page(request, "error.html", 400, message="Discord refused the login.")
            member = await client.get(
                f"{DISCORD_API}/users/@me/guilds/{config['DISCORD_GUILD_ID']}/member",
                headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            )

        if member.status_code == 404:  # not a member of the server
            response = page(request, "denied.html", 403)
        elif member.status_code != 200:
            response = page(request, "error.html", 502, message="Discord could not be reached.")
        elif config["DISCORD_ROLE_ID"] not in member.json().get("roles", []):
            response = page(request, "denied.html", 403)
        else:
            user = member.json()["user"]
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                sessions.dumps(
                    {"id": user["id"], "name": user.get("global_name") or user["username"]}
                ),
                max_age=SESSION_MAX_AGE,
                **cookie_flags,
            )
        response.delete_cookie(STATE_COOKIE, **cookie_flags)
        return response

    @app.get("/logout")
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, **cookie_flags)
        return response

    return app
