"""Unit tests for src/main.py on hand-written fixtures; no Riot API, no MongoDB."""

import main
from main import (
    aggregate,
    analyze_enemy_comp,
    build_frontend_data,
    extract_bans,
    get_short_version,
    slim_match,
)

LEGENDARY = {3001, 3002, 3003, 3004}


def player(
    champ_id, champ, team, role, win=True, k=0, d=1, a=0, items=(), name=None, dmg=(0, 0, 0)
):
    """A participant in the shape slim_match produces."""
    p = {
        "puuid": f"pu-{name}",
        "championId": champ_id,
        "championName": champ,
        "teamId": team,
        "teamPosition": role,
        "win": win,
        "kills": k,
        "deaths": d,
        "assists": a,
        "riotIdGameName": name,
        "riotIdTagline": "EUW" if name else None,
        "summonerName": None,
        "physicalDamageDealtToChampions": dmg[0],
        "magicDamageDealtToChampions": dmg[1],
        "trueDamageDealtToChampions": dmg[2],
    }
    padded = list(items) + [0] * (6 - len(items))
    p.update({f"item{i}": padded[i] for i in range(6)})
    return p


def match(participants, bans=(), region="euw1", version="15.22.700.1"):
    return {
        "_region": region,
        "metadata": {"matchId": "M"},
        "info": {
            "gameVersion": version,
            "gameCreation": 1,
            "teams": [
                {"teamId": 100, "bans": [{"championId": b, "pickTurn": 1} for b in bans]},
                {"teamId": 200, "bans": []},
            ],
            "participants": participants,
        },
    }


def build(matches, valid_items=None, tank_champs=frozenset()):
    return build_frontend_data(matches, "15.22.1", 10, tank_champs, valid_items, "now")


def by_name(rows):
    return {c["name"]: c for c in rows}


def test_import_has_no_side_effects():
    assert not hasattr(main, "matches_col")
    assert not hasattr(main, "watcher")


def test_get_short_version():
    assert get_short_version("15.22.700.1234") == "15.22"
    assert get_short_version("15") == "15"
    assert get_short_version("") == "0.0"
    assert get_short_version(None) == "0.0"


def test_extract_bans_skips_empty_ban():
    info = {
        "teams": [
            {"bans": [{"championId": 1}, {"championId": -1}]},
            {"bans": [{"championId": 2}]},
        ]
    }
    assert extract_bans(info) == [1, 2]


def test_slim_match_keeps_only_analysis_fields():
    detail = {
        "metadata": {"matchId": "EUW1_1", "participants": ["x"]},
        "info": {
            "gameVersion": "15.22.1",
            "gameCreation": 123,
            "gameMode": "CLASSIC",
            "teams": [{"teamId": 100, "bans": [{"championId": 5}], "objectives": {}}],
            "participants": [
                {"puuid": "p", "championId": 1, "championName": "Annie", "item0": 3001, "gold": 9}
            ],
        },
    }
    slim = slim_match(detail, "euw1")
    assert slim["_region"] == "euw1"
    assert slim["metadata"] == {"matchId": "EUW1_1"}
    assert slim["info"]["teams"] == [{"teamId": 100, "bans": [{"championId": 5}]}]
    assert set(slim["info"]) == {"gameVersion", "gameCreation", "teams", "participants"}
    p = slim["info"]["participants"][0]
    assert "gold" not in p
    assert p["item0"] == 3001 and p["item5"] == 0
    assert p["physicalDamageDealtToChampions"] == 0
    assert p["riotIdGameName"] is None


def enemies(dmg_each, champs=("A", "B")):
    return {
        "participants": [player(1, "Me", 100, "TOP", dmg=(999, 999, 999))]
        + [player(i, c, 200, "TOP", dmg=dmg_each) for i, c in enumerate(champs)]
    }


def test_analyze_enemy_comp_heavy_ad():
    assert analyze_enemy_comp(enemies((70, 20, 10)), 100, set()) == ["Heavy AD"]


def test_analyze_enemy_comp_heavy_ap():
    assert analyze_enemy_comp(enemies((20, 70, 10)), 100, set()) == ["Heavy AP"]


def test_analyze_enemy_comp_tank_heavy():
    info = enemies((50, 50, 0), champs=("Malphite", "Ornn", "Lux"))
    assert analyze_enemy_comp(info, 100, {"Malphite", "Ornn"}) == ["Tank Heavy"]
    assert analyze_enemy_comp(info, 100, {"Malphite"}) == []


def test_analyze_enemy_comp_zero_damage():
    info = enemies((0, 0, 0), champs=("Ornn", "Sion"))
    assert analyze_enemy_comp(info, 100, {"Ornn", "Sion"}) == []


def test_pick_ban_and_win_rate():
    matches = [
        match([player(1, "Garen", 100, "TOP", win=True, k=4, d=0, a=2)], bans=[2]),
        match([player(1, "Garen", 100, "TOP", win=False, k=2, d=0, a=0)], bans=[2]),
        match([player(1, "Garen", 100, "TOP", win=True, k=1, d=2, a=1)]),
        match([player(2, "Darius", 100, "TOP", win=True, k=3, d=1, a=0)], bans=[1]),
    ]
    top = by_name(build(matches)["regions"]["euw1"]["TOP"])

    garen = top["Garen"]
    assert (garen["count"], garen["pick_rate"], garen["win_rate"], garen["ban_rate"]) == (
        3,
        75.0,
        66.7,
        25.0,
    )
    assert garen["kda"] == 5.0  # (7 kills + 3 assists) / 2 deaths

    darius = top["Darius"]
    assert (darius["pick_rate"], darius["ban_rate"], darius["win_rate"]) == (25.0, 50.0, 100.0)


