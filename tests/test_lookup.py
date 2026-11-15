"""Tests for src/lookup.py and the lookup pages of src/web.py; MongoDB, Riot and Discord faked."""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from test_main import FakeCollection, api_error
from test_player import watchers
from test_web import ENV, client

import lookup
import web

T0 = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class FakeJobs(FakeCollection):
    """The pymongo calls lookup.py makes on lookup_jobs."""

    def insert_one(self, doc):
        doc = {"_id": ObjectId(), **doc}
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    def find(self, query=None, projection=None, sort=(), limit=0):
        hits = super().find(query)
        for key, direction in reversed(list(sort)):
            hits.sort(key=lambda d: d[key], reverse=direction < 0)
        return hits[: limit or None]

    def find_one(self, query, sort=()):
        return next(iter(self.find(query, sort=sort)), None)

    def find_one_and_update(self, query, update, sort=(), return_document=None):
        doc = self.find_one(query, sort)
        if doc:
            doc.update(update["$set"])
        return doc

    def update_one(self, query, update):
        for doc in self.find(query)[:1]:
            doc.update(update.get("$set", {}))
            for key, value in update.get("$addToSet", {}).items():
                if value not in doc[key]:
                    doc[key].append(value)

    def update_many(self, query, update):
        for doc in self.find(query):
            doc.update(update["$set"])

    def count_documents(self, query):
        return len(self.find(query))


def request(jobs, riot_id="Faker#KR1", region="kr", user="u1", now=T0, per_day=10):
    return lookup.request_lookup(jobs, riot_id, region, user, 6, per_day, now)


def finish(jobs, job_id, at, state="done"):
    jobs.update_one({"_id": job_id}, {"$set": {"state": state, "finished_at": at}})


def test_job_creation():
    jobs = FakeJobs()
    job_id = request(jobs)
    [job] = jobs.docs
    assert job["_id"] == job_id
    assert {k: job[k] for k in ("riot_id", "key", "region", "user_id", "state")} == {
        "riot_id": "Faker#KR1",
        "key": "faker#kr1",
        "region": "kr",
        "user_id": "u1",
        "state": "queued",
    }
    assert job["created_at"] == T0 and job["progress"] is None and job["result"] is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("  Hide on bush#KR1 ", "Hide on bush#KR1"),
        ("Ünïcödé#EUW", "Ünïcödé#EUW"),
        ("ab#KR1", None),
        ("name", None),
        ("name#", None),
        ("name#toolong", None),
        ("na/me#KR1", None),
        ("name#K_1", None),
        ("a#b#KR1", None),
        ("   #KR1", None),
    ],
)
def test_riot_id_is_validated(text, expected):
    if expected is None:
        with pytest.raises(ValueError):
            lookup.parse_riot_id(text)
    else:
        assert lookup.parse_riot_id(text) == expected


def test_worker_runs_one_job_at_a_time_oldest_first():
    jobs = FakeJobs()
    for minutes, name in ((2, "Third#KR1"), (0, "First#KR1"), (1, "Second#KR1")):
        request(jobs, name, now=T0 + timedelta(minutes=minutes))
    stop, seen = threading.Event(), []

    def run(job, progress):
        assert jobs.count_documents({"state": "running"}) == 1
        seen.append(job["riot_id"])
        progress(1, 1)
        if len(seen) == 3:
            stop.set()
        return {"champions": []}

    lookup.work(jobs, run, stop, poll_seconds=0)
    assert seen == ["First#KR1", "Second#KR1", "Third#KR1"]
    assert all(j["state"] == "done" and j["progress"] == {"done": 1, "total": 1} for j in jobs.docs)


@pytest.fixture
def real_run(monkeypatch):
    """lookup.make_runner with Data Dragon stubbed; account: replaces the Riot ID lookup."""
    monkeypatch.setattr(
        lookup,
        "load_static_data",
        lambda: ("15.22.1", set(), None, set(), ["15.22.1", "15.21.1", "15.20.1"]),
    )
    monkeypatch.setattr(lookup, "load_names", lambda v: {"version": v})

    def make(match_ids=(), account=None, matches=None):
        return lookup.make_runner(
            *watchers(list(match_ids), account=account),
            matches if matches is not None else FakeCollection(),
            FakeCollection(),
        )

    return make


