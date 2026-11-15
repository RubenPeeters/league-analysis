"""Unit tests for src/main.py on hand-written fixtures; no Riot API, no MongoDB."""

import json
from types import SimpleNamespace

import pytest
import requests
from pymongo.errors import DuplicateKeyError
from riotwatcher import ApiError

import main
from main import (
    aggregate,
    analyze_enemy_comp,
    build_frontend_data,
    champion_damage_shares,
    extract_bans,
    get_short_version,
    incomplete_matches,
    refetch_incomplete,
    select_items,
    slim_match,
    update_history,
)

LEGENDARY = {3001, 3002, 3003, 3004}


def player(
    champ_id,
    champ,
    team,
    role,
    win=True,
    k=0,
    d=1,
    a=0,
    items=(),
    name=None,
    dmg=(0, 0, 0),
    perks=None,
    spells=(None, None),
    remake=None,
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
        "perks": perks,
        "summoner1Id": spells[0],
        "summoner2Id": spells[1],
        "gameEndedInEarlySurrender": remake,
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


def build(matches, valid_items=None, tank_champs=frozenset(), boots=frozenset()):
    return build_frontend_data(matches, "15.22.1", 10, tank_champs, valid_items, "now", boots)


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
            "queueId": 420,
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
    assert set(slim["info"]) == {
        "gameVersion",
        "gameCreation",
        "queueId",
        "gameDuration",
        "teams",
        "participants",
    }
    assert slim["info"]["queueId"] == 420
    p = slim["info"]["participants"][0]
    assert "gold" not in p
    assert p["item0"] == 3001 and p["item5"] == 0
    assert p["physicalDamageDealtToChampions"] == 0
    assert p["riotIdGameName"] is None
    # A response without perks, spells or duration still slims, with None in their place.
    assert slim["info"]["gameDuration"] is None
    assert p["perks"] is None and p["summoner1Id"] is None
    assert p["gameEndedInEarlySurrender"] is None


def test_slim_match_keeps_runes_spells_and_game_length():
    def selections(*ids):
        return [{"perk": i, "var1": 0, "var2": 0, "var3": 0} for i in ids]

    participant = {
        "championId": 1,
        "summoner1Id": 4,
        "summoner2Id": 14,
        "gameEndedInEarlySurrender": True,
        "perks": {
            "statPerks": {"defense": 5011, "flex": 5008, "offense": 5005},
            "styles": [
                {
                    "description": "primaryStyle",
                    "style": 8100,
                    "selections": selections(8112, 8139, 8138, 8135),
                },
                {"description": "subStyle", "style": 8300, "selections": selections(8304, 8347)},
            ],
        },
    }
    detail = {"info": {"gameDuration": 212, "participants": [participant]}}
    slim = slim_match(detail, "kr")
    assert slim["info"]["gameDuration"] == 212
    p = slim["info"]["participants"][0]
    assert (p["summoner1Id"], p["summoner2Id"], p["gameEndedInEarlySurrender"]) == (4, 14, True)
    assert p["perks"] == {
        "primaryStyle": 8100,
        "primary": [8112, 8139, 8138, 8135],
        "subStyle": 8300,
        "secondary": [8304, 8347],
        "shards": [5005, 5008, 5011],
    }


def stored(match_id, version, **info):
    return {
        "_region": "euw1",
        "metadata": {"matchId": match_id},
        "info": {"gameVersion": version, **info},
    }


def test_incomplete_matches_are_window_matches_without_newer_fields():
    docs = [
        stored("old_format", "15.22.700.1"),
        stored("no_queue", "15.22.700.1", gameDuration=1800),
        stored("new_format", "15.22.700.1", gameDuration=1800, queueId=420),
        stored("new_format_no_duration", "15.22.700.1", gameDuration=None, queueId=420),
        stored("previous_patch", "15.21.400.1"),
        stored("too_old", "15.19.400.1"),
        stored("prefix_patch", "15.2.1.1"),
    ]
    assert incomplete_matches(docs, ["15.22", "15.21", "15.20"]) == [
        ("euw1", "old_format"),
        ("euw1", "no_queue"),
        ("euw1", "previous_patch"),
    ]


class RefetchCollection:
    def __init__(self, docs):
        self.docs = docs
        self.replaced = []

    def find(self, query, projection):
        return list(self.docs)

    def replace_one(self, query, doc):
        self.replaced.append((query, doc))


def test_refetch_replaces_incomplete_matches_and_tries_each_once():
    calls = []

    def by_id(region, match_id):
        calls.append(match_id)
        if match_id == "gone":
            raise api_error(404)
        if match_id == "broken":
            raise api_error(500)
        return {"metadata": {"matchId": match_id}, "info": {"gameDuration": 1500}}

    col = RefetchCollection([stored(m, "15.22.1") for m in ("gone", "broken", "ok")])
    watcher = SimpleNamespace(match=SimpleNamespace(by_id=by_id))
    assert refetch_incomplete(watcher, col, ["15.22"]) == 1
    assert calls == ["gone", "broken", "ok"]
    [(query, doc)] = col.replaced
    assert query == {"metadata.matchId": "ok"}
    assert doc["_region"] == "euw1" and doc["info"]["gameDuration"] == 1500


def test_rejected_key_during_refetch_propagates():
    def by_id(region, match_id):
        raise api_error(403)

    col = RefetchCollection([stored("a", "15.22.1")])
    watcher = SimpleNamespace(match=SimpleNamespace(by_id=by_id))
    with pytest.raises(main.RiotAuthError):
        refetch_incomplete(watcher, col, ["15.22"])
    assert col.replaced == []


def champ_games(champ, dmg, n):
    """n stored games of champ dealing dmg (physical, magic, true) to champions."""
    return [match([player(1, champ, 100, "TOP", dmg=dmg)]) for _ in range(n)]


def test_champion_damage_share_sums_damage_over_games():
    games = champ_games("Ahri", (10, 80, 10), 3) + champ_games("Ahri", (40, 50, 10), 2)
    games.append(match([player(1, "Ahri", 100, "MIDDLE", dmg=(0, 0, 0))], region="kr"))
    phys, magic = champion_damage_shares(games)["Ahri"]
    assert round(phys, 4) == round(110 / 500, 4)
    assert round(magic, 4) == round(340 / 500, 4)


def test_champion_with_fewer_than_five_games_has_no_share():
    assert champion_damage_shares(champ_games("Ahri", (0, 100, 0), 4)) == {}
    assert "Ahri" in champion_damage_shares(champ_games("Ahri", (0, 100, 0), 5))


def enemies(champs):
    return {
        "participants": [player(1, "Me", 100, "TOP")]
        + [player(i, c, 200, "TOP") for i, c in enumerate(champs)]
    }


SHARES = {"Darius": (0.9, 0.05), "Jinx": (0.7, 0.1), "Lux": (0.1, 0.85), "Ahri": (0.2, 0.7)}


def test_team_type_is_the_mean_over_known_champions():
    assert analyze_enemy_comp(enemies(["Darius", "Jinx"]), 100, set(), SHARES) == ["Heavy AD"]
    assert analyze_enemy_comp(enemies(["Lux", "Ahri"]), 100, set(), SHARES) == ["Heavy AP"]
    # mean physical (0.9 + 0.1) / 2 = 0.5, mean magic 0.45: neither
    assert analyze_enemy_comp(enemies(["Darius", "Lux"]), 100, set(), SHARES) == []
    # a champion without a share is left out of the mean, not counted as 0
    assert analyze_enemy_comp(enemies(["Darius", "Zoe"]), 100, set(), SHARES) == ["Heavy AD"]
    assert analyze_enemy_comp(enemies(["Zoe"]), 100, set(), SHARES) == []


def test_team_type_ignores_damage_dealt_in_the_game():
    info = enemies(["Lux", "Ahri"])
    for p in info["participants"][1:]:
        p["physicalDamageDealtToChampions"] = 10_000
    assert analyze_enemy_comp(info, 100, set(), SHARES) == ["Heavy AP"]


def test_team_type_tank_heavy():
    info = enemies(["Malphite", "Ornn", "Lux"])
    assert analyze_enemy_comp(info, 100, {"Malphite", "Ornn"}, {}) == ["Tank Heavy"]
    assert analyze_enemy_comp(info, 100, {"Malphite"}, {}) == []


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
        37.5,  # 3 picks over 8 TOP slots (2 per match)
        66.7,
        25.0,
    )
    assert garen["kda"] == 5.0  # (7 kills + 3 assists) / 2 deaths

    darius = top["Darius"]
    assert (darius["pick_rate"], darius["ban_rate"], darius["win_rate"]) == (12.5, 50.0, 100.0)


