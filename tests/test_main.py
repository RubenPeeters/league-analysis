"""Unit tests for src/main.py on hand-written fixtures; no Riot API, no MongoDB."""

import json
from types import SimpleNamespace

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
    select_items,
    slim_match,
    update_history,
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
        summaries += [{"bans": [], "participants": [{**p, "items": [], "context": []}]}] * (n + 1)
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


class FakeCollection:
    def __init__(self):
        self.docs = []
        self.deletes = []

    def find(self, query=None, projection=None):
        return list(self.docs) if not query else []

    def aggregate(self, pipeline):
        return []

    def insert_one(self, doc):
        self.docs.append(doc)

    def delete_many(self, query):
        self.deletes.append(query)
        return SimpleNamespace(deleted_count=0)


def fake_watcher(failing_regions=(), status=500):
    def ladder(region, queue):
        if region in failing_regions:
            raise api_error(status)
        return {"entries": [{"puuid": f"pu-{region}", "leaguePoints": 1}]}

    def by_id(region, match_id):
        return match([player(1, "Garen", 100, "TOP")], region=region, version="15.22.1")

    return SimpleNamespace(
        league=SimpleNamespace(
            challenger_by_queue=ladder, grandmaster_by_queue=ladder, masters_by_queue=ladder
        ),
        match=SimpleNamespace(
            matchlist_by_puuid=lambda region, puuid, count: [f"{region}_1"], by_id=by_id
        ),
    )


def run_main(monkeypatch, tmp_path, watcher):
    col = FakeCollection()
    monkeypatch.setenv("RIOT_API_KEY", "key")
    monkeypatch.setenv("MONGODB_URI", "uri")
    monkeypatch.setenv("PLAYER_COUNT", "1")
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "DATA_FOLDER", str(tmp_path))
    monkeypatch.setattr(main, "OUTPUT_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(main, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(main, "LolWatcher", lambda key: watcher)
    monkeypatch.setattr(main, "RiotWatcher", lambda key: SimpleNamespace())
    monkeypatch.setattr(main, "connect_db", lambda uri: col)
    monkeypatch.setattr(main, "load_static_data", lambda: ("15.22.1", set(), None, set()))
    return main.main(), col, tmp_path / "data.json"


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


def test_insert_error_other_than_duplicate_is_logged(monkeypatch, tmp_path, caplog):
    def insert_one(doc):
        raise DuplicateKeyError("dup") if doc["_region"] == "kr" else OSError("disk full")

    monkeypatch.setattr(FakeCollection, "insert_one", lambda self, doc: insert_one(doc))
    code, _, _ = run_main(monkeypatch, tmp_path, fake_watcher())
    assert code == 0
    assert "Could not save euw1_1: disk full" in caplog.text
    assert "Could not save kr_1" not in caplog.text


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
