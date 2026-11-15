"""Unit tests for src/player.py on hand-written fixtures; no Riot API, no MongoDB."""

from types import SimpleNamespace

import pytest
from test_main import FakeCollection, api_error, ev, fake_watcher, stored

import main
import player

WINDOW = ["15.22", "15.21", "15.20"]


def watchers(match_ids, versions=None, calls=None, lists=None, account=None):
    """A LolWatcher listing match_ids for puuid "pu-me" and a RiotWatcher resolving any Riot ID
    to it (account: a function replacing that lookup)."""
    base = fake_watcher(versions=versions, calls=calls)

    def matchlist(region, puuid, start, count, queue):
        assert (region, puuid, queue) == ("euw1", "pu-me", 420)
        if lists is not None:
            lists.append((start, count))
        return match_ids[start : start + count]

    def by_riot_id(routing, name, tag):
        assert routing == "europe"
        return {"puuid": "pu-me", "gameName": name, "tagLine": tag}

    watcher = SimpleNamespace(
        match=SimpleNamespace(
            matchlist_by_puuid=matchlist,
            by_id=base.match.by_id,
            timeline_by_match=base.match.timeline_by_match,
        )
    )
    return watcher, SimpleNamespace(account=SimpleNamespace(by_riot_id=account or by_riot_id))


def download(watcher, riot_watcher, col=None, old=None, progress=None):
    col = col if col is not None else FakeCollection()
    old = old if old is not None else FakeCollection()
    return player.download_player_matches(
        watcher, riot_watcher, col, old, WINDOW, "Me#EUW", "euw1", None, progress
    )


def test_unknown_riot_id_is_its_own_error():
    def not_found(routing, name, tag):
        raise api_error(404)

    with pytest.raises(player.UnknownRiotIdError):
        download(*watchers([], account=not_found))


def test_rejected_key_raises_riot_auth_error():
    def rejected(routing, name, tag):
        raise api_error(403)

    with pytest.raises(main.RiotAuthError):
        download(*watchers([], account=rejected))


def test_malformed_riot_id_is_a_value_error():
    with pytest.raises(ValueError):
        player.download_player_matches(
            *watchers([]), FakeCollection(), FakeCollection(), WINDOW, "Me", "euw1"
        )


def test_match_stored_by_the_ladder_is_not_downloaded_again():
    calls = []
    col = FakeCollection([stored("m1", "15.22.1")])
    puuid, found = download(*watchers(["m2", "m1"], calls=calls), col=col)
    assert (puuid, found, calls) == ("pu-me", ["m2", "m1"], ["m2"])
    assert sorted(d["metadata"]["matchId"] for d in col.docs) == ["m1", "m2"]
    [new] = [d for d in col.docs if d["metadata"]["matchId"] == "m2"]
    assert new["_region"] == "euw1" and "timeline" in new


def test_scan_stops_at_the_first_match_outside_the_window():
    calls = []
    old = FakeCollection()
    versions = {"m2": "15.19.1"}
    _, found = download(*watchers(["m3", "m2", "m1"], versions, calls), old=old)
    assert found == ["m3"] and calls == ["m3", "m2"]
    assert [d["_id"] for d in old.docs] == ["m2"]


def test_progress_reports_done_and_total_and_ends_complete():
    seen = []
    download(
        *watchers(["m3", "m2", "m1"], {"m2": "15.19.1"}), progress=lambda d, t: seen.append((d, t))
    )
    assert seen == [(0, 3), (1, 3), (3, 3)]


def test_match_count_caps_the_list_and_pages_by_100(monkeypatch):
    monkeypatch.setenv("PLAYER_MATCH_COUNT", "150")
    lists = []
    ids = [f"m{i}" for i in range(300)]
    _, found = download(*watchers(ids, lists=lists))
    assert lists == [(0, 100), (100, 50)] and len(found) == 150


def test_start_items_are_first_minute_purchases_with_undo_applied():
    timeline = {
        "info": {
            "participants": [{"participantId": 1, "puuid": "pu1"}],
            "frames": [
                {
                    "events": [
                        ev("ITEM_PURCHASED", 1, 1_000, itemId=1055),
                        ev("ITEM_PURCHASED", 1, 1_500, itemId=2003),
                        ev("ITEM_PURCHASED", 1, 2_000, itemId=2003),
                        ev("ITEM_UNDO", 1, 2_100, beforeId=2003, afterId=0),
                        ev("ITEM_PURCHASED", 1, 3_000, itemId=3340),
                    ]
                },
                {
                    "events": [
                        ev("ITEM_PURCHASED", 1, 61_000, itemId=1001),
                        # The undo reverts this later 2003, not the starting one.
                        ev("ITEM_PURCHASED", 1, 70_000, itemId=2003),
                        ev("ITEM_UNDO", 1, 70_500, beforeId=2003, afterId=0),
                    ]
                },
            ],
        }
    }
    slim = main.slim_timeline(timeline, {"pu1"}, {3031})
    assert slim["pu1"]["start"] == [1055, 2003, 3340]
    assert slim["pu1"]["items"] == []