def test_red_side_champion_is_counted():
    matches = [
        match([player(1, "Garen", 100, "TOP"), player(2, "Darius", 200, "TOP", win=False)]),
        match([player(1, "Garen", 100, "TOP"), player(3, "Sion", 200, "TOP")]),
    ]
    top = by_name(build(matches)["regions"]["euw1"]["TOP"])
    assert (top["Darius"]["count"], top["Darius"]["pick_rate"], top["Darius"]["win_rate"]) == (
        1,
        25.0,
        0,
    )
    assert (top["Garen"]["count"], top["Garen"]["pick_rate"]) == (2, 50.0)


def test_pick_rates_add_up_to_100_per_role():
    roles = ("TOP", "JUNGLE")
    matches = [
        match(
            [player(n, f"B{n % 3}", 100, r) for r in roles]
            + [player(n, f"R{n % 2}", 200, r) for r in roles]
        )
        for n in range(5)
    ]
    region = build(matches)["regions"]["euw1"]
    for r in roles:
        assert sum(c["pick_rate"] for c in region[r]) == 100.0


def test_leaderboard_contains_red_side_player():
    matches = [
        match(
            [player(1, "Garen", 100, "TOP", name="Ann"), player(2, "Darius", 200, "TOP", name="Bo")]
        )
    ]
    lb = build(matches)["leaderboards"]
    assert [e["player"] for e in lb["TOP:Darius"]] == ["Bo#EUW"]


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
    assert (darius["items"], darius["boots"]) == ([], [])