def run_one(run, riot_id="Me#EUW"):
    jobs = FakeJobs()
    request(jobs, riot_id, "euw1")
    lookup.run_job(jobs, lookup.claim_next(jobs), run)
    return jobs.docs[0]


def test_done_job_stores_the_analysis_and_progress(real_run):
    job = run_one(real_run(["m2", "m1"]))
    assert job["state"] == "done" and job["reason"] is None
    assert job["progress"] == {"done": 2, "total": 2}
    result = job["result"]
    assert (result["puuid"], result["matches"], result["champions"]) == ("pu-me", 2, [])
    assert result["names"] == {"version": "15.22.1"}


@pytest.mark.parametrize("status, reason", [(404, "unknown_riot_id"), (403, "riot_key_rejected")])
def test_riot_failures_have_their_own_reason(real_run, status, reason):
    def account(routing, name, tag):
        raise api_error(status)

    job = run_one(real_run(account=account))
    assert (job["state"], job["reason"], job["result"]) == ("failed", reason, None)


def test_any_other_failure_is_reason_other_and_the_worker_goes_on(caplog):
    def run(job, progress):
        raise RuntimeError("boom")

    job = run_one(run)
    assert (job["state"], job["reason"]) == ("failed", "other")
    assert job["finished_at"] is not None and "boom" in caplog.text


def test_result_is_reused_inside_the_window_and_redownloaded_after():
    jobs = FakeJobs()
    first = request(jobs)
    # still queued: a second request joins it
    assert request(jobs, "faker#kr1", user="u2") == first
    finish(jobs, first, T0)
    assert request(jobs, user="u3", now=T0 + timedelta(hours=5)) == first
    assert jobs.docs[0]["users"] == ["u1", "u2", "u3"]
    assert request(jobs, region="euw1", now=T0 + timedelta(hours=5)) != first
    later = request(jobs, user="u3", now=T0 + timedelta(hours=7))
    assert later != first and len(jobs.docs) == 3


def test_failed_job_is_not_reused():
    jobs = FakeJobs()
    first = request(jobs)
    finish(jobs, first, T0, "failed")
    assert request(jobs, now=T0 + timedelta(minutes=1)) != first


def test_daily_limit_counts_new_lookups_of_the_last_24_hours():
    jobs = FakeJobs()
    request(jobs, "Old#KR1", now=T0 - timedelta(hours=25), per_day=2)
    request(jobs, "One#KR1", per_day=2)
    request(jobs, "Two#KR1", per_day=2)
    with pytest.raises(lookup.DailyLimitError):
        request(jobs, "Three#KR1", per_day=2)
    # reusing a running lookup is not a new one
    assert request(jobs, "One#KR1", per_day=2)
    assert request(jobs, "Three#KR1", user="u2", per_day=2)


def test_running_job_is_requeued_at_start_up():
    jobs = FakeJobs()
    running, done = request(jobs, "A#KR1"), request(jobs, "B#KR1")
    lookup.claim_next(jobs)
    finish(jobs, done, T0)
    lookup.requeue_running(jobs)
    assert [j["state"] for j in jobs.docs] == ["queued", "done"]
    assert lookup.claim_next(jobs)["_id"] == running


def test_service_start_requeues_and_runs_the_job_left_running():
    jobs = FakeJobs()
    request(jobs)
    lookup.claim_next(jobs)
    ran = threading.Event()

    def run(job, progress):
        ran.set()
        return {"champions": []}

    with client(jobs=jobs, run=run):
        assert ran.wait(5)
    assert jobs.docs[0]["started_at"] is not None


def logged_in(jobs=None, env=ENV):
    c = client(jobs=jobs if jobs is not None else FakeJobs(), env=env)
    c.cookies.set(
        "session", web.serializer(ENV["SESSION_SECRET"], "session").dumps({"id": "42", "name": "R"})
    )
    return c


def test_request_without_a_session_creates_nothing():
    jobs = FakeJobs()
    c = client(jobs=jobs)
    response = c.post("/lookup", data={"riot_id": "Faker#KR1", "region": "kr"})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert jobs.docs == []
    assert c.get(f"/jobs/{ObjectId()}").headers["location"] == "/login"


