"""Web service for the player lookup: Discord login for members of one server holding one role.

Run with `uvicorn --factory web:create_app --app-dir src`.
"""

import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import httpx
from bson import ObjectId
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from riotwatcher import LolWatcher, RiotWatcher

import lookup
from main import connect_db

CONFIG_KEYS = (
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "DISCORD_GUILD_ID",
    "DISCORD_ROLE_ID",
    "SESSION_SECRET",
    "PUBLIC_URL",
    "RIOT_API_KEY",
    "MONGODB_URI",
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


def create_app(
    env=None, transport: httpx.AsyncBaseTransport | None = None, jobs=None, run=None
) -> FastAPI:
    """transport, jobs (the lookup_jobs collection) and run (see lookup.run_job) replace the
    network, MongoDB and the Riot API in tests."""
    env = os.environ if env is None else env
    config = load_config(env)
    reuse_hours = float(env.get("LOOKUP_REUSE_HOURS") or 6)
    per_day = int(env.get("LOOKUPS_PER_DAY") or 10)
    if jobs is None:
        matches_col, old_ids_col = connect_db(config["MONGODB_URI"])
        jobs = matches_col.database["lookup_jobs"]
        run = lookup.make_runner(
            LolWatcher(config["RIOT_API_KEY"]),
            RiotWatcher(config["RIOT_API_KEY"]),
            matches_col,
            old_ids_col,
            int(env.get("KEEP_PREVIOUS_PATCHES") or 2),
        )
    public_url = config["PUBLIC_URL"].rstrip("/")
    redirect_uri = f"{public_url}/auth/discord/callback"
    sessions = serializer(config["SESSION_SECRET"], "session")
    states = serializer(config["SESSION_SECRET"], "oauth-state")
    cookie_flags = {"httponly": True, "secure": True, "samesite": "lax"}

    @asynccontextmanager
    async def lifespan(app):
        lookup.requeue_running(jobs)
        stop = threading.Event()
        worker = threading.Thread(
            target=lookup.work, args=(jobs, run, stop), name="lookup-worker", daemon=True
        )
        worker.start()
        yield
        stop.set()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

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
        return home_page(request, user)

    def home_page(request: Request, user: dict, status: int = 200, **context):
        return page(
            request,
            "home.html",
            status,
            user=user,
            regions=lookup.REGION_NAMES,
            recent=lookup.recent_lookups(jobs, user["id"]),
            per_day=per_day,
            **context,
        )

    @app.post("/lookup")
    async def create_lookup(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        # parsed by hand: FastAPI's Form() needs python-multipart for a two-field form
        body = parse_qs((await request.body()).decode(errors="replace"))
        riot_id, region = (body.get(k, [""])[0] for k in ("riot_id", "region"))
        form = {"riot_id": riot_id, "region": region}
        if region not in lookup.REGION_NAMES:
            return home_page(request, user, 400, error="Choose KR or EUW.", form=form)
        try:
            parsed = lookup.parse_riot_id(riot_id)
        except ValueError as e:
            return home_page(request, user, 400, error=str(e), form=form)
        try:
            job_id = lookup.request_lookup(jobs, parsed, region, user["id"], reuse_hours, per_day)
        except lookup.DailyLimitError:
            error = f"You have started {per_day} lookups in the last 24 hours, the most allowed."
            return home_page(request, user, 429, error=error, form=form)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        job = jobs.find_one({"_id": ObjectId(job_id)}) if ObjectId.is_valid(job_id) else None
        if job is None:
            return page(request, "error.html", 404, message="There is no such lookup.")
        return page(request, "job.html", job=job, regions=lookup.REGION_NAMES)

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