def test_item_shares_are_over_all_games_with_boots_apart():
    boots = {3020}
    matches = [
        # the component 1001 is dropped; a duplicate item counts once
        match([player(1, "Garen", 100, "TOP", items=[3002, 1001, 3001, 3020, 3001])]),
        match([player(1, "Garen", 100, "TOP", items=[3001, 3020])]),
        match([player(1, "Garen", 100, "TOP", items=[3003])]),
        # a game without finished items still counts in the denominator
        match([player(1, "Garen", 100, "TOP", items=[1001])]),
    ]
    garen = build(matches, valid_items=LEGENDARY | boots, boots=boots)["regions"]["euw1"]["TOP"][0]
    assert garen["count"] == 4
    assert garen["items"] == [
        {"id": 3001, "games": 2, "share": 50.0},
        {"id": 3002, "games": 1, "share": 25.0},
        {"id": 3003, "games": 1, "share": 25.0},
    ]
    assert garen["boots"] == [{"id": 3020, "games": 2, "share": 50.0}]


def test_items_are_capped_at_eight_by_share():
    ids = list(range(4001, 4011))
    matches = [
        match([player(1, "Garen", 100, "TOP", items=ids[:6])]),
        match([player(1, "Garen", 100, "TOP", items=ids[4:])]),
    ]
    garen = build(matches)["regions"]["euw1"]["TOP"][0]
    assert [i["id"] for i in garen["items"]] == [4005, 4006, 4001, 4002, 4003, 4004, 4007, 4008]


def test_champion_without_finished_items_has_empty_lists():
    matches = [match([player(1, "Garen", 100, "TOP", items=[1001])])]
    garen = build(matches, valid_items=LEGENDARY)["regions"]["euw1"]["TOP"][0]
    assert (garen["count"], garen["items"], garen["boots"]) == (1, [], [])


def test_no_item_data_means_no_item_filtering():
    matches = [match([player(1, "Garen", 100, "TOP", items=[1001, 1002])])]
    garen = build(matches, valid_items=None)["regions"]["euw1"]["TOP"][0]
    assert [i["id"] for i in garen["items"]] == [1001, 1002]


def test_remakes_count_for_pick_rate_but_not_for_items():
    matches = [
        match([player(1, "Garen", 100, "TOP", items=[3001])]),
        match([player(1, "Garen", 100, "TOP", items=[], remake=True)]),
    ]
    garen = build(matches, valid_items=LEGENDARY)["regions"]["euw1"]["TOP"][0]
    assert (garen["count"], garen["pick_rate"], garen["item_games"]) == (2, 50.0, 1)
    assert garen["items"] == [{"id": 3001, "games": 1, "share": 100.0}]


def page(keystone=8112, primary=8100, sub=8300, rest=(8139, 8138, 8135), sec=(8304, 8347)):
    """A rune page in the shape slim_perks produces."""
    return {
        "primaryStyle": primary,
        "primary": [keystone, *rest],
        "subStyle": sub,
        "secondary": list(sec),
        "shards": [5005, 5008, 5011],
    }


def garen_runes(*players):
    matches = [match([p]) for p in players]
    return build(matches)["regions"]["euw1"]["TOP"][0]


def test_rune_pages_by_frequency_with_selection_of_the_top_page():
    other_rest = page(rest=(8126, 8138, 8135))
    garen = garen_runes(
        player(1, "Garen", 100, "TOP", perks=page(), win=True),
        player(1, "Garen", 100, "TOP", perks=page(), win=False),
        player(1, "Garen", 100, "TOP", perks=other_rest, win=True),
        player(1, "Garen", 100, "TOP", perks=page(sub=8000)),
        player(1, "Garen", 100, "TOP", perks=page(keystone=8128)),
        player(1, "Garen", 100, "TOP", perks=page(primary=8200, keystone=8214)),
    )
    assert garen["rune_games"] == 6
    assert [(r["keystone"], r["games"], r["share"], r["win_rate"]) for r in garen["runes"]] == [
        (8112, 3, 50.0, 66.7),
        (8112, 1, 16.7, 100.0),
        (8128, 1, 16.7, 100.0),
    ]
    assert garen["runes"][0]["secondary_style"] == 8300
    assert garen["runes"][0]["selection"] == {
        "primary": [8139, 8138, 8135],
        "secondary": [8304, 8347],
        "shards": [5005, 5008, 5011],
        "games": 2,
    }
    assert "selection" not in garen["runes"][1]


def test_spell_pair_order_is_ignored_and_capped_at_two():
    garen = garen_runes(
        player(1, "Garen", 100, "TOP", perks=page(), spells=(4, 14)),
        player(1, "Garen", 100, "TOP", perks=page(), spells=(14, 4)),
        player(1, "Garen", 100, "TOP", perks=page(), spells=(4, 12)),
        player(1, "Garen", 100, "TOP", perks=page(), spells=(6, 12)),
    )
    assert garen["spells"] == [
        {"ids": [4, 14], "games": 2, "share": 50.0},
        {"ids": [4, 12], "games": 1, "share": 25.0},
    ]


def test_rune_and_spell_shares_are_over_games_with_rune_data_without_remakes():
    garen = garen_runes(
        player(1, "Garen", 100, "TOP", perks=page(), spells=(4, 14)),
        player(1, "Garen", 100, "TOP", perks=page(keystone=8128), spells=(4, 12)),
        # stored before runes were kept: no rune data, still a game
        player(1, "Garen", 100, "TOP"),
        player(1, "Garen", 100, "TOP", perks=page(keystone=8128), spells=(4, 12), remake=True),
    )
    assert (garen["count"], garen["item_games"], garen["rune_games"]) == (4, 3, 2)
    assert [(r["keystone"], r["share"]) for r in garen["runes"]] == [(8112, 50.0), (8128, 50.0)]
    assert [s["share"] for s in garen["spells"]] == [50.0, 50.0]


