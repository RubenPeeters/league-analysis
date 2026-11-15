"""Unit tests for src/player.py on hand-written fixtures; no Riot API, no MongoDB."""

import json
from types import SimpleNamespace

import pytest
from test_main import (
    BOOTS,
    LEGENDARY,
    FakeCollection,
    api_error,
    build,
    buys,
    ev,
    fake_watcher,
    match,
    page,
    stored,
)
from test_main import player as participant

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


STARTER = (1055, 2003)


def game(
    champ="Garen",
    opp="Darius",
    items=(3001,),
    win=True,
    keystone=8010,
    start=STARTER,
    remake=None,
    timeline=True,
):
    """A stored match of "pu-Me" in TOP against opp (None: no enemy TOP), with a timeline whose
    purchases are items in order and whose first-minute purchases are start."""
    ps = [
        participant(1, champ, 100, "TOP", win=win, items=items, name="Me", remake=remake),
        participant(3, "Lee Sin", 200, "JUNGLE", items=(3002,), name="Jg"),
    ]
    ps[0]["perks"] = page(keystone=keystone)
    ps[0]["summoner1Id"], ps[0]["summoner2Id"] = 4, 12
    if opp:
        ps.append(participant(2, opp, 200, "TOP", win=not win, items=(3003,), name="Opp"))
    m = match(ps)
    if timeline:
        m["timeline"] = {
            "pu-Me": {
                "items": buys(*((i, 100_000 * (n + 1)) for n, i in enumerate(items))),
                "skills": [],
                "start": list(start),
            }
        }
    return m


def analyze(matches, ladder=None):
    ladder = ladder if ladder is not None else {"regions": {}}
    result = player.analyze_player(matches, "pu-Me", ladder, LEGENDARY | BOOTS, BOOTS)
    json.dumps(result)
    return result["champions"]


def test_champion_needs_five_games_without_remakes():
    assert analyze([game()] * 4 + [game(remake=True)]) == []
    (garen,) = analyze([game(win=False)] + [game()] * 4 + [game(remake=True)])
    assert (garen["region"], garen["role"], garen["champion"]) == ("euw1", "TOP", "Garen")
    assert (garen["games"], garen["win_rate"], garen["item_games"]) == (5, 80.0, 5)
    assert garen["items"][0] == {"id": 3001, "games": 5, "share": 100.0, "ladder_share": None}
    assert garen["runes"][0]["keystone"] == 8010
    assert garen["spells"][0] == {"ids": [4, 12], "games": 5, "share": 100.0}


def test_matches_without_timeline_count_for_builds_but_not_for_order_or_start():
    matches = [game(start=(1055, 2003, 2003))] * 2 + [game(start=(1054,))] + [game()] * 2
    matches += [game(start=(2033,)), game(timeline=False)]
    (garen,) = analyze(matches)
    assert (garen["games"], garen["timeline_games"], garen["start_games"]) == (7, 6, 6)
    assert garen["first_items"][0]["games"] == 6
    assert garen["starting_items"] == [
        {"ids": [1055, 2003], "games": 2},
        {"ids": [1055, 2003, 2003], "games": 2},
        {"ids": [1054], "games": 1},
    ]


def test_lane_opponent_needs_three_games_and_ignores_other_roles():
    matches = [game(opp="Darius", items=(3001, 3004), keystone=8437, start=(1054,))] * 3
    matches += [game(opp="Sett")] * 2 + [game(opp=None)]
    (garen,) = analyze(matches)
    (darius,) = garen["opponents"]
    assert darius == {
        "champion": "Darius",
        "games": 3,
        "win_rate": 100.0,
        "keystone": {"id": 8437, "games": 3},
        "starting_items": {"ids": [1054], "games": 3},
        "other_games": 3,
        "items": [
            {"id": 3004, "games": 3, "share": 100.0, "other_games": 0, "other_share": 0.0},
        ],
    }


def test_lane_opponent_without_three_other_games_has_no_item_differences():
    (garen,) = analyze([game(opp="Darius", items=(3004,))] * 3 + [game(opp="Sett")] * 2)
    assert [(o["champion"], o["items"]) for o in garen["opponents"]] == [("Darius", [])]


def test_ladder_shares_where_the_ladder_has_the_champion():
    ladder_games = [
        match([participant(1, "Garen", 100, "TOP", items=(3001,), name=f"L{n}")]) for n in range(3)
    ] + [match([participant(1, "Garen", 100, "TOP", items=(3002,), name="L9")])]
    for m in ladder_games:
        m["info"]["participants"][0]["perks"] = page(keystone=8010)
        m["timeline"] = {
            p["puuid"]: {"items": buys((p["item0"], 1)), "skills": []}
            for p in m["info"]["participants"]
        }
    ladder = build(ladder_games, valid_items=LEGENDARY | BOOTS, boots=BOOTS)

    garen, darius = analyze([game()] * 6 + [game(champ="Darius", opp="Garen")] * 5, ladder)
    assert garen["ladder"] == {"games": 4, "item_games": 4, "rune_games": 4, "timeline_games": 4}
    assert garen["items"][0]["ladder_share"] == 75.0
    assert garen["runes"][0]["ladder_share"] == 100.0
    assert garen["first_items"][0]["ladder_share"] == 75.0
    assert darius["ladder"] is None
    assert darius["items"][0]["ladder_share"] is None
    assert darius["runes"][0]["ladder_share"] is None
    assert darius["first_items"][0]["ladder_share"] is None


def test_regions_are_kept_apart_and_other_players_have_no_result():
    kr = [game() for _ in range(5)]
    for m in kr:
        m["_region"] = "kr"
    euw, korea = analyze([game()] * 6 + kr)
    assert [(c["region"], c["games"]) for c in (euw, korea)] == [("euw1", 6), ("kr", 5)]
    assert player.analyze_player([game()] * 5, "pu-X", {}) == {"puuid": "pu-X", "champions": []}