def test_kda_with_zero_deaths_divides_by_one():
    data = build([match([player(1, "Garen", 100, "TOP", k=5, d=0, a=3, name="Ann")])])
    assert data["regions"]["euw1"]["TOP"][0]["kda"] == 8.0
    assert data["leaderboards"] == {
        "TOP:Garen": [
            {"player": "Ann#EUW", "region": "euw1", "games": 1, "win_rate": 100.0, "kda": 8.0}
        ]
    }


def test_banned_never_picked_champion_is_listed_with_zero_games():
    matches = [match([player(1, "Garen", 100, "TOP"), player(2, "Darius", 200, "JUNGLE")], [2])]
    darius = by_name(build(matches)["regions"]["euw1"]["TOP"])["Darius"]
    assert (darius["count"], darius["win_rate"], darius["ban_rate"]) == (0, 0, 100.0)
    assert darius["top_items"] == []


def test_top_build_signature_is_sorted_first_three_valid_items():
    matches = [
        # the component 1001 is dropped; only the first 3 valid items count, sorted
        match([player(1, "Garen", 100, "TOP", items=[3002, 1001, 3001, 3004, 3003])]),
        match([player(1, "Garen", 100, "TOP", items=[3001, 3004, 3002])]),
        match([player(1, "Garen", 100, "TOP", items=[3003, 3004])]),
        # a single valid item is not a build
        match([player(1, "Garen", 100, "TOP", items=[3003])]),
    ]
    garen = build(matches, valid_items=LEGENDARY)["regions"]["euw1"]["TOP"][0]
    assert garen["top_items"] == [3001, 3002, 3004]


def test_no_item_data_means_no_item_filtering():
    matches = [match([player(1, "Garen", 100, "TOP", items=[1001, 1002])])]
    garen = build(matches, valid_items=None)["regions"]["euw1"]["TOP"][0]
    assert garen["top_items"] == [1001, 1002]


def ad_game(items):
    """Garen top against an enemy team doing only physical damage."""
    return match(
        [
            player(1, "Garen", 100, "TOP", items=items),
            player(2, "Darius", 200, "JUNGLE", dmg=(100, 0, 0)),
        ]
    )


def test_context_build_below_three_games_is_left_out():
    two = [ad_game([3001, 3002]), ad_game([3001, 3002])]
    garen = by_name(build(two, valid_items=LEGENDARY)["regions"]["euw1"]["TOP"])["Garen"]
    assert garen["context_builds"] == {}


def test_context_build_at_three_games_is_included():
    three = [ad_game([3001, 3002]), ad_game([3001, 3002]), ad_game([3003, 3004])]
    garen = by_name(build(three, valid_items=LEGENDARY)["regions"]["euw1"]["TOP"])["Garen"]
    assert garen["context_builds"] == {"Heavy AD": {"games": 3, "items": [3001, 3002]}}


def test_leaderboard_keys_are_role_and_champion():
    matches = [
        match([player(1, "Garen", 100, "TOP", name="Ann"), player(2, "Lee", 200, "JUNGLE")]),
        match([player(1, "Garen", 100, "TOP", name="Cy", win=False)]),
        match([player(1, "Garen", 100, "TOP", name="Cy")], region="kr"),
        match([player(1, "Garen", 100, "MIDDLE", name="Ann")]),
    ]
    lb = build(matches)["leaderboards"]
    assert set(lb) == {"TOP:Garen", "JUNGLE:Lee", "MIDDLE:Garen"}
    assert lb["JUNGLE:Lee"][0]["player"] == "Unknown"
    # Cy is seen in kr first, so kr stays their region despite a game in euw1
    assert [(e["player"], e["games"], e["region"]) for e in lb["TOP:Garen"]] == [
        ("Cy#EUW", 2, "kr"),
        ("Ann#EUW", 1, "euw1"),
    ]


def test_aggregate_keeps_top_15_by_pick_rate():
    summaries = []
    for n in range(20):
        p = {"role": "TOP", "name": "x", "champ": f"C{n}", "win": True, "k": 0, "d": 0, "a": 0}
        summaries += [{"bans": [], "participants": [{**p, "items": [], "context": []}]}] * (n + 1)
    result = aggregate(summaries, "TOP", "euw1", {}, None, {})
    assert [c["name"] for c in result] == [f"C{n}" for n in range(19, 4, -1)]


def test_meta_and_regions_shape():
    matches = [match([player(1, "Garen", 100, "TOP")]), match([], region="na1")]
    data = build_frontend_data(matches, None, 7, set(), None, "2026-11-15 00:00 UTC")
    assert data["meta"] == {
        "total_games": 2,
        "last_updated": "2026-11-15 00:00 UTC",
        "current_patch": "14.1",
        "ddragon_version": "14.23.1",
        "player_count": 7,
    }
    assert list(data["regions"]) == ["kr", "euw1"]
    assert list(data["regions"]["kr"]) == ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    assert data["regions"]["kr"]["TOP"] == []