def test_champion_without_rune_data_has_empty_runes_and_spells():
    garen = garen_runes(player(1, "Garen", 100, "TOP"), player(1, "Garen", 100, "TOP"))
    assert (garen["rune_games"], garen["runes"], garen["spells"]) == (0, [], [])


def garen_game(items, enemy):
    """Garen top against one enemy champion; Darius has a physical share of 1, Lux of 0."""
    return match([player(1, "Garen", 100, "TOP", items=items), player(2, enemy, 200, "MIDDLE")])


def matchups(ad_games, other_games, boots=frozenset()):
    """Garen's matchup cards; ad_games and other_games are item lists, one per game."""
    damage = [
        match([player(2, c, 200, "MIDDLE", dmg=d)])
        for c, d in [("Darius", (1, 0, 0)), ("Lux", (0, 1, 0))]
    ] * 5
    matches = (
        damage
        + [garen_game(i, "Darius") for i in ad_games]
        + [garen_game(i, "Lux") for i in other_games]
    )
    data = build(matches, valid_items=LEGENDARY | boots, boots=boots)
    return by_name(data["regions"]["euw1"]["TOP"])["Garen"]["matchups"]


def test_matchup_card_lists_items_differing_by_twenty_points():
    # vs Heavy AD: 3001 in 8/10 (80%), others 3/10 (30%); 3002 in 5/10 vs 3/10 (20 points,
    # included); 3003 in 4/10 vs 3/10 (10 points, left out); boots 3020 in 0/10 vs 10/10.
    ad = [[3001, 3002, 3003]] * 3 + [[3001, 3002, 3003]] + [[3001, 3002]] + [[3001]] * 3 + [[]] * 2
    other = [[3001, 3002, 3003, 3020]] * 3 + [[3020]] * 7
    card = matchups(ad, other, boots={3020})["Heavy AD"]
    assert card == {
        "games": 10,
        "other_games": 10,
        "items": [
            {"id": 3020, "games": 0, "share": 0.0, "other_games": 10, "other_share": 100.0},
            {"id": 3001, "games": 8, "share": 80.0, "other_games": 3, "other_share": 30.0},
            {"id": 3002, "games": 5, "share": 50.0, "other_games": 3, "other_share": 30.0},
        ],
    }


def test_matchup_card_needs_ten_games_on_both_sides():
    assert "Heavy AD" in matchups([[3001]] * 10, [[]] * 10)
    assert matchups([[3001]] * 9, [[]] * 10) == {}
    assert matchups([[3001]] * 10, [[]] * 9) == {}


def test_champion_with_no_card_when_no_item_differs():
    assert matchups([[3001]] * 10, [[3001]] * 9 + [[]]) == {}


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


def test_aggregate_returns_all_by_pick_rate_and_data_json_keeps_top_15():
    summaries = []
    for n in range(20):
        p = {"role": "TOP", "name": "x", "champ": f"C{n}", "win": True, "k": 0, "d": 0, "a": 0}
        summaries += [
            {
                "bans": [],
                "participants": [{**p, "items": [], "context": [], "remake": False, "perks": None}],
            }
        ] * (n + 1)
    result = aggregate(summaries, "TOP", "euw1", {}, None, {})
    assert [c["name"] for c in result] == [f"C{n}" for n in range(19, -1, -1)]

    matches = [match([player(n, f"C{n}", 100, "TOP")]) for n in range(20) for _ in range(n + 1)]
    assert [c["name"] for c in build(matches)["regions"]["euw1"]["TOP"]] == [
        f"C{n}" for n in range(19, 4, -1)
    ]


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


# --- main() with a fake Riot client and a fake collection ---


def api_error(status):
    response = requests.Response()
    response.status_code = status
    return ApiError(response=response)


def field(doc, path):
    for key in path.split("."):
        doc = doc.get(key, {}) if isinstance(doc, dict) else {}
    return doc


def matches_query(doc, query):
    """The few MongoDB operators main.py uses; a missing field reads as {}."""
    for path, cond in query.items():
        if path == "$or":
            if not any(matches_query(doc, q) for q in cond):
                return False
            continue
        value = field(doc, path)
        if not isinstance(cond, dict):
            cond = {"$eq": cond}
        for op, arg in cond.items():
            if op == "$eq":
                ok = value == arg
            elif op == "$in":
                ok = value in arg
            elif op == "$nin":
                ok = value not in arg
            else:  # $exists
                ok = (value != {}) == arg
            if not ok:
                return False
    return True


class FakeCollection:
    def __init__(self, docs=()):
        self.docs = [{"_id": f"oid{i}", **d} for i, d in enumerate(docs)]
        self.deletes = []

    def find(self, query=None, projection=None):
        return [d for d in self.docs if matches_query(d, query or {})]

    def aggregate(self, pipeline):
        return []

    def insert_one(self, doc):
        self.docs.append({"_id": f"oid{len(self.docs)}", **doc})

    def update_one(self, query, update, upsert=False):
        hit = self.find(query)
        if hit:
            hit[0].update(update["$set"])
        elif upsert:
            self.docs.append({**query, **update["$set"]})

    def delete_many(self, query):
        self.deletes.append(query)
        gone = self.find(query)
        self.docs = [d for d in self.docs if d not in gone]
        return SimpleNamespace(deleted_count=len(gone))