@pytest.mark.parametrize("form", [{"riot_id": "x", "region": "kr"}, {"riot_id": "Faker#KR1"}])
def test_invalid_form_is_refused_without_a_job(form):
    jobs = FakeJobs()
    response = logged_in(jobs).post("/lookup", data=form)
    assert response.status_code == 400 and jobs.docs == []


def test_lookup_page_flow_from_form_to_result():
    jobs = FakeJobs()
    c = logged_in(jobs)
    home = c.get("/")
    assert 'name="riot_id"' in home.text and "None yet" in home.text
    response = c.post("/lookup", data={"riot_id": "Faker#KR1", "region": "kr"})
    assert response.status_code == 303
    page = c.get(response.headers["location"])
    assert "Waiting in the queue" in page.text and 'http-equiv="refresh"' in page.text
    assert "Faker#KR1 (KR)" in c.get("/").text

    job = jobs.docs[0]
    job.update(state="running", progress={"done": 3, "total": 40})
    assert "3 of 40 done" in c.get(response.headers["location"]).text

    job.update(state="done", finished_at=T0, result=RESULT)
    page = c.get(response.headers["location"]).text
    assert 'http-equiv="refresh"' not in page
    for text in ("Kai&#39;Sa", "Infinity Edge", "Lethal Tempo", "62.5%", "40.0%", "–", "Ezreal"):
        assert text in page, text
    assert "img/champion/Kaisa.png" in page and "perk-images/LT.png" in page
    assert ENV["RIOT_API_KEY"] not in page


def test_failed_job_page_says_why():
    jobs = FakeJobs()
    c = logged_in(jobs)
    job_id = request(jobs, user="42")
    finish(jobs, job_id, T0, "failed")
    jobs.docs[0]["reason"] = "unknown_riot_id"
    assert "no player with this Riot ID" in c.get(f"/jobs/{job_id}").text
    assert c.get("/jobs/not-an-id").status_code == 404


def test_page_says_when_the_daily_limit_is_reached():
    env = {**ENV, "LOOKUPS_PER_DAY": "1"}
    c = logged_in(env=env)
    assert c.post("/lookup", data={"riot_id": "One#KR1", "region": "kr"}).status_code == 303
    response = c.post("/lookup", data={"riot_id": "Two#KR1", "region": "kr"})
    assert response.status_code == 429 and "1 lookups in the last 24 hours" in response.text


RESULT = {
    "puuid": "pu-me",
    "matches": 8,
    "patches": ["15.22", "15.21", "15.20"],
    "names": {
        "version": "15.22.1",
        "champions": {"Kaisa": "Kai'Sa", "Ezreal": "Ezreal"},
        "items": {"3031": "Infinity Edge"},
        "runes": {"8008": {"name": "Lethal Tempo", "icon": "perk-images/LT.png"}},
    },
    "champions": [
        {
            "region": "kr",
            "role": "BOTTOM",
            "champion": "Kaisa",
            "games": 8,
            "win_rate": 62.5,
            "items": [{"id": 3031, "games": 5, "share": 62.5, "ladder_share": 40.0}],
            "boots": [{"id": 3006, "games": 8, "share": 100.0, "ladder_share": None}],
            "first_items": [],
            "runes": [
                {
                    "keystone": 8008,
                    "primary_style": 8000,
                    "secondary_style": 8100,
                    "share": 100.0,
                    "ladder_share": 50.0,
                }
            ],
            "starting_items": [{"ids": [1055, 2003], "games": 8}],
            "start_games": 8,
            "skill_max": {"order": ["Q", "W", "E"], "games": 6},
            "opponents": [
                {
                    "champion": "Ezreal",
                    "games": 3,
                    "win_rate": 66.7,
                    "keystone": {"id": 8008, "games": 3},
                    "starting_items": {"ids": [1055], "games": 3},
                    "other_games": 5,
                    "items": [
                        {
                            "id": 3031,
                            "games": 3,
                            "share": 100.0,
                            "other_games": 2,
                            "other_share": 40.0,
                        }
                    ],
                }
            ],
        }
    ],
}
