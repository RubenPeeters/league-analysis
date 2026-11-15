"""Player lookup jobs: a queue in MongoDB (`lookup_jobs`) and the one worker that runs them."""

import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from pymongo import ReturnDocument

from main import RiotAuthError, get_short_version, load_static_data, retention_window
from player import ROUTING, UnknownRiotIdError, analyze_player, download_player_matches

logger = logging.getLogger(__name__)

REGION_NAMES = {"kr": "KR", "euw1": "EUW"}
DDRAGON = "https://ddragon.leagueoflegends.com"
RECENT_LOOKUPS = 10
# Riot's rules: game name 3-16 characters, tag 3-5 letters or digits. The excluded characters
# would change the path of the account-v1 request.
RIOT_ID = re.compile(r"([^#/\\?%&]{3,16})#([^\W_]{3,5})")


class DailyLimitError(Exception):
    """The user has started LOOKUPS_PER_DAY lookups in the last 24 hours."""


def utcnow():
    return datetime.now(timezone.utc)


def parse_riot_id(text):
    """The Riot ID as "name#tag", stripped; ValueError when it cannot be one."""
    m = RIOT_ID.fullmatch(text.strip())
    if not m or not m[1].strip() or not m[1].isprintable():
        raise ValueError(
            "A Riot ID looks like name#tag: 3 to 16 characters, #, 3 to 5 letters or digits."
        )
    return f"{m[1].strip()}#{m[2]}"


def request_lookup(jobs, riot_id, region, user_id, reuse_hours, per_day, now=None):
    """Return the id of the job that answers riot_id (already parsed) on region for user_id:
    a queued or running one, or one done within reuse_hours, else a new queued job.
    Raise DailyLimitError when a new job is needed and the user is at per_day."""
    now = now or utcnow()
    same = {"key": riot_id.casefold(), "region": region}
    job = jobs.find_one({**same, "state": {"$in": ["queued", "running"]}}) or jobs.find_one(
        {**same, "state": "done", "finished_at": {"$gte": now - timedelta(hours=reuse_hours)}},
        sort=[("finished_at", -1)],
    )
    if job:
        # so it shows in this user's recent lookups too
        jobs.update_one({"_id": job["_id"]}, {"$addToSet": {"users": user_id}})
        return job["_id"]
    since = now - timedelta(days=1)
    if jobs.count_documents({"user_id": user_id, "created_at": {"$gte": since}}) >= per_day:
        raise DailyLimitError
    return jobs.insert_one(
        {
            **same,
            "riot_id": riot_id,
            "user_id": user_id,
            "users": [user_id],
            "state": "queued",
            "progress": None,
            "reason": None,
            "result": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        }
    ).inserted_id


def recent_lookups(jobs, user_id):
    return list(
        jobs.find(
            {"users": user_id},
            {"result": 0},
            sort=[("created_at", -1)],
            limit=RECENT_LOOKUPS,
        )
    )


def requeue_running(jobs):
    """Put jobs left running by a stopped service back in the queue; call before the worker."""
    jobs.update_many(
        {"state": "running"}, {"$set": {"state": "queued", "progress": None, "started_at": None}}
    )


def claim_next(jobs, now=None):
    """Mark the oldest queued job running and return it; None when the queue is empty."""
    return jobs.find_one_and_update(
        {"state": "queued"},
        {"$set": {"state": "running", "started_at": now or utcnow()}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def run_job(jobs, job, run, now=utcnow):
    """Run run(job, progress) and store its result, or the reason it failed."""
    _id = job["_id"]

    def progress(done, total):
        jobs.update_one({"_id": _id}, {"$set": {"progress": {"done": done, "total": total}}})

    update = {"state": "failed", "result": None}
    try:
        update = {"state": "done", "result": run(job, progress), "reason": None}
    except UnknownRiotIdError:
        update["reason"] = "unknown_riot_id"
    except RiotAuthError:
        update["reason"] = "riot_key_rejected"
    except Exception as e:  # noqa: BLE001 - the worker must survive any one job
        # Type and message only: riotwatcher sends the key as a header, never in the message.
        logger.error(f"Lookup {_id} failed: {type(e).__name__}: {e}")
        update["reason"] = "other"
    jobs.update_one({"_id": _id}, {"$set": {**update, "finished_at": now()}})


def work(jobs, run, stop, poll_seconds=5):
    """The one worker: jobs one at a time, oldest first, until stop is set."""
    while not stop.is_set():
        job = claim_next(jobs)
        if job is None:
            stop.wait(poll_seconds)
        else:
            run_job(jobs, job, run)


def load_names(version):
    """Display names (and rune icon paths) from Data Dragon; empty maps when it cannot be read."""
    names = {"version": version, "champions": {}, "items": {}, "runes": {}}
    base = f"{DDRAGON}/cdn/{version}/data/en_US"

    def get(name):
        r = requests.get(f"{base}/{name}", timeout=10)
        r.raise_for_status()
        return r.json()

    try:
        names["champions"] = {k: d["name"] for k, d in get("champion.json")["data"].items()}
        names["items"] = {k: d["name"] for k, d in get("item.json")["data"].items()}
        for style in get("runesReforged.json"):
            runes = [r for slot in style["slots"] for r in slot["runes"]]
            for r in [style, *runes]:
                names["runes"][str(r["id"])] = {"name": r["name"], "icon": r["icon"]}
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning(f"Data Dragon names not loaded: {e}")
    return names


def make_runner(watcher, riot_watcher, matches_col, old_ids_col, keep_previous=2):
    """run(job, progress) for run_job: download the player's matches, analyse them against the
    ladder's matches of the current patch, and add Data Dragon names for the page."""

    def run(job, progress):
        region = job["region"]
        if region not in ROUTING:
            raise ValueError(f"unknown region {region!r}")
        latest_ver, _, valid_items, boots, versions = load_static_data()
        window = retention_window(versions, keep_previous)
        if not window:
            raise RuntimeError("Data Dragon patch list could not be read")
        puuid, found = download_player_matches(
            watcher,
            riot_watcher,
            matches_col,
            old_ids_col,
            window,
            job["riot_id"],
            region,
            valid_items,
            progress,
        )
        found = set(found)
        stored = list(matches_col.find({"_region": region}))
        mine = [m for m in stored if m.get("metadata", {}).get("matchId") in found]
        ladder = [
            m
            for m in stored
            if get_short_version(m.get("info", {}).get("gameVersion")) == window[0]
        ]
        result = analyze_player(mine, puuid, ladder, valid_items, boots)
        result["matches"] = len(mine)
        result["patches"] = window
        result["names"] = load_names(latest_ver)
        return result

    return run