def fake_watcher(
    failing_regions=(), status=500, lists=None, versions=None, calls=None, timeline=None
):
    """lists: region -> match ids listed; versions: match id -> game version (default 15.22);
    timeline: function (region, match id) -> timeline (default: one without events)."""
    lists = lists or {}
    versions = versions or {}

    def ladder(region, queue):
        if region in failing_regions:
            raise api_error(status)
        return {"entries": [{"puuid": f"pu-{region}", "leaguePoints": 1}]}

    def matchlist(region, puuid, count, queue):
        assert queue == 420
        return lists.get(region, [f"{region}_1"])

    def by_id(region, match_id):
        if calls is not None:
            calls.append(match_id)
        m = match(
            [player(1, "Garen", 100, "TOP")],
            region=region,
            version=versions.get(match_id, "15.22.1"),
        )
        m["metadata"]["matchId"] = match_id
        m["info"]["participants"][0]["puuid"] = "pu1"
        m["info"].update(gameDuration=1800, queueId=420)
        return m

    def empty_timeline(region, match_id):
        return {"info": {"participants": [{"participantId": 1, "puuid": "pu1"}], "frames": []}}

    return SimpleNamespace(
        league=SimpleNamespace(
            challenger_by_queue=ladder, grandmaster_by_queue=ladder, masters_by_queue=ladder
        ),
        match=SimpleNamespace(
            matchlist_by_puuid=matchlist,
            by_id=by_id,
            timeline_by_match=timeline or empty_timeline,
        ),
    )


# Data Dragon versions, newest first: with KEEP_PREVIOUS_PATCHES 2 the window is 15.22-15.20.
VERSIONS = ["15.22.1", "15.21.1", "15.20.1", "15.19.1", "15.18.1"]


def run_main(monkeypatch, tmp_path, watcher, col=None, old=None, versions=VERSIONS):
    col = col if col is not None else FakeCollection()
    old = old if old is not None else FakeCollection()
    monkeypatch.setenv("RIOT_API_KEY", "key")
    monkeypatch.setenv("MONGODB_URI", "uri")
    monkeypatch.setenv("PLAYER_COUNT", "1")
    monkeypatch.delenv("KEEP_PREVIOUS_PATCHES", raising=False)
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "DATA_FOLDER", str(tmp_path))
    monkeypatch.setattr(main, "OUTPUT_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(main, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(main, "LolWatcher", lambda key: watcher)
    monkeypatch.setattr(main, "RiotWatcher", lambda key: SimpleNamespace())
    monkeypatch.setattr(main, "connect_db", lambda uri: (col, old))
    monkeypatch.setattr(
        main, "load_static_data", lambda: (versions[0], set(), None, set(), versions)
    )
    return main.main(), col, tmp_path / "data.json"


def test_retention_window_is_current_and_previous_patches():
    versions = ["15.22.1", "15.21.2", "15.21.1", "15.20.1", "15.2.1"]
    assert main.retention_window(versions, 2) == ["15.22", "15.21", "15.20"]
    assert main.is_older("15.2", ["15.22", "15.21", "15.20"])
    assert not main.is_older("15.20", ["15.22", "15.21", "15.20"])


def test_second_run_over_the_same_lists_requests_no_match(monkeypatch, tmp_path):
    lists = {"kr": ["kr_3", "kr_2", "kr_1", "kr_0"], "euw1": ["euw1_1"]}
    versions = {"kr_2": "15.20.5", "kr_1": "15.19.1", "kr_0": "15.19.1"}
    calls = []
    watcher = fake_watcher(lists=lists, versions=versions, calls=calls)
    col, old = FakeCollection(), FakeCollection()
    run_main(monkeypatch, tmp_path, watcher, col, old)
    # kr_2 is inside the window and stored; kr_1 is outside, recorded, and ends the scan.
    # The regions run concurrently, so only the order within a region is fixed.
    assert [c for c in calls if c.startswith("kr")] == ["kr_3", "kr_2", "kr_1"]
    assert [c for c in calls if c.startswith("euw1")] == ["euw1_1"]
    assert sorted(d["metadata"]["matchId"] for d in col.docs) == ["euw1_1", "kr_2", "kr_3"]
    assert old.docs == [{"_id": "kr_1", "seen": "15.22"}]
    calls.clear()
    code, _, _ = run_main(monkeypatch, tmp_path, watcher, col, old)
    assert code == 0 and calls == []
    assert len(col.docs) == 3 and len(old.docs) == 1


def test_list_returning_after_the_window_moved_on_requests_no_match(monkeypatch, tmp_path):
    calls = []
    watcher = fake_watcher(
        lists={"kr": ["kr_1", "kr_0"]}, versions={"kr_1": "15.19.1"}, calls=calls
    )
    col, old = FakeCollection(), FakeCollection()
    run_main(monkeypatch, tmp_path, watcher, col, old)
    assert sorted(calls) == ["euw1_1", "kr_1"]
    # The player drops off the ladder for longer than the window.
    later = ["15.26.1", "15.25.1", "15.24.1", "15.23.1", "15.22.1"]
    away = fake_watcher(lists={"kr": [], "euw1": []})
    run_main(monkeypatch, tmp_path, away, col, old, versions=later)
    calls.clear()
    run_main(monkeypatch, tmp_path, watcher, col, old, versions=later)
    assert "kr_1" not in calls and "kr_0" not in calls


def test_data_json_ignores_stored_matches_of_previous_patches():
    current = match([player(1, "Garen", 100, "TOP")], version="15.22.1")
    previous = match([player(2, "Darius", 100, "TOP")], version="15.21.1")
    data = build([current, previous])
    assert data["meta"]["total_games"] == 1
    assert [c["name"] for c in data["regions"]["euw1"]["TOP"]] == ["Garen"]


def test_purge_removes_non_ranked_and_out_of_window_matches_and_keeps_id_records():
    def doc(match_id, version, **info):
        return {"metadata": {"matchId": match_id}, "info": {"gameVersion": version, **info}}

    col = FakeCollection(
        [
            doc("ranked", "15.22.1", queueId=420),
            doc("no_queue", "15.22.1"),
            doc("flex", "15.22.1", queueId=440),
            doc("previous", "15.20.1", queueId=420),
            doc("too_old", "15.19.1", queueId=420),
            doc("prefix", "15.2.1", queueId=420),
        ]
    )
    old = FakeCollection()
    main.purge_old_patches(col, old, ["15.22", "15.21", "15.20"])
    assert [d["metadata"]["matchId"] for d in col.docs] == ["ranked", "no_queue", "previous"]
    assert old.docs == [{"_id": "too_old", "seen": "15.22"}, {"_id": "prefix", "seen": "15.22"}]


def test_match_aging_out_while_its_player_is_away_is_not_downloaded_again(monkeypatch, tmp_path):
    calls = []
    watcher = fake_watcher(lists={"kr": ["kr_1"]}, versions={"kr_1": "15.22.1"}, calls=calls)
    col, old = FakeCollection(), FakeCollection()
    run_main(monkeypatch, tmp_path, watcher, col, old)
    assert sorted(calls) == ["euw1_1", "kr_1"]
    # The player is off the ladder while kr_1 ages out of the window and is purged.
    later = ["15.26.1", "15.25.1", "15.24.1", "15.23.1", "15.22.1"]
    away = fake_watcher(lists={"kr": [], "euw1": []})
    run_main(monkeypatch, tmp_path, away, col, old, versions=later)
    assert col.docs == []
    calls.clear()
    run_main(monkeypatch, tmp_path, watcher, col, old, versions=later)
    assert "kr_1" not in calls


def test_rejected_key_stops_before_purge_and_write(monkeypatch, tmp_path, caplog):
    code, col, out = run_main(monkeypatch, tmp_path, fake_watcher(("kr", "euw1"), status=401))
    assert code != 0
    assert not out.exists()
    assert col.deletes == []
    assert "rejected the key" in caplog.text


def test_every_region_failing_stops_before_purge_and_write(monkeypatch, tmp_path):
    code, col, out = run_main(monkeypatch, tmp_path, fake_watcher(("kr", "euw1")))
    assert code != 0
    assert not out.exists()
    assert col.deletes == []


def test_one_failing_region_still_writes(monkeypatch, tmp_path):
    code, col, out = run_main(monkeypatch, tmp_path, fake_watcher(("kr",)))
    assert code == 0
    assert len(col.deletes) == 1
    data = json.loads(out.read_text())
    assert data["meta"]["total_games"] == 1
    assert data["regions"]["euw1"]["TOP"][0]["name"] == "Garen"


def crawl_regions(watcher):
    return main.crawl(watcher, SimpleNamespace(), FakeCollection(), FakeCollection(), ["15.22"], 1)


def test_ordinary_error_in_one_region_still_scans_the_other():
    results = crawl_regions(fake_watcher(("kr",)))
    assert results["kr"]["ok"] is False
    assert results["euw1"]["ok"] is True and results["euw1"]["downloaded"] == 1


def test_rejected_key_in_one_region_fails_the_run(monkeypatch, tmp_path):
    with pytest.raises(main.RiotAuthError):
        crawl_regions(fake_watcher(("euw1",), status=403))
    code, _, out = run_main(monkeypatch, tmp_path, fake_watcher(("euw1",), status=403))
    assert code != 0 and not out.exists()


def test_crawl_counts_requests_and_downloads_per_region(caplog):
    caplog.set_level("INFO")
    results = crawl_regions(fake_watcher(("kr",)))
    # euw1: challenger ladder, match list, one match and its timeline; kr: only the failing
    # ladder request.
    assert {k: (r["requests"], r["downloaded"]) for k, r in results.items()} == {
        "kr": (1, 0),
        "euw1": (4, 1),
    }
    assert all(r["seconds"] >= 0 for r in results.values())
    assert {rec.threadName for rec in caplog.records if "Scan" in rec.message} == {"kr", "euw1"}


def test_insert_error_other_than_duplicate_is_logged(monkeypatch, tmp_path, caplog):
    def insert_one(doc):
        raise DuplicateKeyError("dup") if doc["_region"] == "kr" else OSError("disk full")

    monkeypatch.setattr(FakeCollection, "insert_one", lambda self, doc: insert_one(doc))
    code, _, _ = run_main(monkeypatch, tmp_path, fake_watcher())
    assert code == 0
    assert "Could not save euw1_1: disk full" in caplog.text
    assert "Could not save kr_1" not in caplog.text


# --- Match timelines ---


def ev(kind, pid, t, **fields):
    return {"type": kind, "participantId": pid, "timestamp": t, **fields}


# Timeline participant ids deliberately differ from the match's participant order.
TIMELINE = {
    "info": {
        "participants": [
            {"participantId": 1, "puuid": "pu2"},
            {"participantId": 2, "puuid": "pu1"},
            {"participantId": 3, "puuid": "not-in-match"},
        ],
        "frames": [
            {
                "events": [
                    ev("ITEM_PURCHASED", 2, 100, itemId=1055),  # starter, not a valid item
                    ev("ITEM_PURCHASED", 2, 900, itemId=3031),
                    ev("ITEM_PURCHASED", 2, 950, itemId=3006),
                    ev("ITEM_UNDO", 2, 960, beforeId=3006, afterId=0, goldGain=1000),
                    ev("SKILL_LEVEL_UP", 2, 1000, skillSlot=1, levelUpType="NORMAL"),
                    ev("SKILL_LEVEL_UP", 2, 1100, skillSlot=4, levelUpType="EVOLVE"),
                    ev("ITEM_PURCHASED", 1, 1200, itemId=3089),
                    ev("ITEM_PURCHASED", 3, 1200, itemId=3089),
                ]
            },
            {
                "events": [
                    ev("ITEM_SOLD", 2, 2000, itemId=3031),
                    ev("ITEM_SOLD", 1, 2100, itemId=3089),
                    ev("ITEM_UNDO", 1, 2110, beforeId=0, afterId=3089, goldGain=-2000),
                    ev("WARD_PLACED", 2, 2200),
                ]
            },
        ],
    }
}


def test_slim_timeline_applies_undo_keeps_sales_and_drops_evolves():
    slim = main.slim_timeline(TIMELINE, {"pu1", "pu2"}, {3031, 3006, 3089})
    assert slim == {
        "pu1": {
            "items": [
                {"t": 900, "e": "buy", "id": 3031},
                {"t": 2000, "e": "sell", "id": 3031},
            ],
            "skills": [{"t": 1000, "slot": 1}],
        },
        # Matched by puuid: participant 1 of the timeline is pu2; the undone sale is gone.
        "pu2": {"items": [{"t": 1200, "e": "buy", "id": 3089}], "skills": []},
    }


def test_crawl_stores_the_slim_timeline_with_the_match():
    def timeline(region, match_id):
        return {
            "info": {
                "participants": [{"participantId": 1, "puuid": "pu1"}],
                "frames": [{"events": [ev("ITEM_PURCHASED", 1, 5, itemId=3031)]}],
            }
        }

    col = FakeCollection()
    main.crawl(
        fake_watcher(("kr",), timeline=timeline),
        SimpleNamespace(),
        col,
        FakeCollection(),
        ["15.22"],
        1,
        {3031},
    )
    [doc] = col.docs
    assert doc["timeline"] == {"pu1": {"items": [{"t": 5, "e": "buy", "id": 3031}], "skills": []}}


def test_failed_timeline_leaves_match_stored_and_a_later_run_fetches_it(monkeypatch, tmp_path):
    def broken(region, match_id):
        raise api_error(500)

    col = FakeCollection()
    code, _, _ = run_main(monkeypatch, tmp_path, fake_watcher(timeline=broken), col)
    assert code == 0
    assert sorted(d["metadata"]["matchId"] for d in col.docs) == ["euw1_1", "kr_1"]
    assert all("timeline" not in d for d in col.docs)
    calls = []
    code, _, _ = run_main(monkeypatch, tmp_path, fake_watcher(calls=calls), col)
    assert code == 0 and calls == []  # matches not downloaded again, only their timelines
    assert all(d["timeline"] == {"pu1": {"items": [], "skills": []}} for d in col.docs)


def test_timeline_not_found_is_marked_and_not_asked_again(monkeypatch, tmp_path):
    asked = []

    def gone(region, match_id):
        asked.append(match_id)
        raise api_error(404)

    col = FakeCollection()
    run_main(monkeypatch, tmp_path, fake_watcher(timeline=gone), col)
    assert all(d["timeline"] is None for d in col.docs)
    asked.clear()
    run_main(monkeypatch, tmp_path, fake_watcher(timeline=gone), col)
    assert asked == []


def test_missing_timelines_of_previous_patches_are_not_fetched():
    col = FakeCollection([stored("prev", "15.21.1"), stored("cur", "15.22.1")])
    assert main.fetch_missing_timelines(fake_watcher(), col, "15.22", None) == 1
    assert [("timeline" in d) for d in col.docs] == [False, True]


def test_rejected_key_on_timeline_stops_the_run(monkeypatch, tmp_path):
    def rejected(region, match_id):
        raise api_error(403)

    code, col, out = run_main(monkeypatch, tmp_path, fake_watcher(timeline=rejected))
    assert code != 0 and not out.exists() and col.docs == []


# --- Data Dragon item selection ---


def item(cost, buy=True, rift=True, tags=(), frm=None, into=None, recipe=None):
    """A Data Dragon item.json entry with only the fields select_items reads."""
    d = {"gold": {"purchasable": buy, "total": cost}, "maps": {"11": rift}, "tags": list(tags)}
    d.update({k: v for k, v in (("from", frm), ("into", into), ("specialRecipe", recipe)) if v})
    return d


ITEMS = {
    "3031": item(3500, frm=["1038"]),  # Infinity Edge, depth 2
    "1038": item(1300, into=["3031"]),  # B. F. Sword, a component
    "2003": item(50, tags=["Consumable"]),  # Health Potion
    "1054": item(450),  # Doran's Shield, starting item
    "3009": item(3000, rift=False),  # finished, but not on Summoner's Rift
    "1001": item(300, tags=["Boots"], into=["3020"]),  # tier 1 boots
    "3020": item(1100, tags=["Boots"], frm=["1001"], into=["3175"]),  # tier 2 boots
    "3004": item(2900),  # Manamune
    "3042": item(2900, buy=False, recipe=3004),  # Muramana, transformed
    "3866": item(400, buy=False, recipe=3865),  # Runic Compass, turns into Bounty of Worlds
    "3867": item(400, buy=False, recipe=3866, into=["3869"]),  # Bounty of Worlds
    "3869": item(400, frm=["3867"]),  # Celestial Opposition, support upgrade
}


def test_select_items_keeps_finished_and_transformed_items():
    finished, boots = select_items(ITEMS)
    assert finished == {3031, 3004, 3042, 3869}
    assert boots == {3020}


# --- history.json ---

BACKFILL = (
    '{"patch":"16.2","snapshot_date":"2026-02-03","source":"backfill","complete":false,'
    '"commit":"e4e488c","regions":{"kr":{"games":2242,"games_estimated":true,"roles":'
    '{"TOP":[{"name":"Rumble","games":199,"pick_rate":8.9,"win_rate":45.2,"ban_rate":27.2}]}}}}'
)


def regions(games=1):
    return {"kr": {"games": games, "games_estimated": False, "roles": {}}}


def test_update_history_adds_new_patch_in_numeric_order():
    history = {"schema": 1, "patches": [json.loads(BACKFILL)]}
    h = update_history(history, "16.10", regions(), "2026-06-01")
    h = update_history(h, "16.9", regions(), "2026-05-15")
    assert [p["patch"] for p in h["patches"]] == ["16.2", "16.9", "16.10"]
    assert h["patches"][2] == {
        "patch": "16.10",
        "snapshot_date": "2026-06-01",
        "source": "generator",
        "complete": True,
        "regions": regions(),
    }
    assert json.dumps(h["patches"][0], separators=(",", ":")) == BACKFILL
    assert history["patches"] == [json.loads(BACKFILL)]  # input not mutated


def test_update_history_replaces_current_patch_entry():
    h = update_history({"schema": 1, "patches": []}, "16.21", regions(1), "2026-11-14")
    h = update_history(h, "16.21", regions(5), "2026-11-15")
    assert [(p["patch"], p["snapshot_date"]) for p in h["patches"]] == [("16.21", "2026-11-15")]
    assert h["patches"][0]["regions"]["kr"]["games"] == 5


def test_update_history_ignores_patch_without_matches():
    history = {"schema": 1, "patches": [json.loads(BACKFILL)]}
    assert update_history(history, "16.2", regions(0), "2026-11-15") == history


def test_patch_regions_lists_every_picked_champion_of_the_patch():
    matches = [
        match([player(n, f"C{n}", 100, "TOP"), player(99, "Garen", 200, "TOP")], bans=[98])
        for n in range(20)
    ]
    matches += [
        match([player(98, "Darius", 100, "TOP")], version="16.20.1"),
        match([player(1, "Ahri", 100, "MIDDLE")], region="kr", version="16.21.1"),
    ]
    out = main.patch_regions(matches, "16.21")
    assert out["kr"]["games"] == 1 and out["kr"]["roles"]["MIDDLE"][0]["name"] == "Ahri"
    assert out["euw1"] == {"games": 0, "games_estimated": False, "roles": out["euw1"]["roles"]}

    out = main.patch_regions(matches, "15.22")
    top = out["euw1"]["roles"]["TOP"]
    assert out["euw1"]["games"] == 20
    assert len(top) == 21  # all 20 + Garen, not the top 15; Darius (16.20, banned) left out
    assert top[0] == {
        "name": "Garen",
        "games": 20,
        "pick_rate": 50.0,
        "win_rate": 100.0,
        "ban_rate": 0,
    }


def test_main_writes_history_and_keeps_other_patches(monkeypatch, tmp_path):
    (tmp_path / "history.json").write_text('{"schema":1,"patches":[' + BACKFILL + "]}")
    code, _, _ = run_main(monkeypatch, tmp_path, fake_watcher())
    assert code == 0
    h = json.loads((tmp_path / "history.json").read_text())
    assert [p["patch"] for p in h["patches"]] == ["15.22", "16.2"]
    assert h["patches"][0]["regions"]["euw1"]["games"] == 1
    assert json.dumps(h["patches"][1], separators=(",", ":")) == BACKFILL


def test_unparsable_or_wrong_schema_history_stops_before_writing(monkeypatch, tmp_path):
    for bad in ("{not json", '{"schema": 2, "patches": []}'):
        (tmp_path / "history.json").write_text(bad)
        code, col, out = run_main(monkeypatch, tmp_path, fake_watcher())
        assert code != 0
        assert not out.exists()
        assert col.docs == [] and col.deletes == []
        assert (tmp_path / "history.json").read_text() == bad
