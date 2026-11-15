"""Unit tests for src/main.py on hand-written fixtures; no Riot API, no MongoDB."""

import json
import os
from types import SimpleNamespace

import requests
from pymongo.errors import DuplicateKeyError
from riotwatcher import ApiError

import main
from main import (
    aggregate,
    aggregate_regions,
    analyze_enemy_comp,
    build_frontend_data,
    extract_bans,
    get_short_version,
    load_history,
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


def build(matches, valid_items=None, tank_champs=frozenset(), ddragon_version="15.22.1"):
    regions, player_performance = aggregate_regions(matches, tank_champs, valid_items)
    return build_frontend_data(
        len(matches), ddragon_version, 10, regions, player_performance, "now"
    )


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


def test_aggregate_keeps_all_and_data_json_keeps_top_15_by_pick_rate():
    summaries = []
    matches = []
    for n in range(20):
        p = {"role": "TOP", "name": "x", "champ": f"C{n}", "win": True, "k": 0, "d": 0, "a": 0}
        summaries += [{"bans": [], "participants": [{**p, "items": [], "context": []}]}] * (n + 1)
        matches += [match([player(n, f"C{n}", 100, "TOP")])] * (n + 1)
    result = aggregate(summaries, "TOP", "euw1", {}, None, {})
    assert [c["name"] for c in result] == [f"C{n}" for n in range(19, -1, -1)]
    top = build(matches)["regions"]["euw1"]["TOP"]
    assert [c["name"] for c in top] == [f"C{n}" for n in range(19, 4, -1)]


def test_meta_and_regions_shape():
    matches = [match([player(1, "Garen", 100, "TOP")]), match([], region="na1")]
    regions, player_performance = aggregate_regions(matches, set(), None)
    data = build_frontend_data(2, None, 7, regions, player_performance, "2026-11-15 00:00 UTC")
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
    monkeypatch.setattr(main, "load_static_data", lambda: ("15.22.1", set(), None))
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


# --- per-patch archive (history.json) ---

BACKFILL = {
    "patch": "16.2",
    "snapshot_date": "2026-02-03",
    "source": "backfill",
    "complete": False,
    "commit": "e4e488c",
    "regions": {
        "kr": {
            "games": 2242,
            "games_estimated": True,
            "roles": {
                "TOP": [
                    {
                        "name": "Rumble",
                        "games": 199,
                        "pick_rate": 8.9,
                        "win_rate": 60.0,
                        "ban_rate": 27.2,
                    }
                ]
            },
        }
    },
}


def regions_for(matches):
    return aggregate_regions(matches, set(), None)[0]


def two_games():
    return [
        match([player(1, "Garen", 100, "TOP"), player(2, "Darius", 200, "TOP", win=False)]),
        match([player(1, "Garen", 100, "TOP", win=False)], bans=[2], region="kr"),
    ]


def test_new_patch_entry_has_every_picked_champion():
    history = update_history({"schema": 1, "patches": []}, "16.21", regions_for(two_games()), "d")
    [entry] = history["patches"]
    assert entry["patch"] == "16.21"
    assert entry["snapshot_date"] == "d"
    assert entry["source"] == "generator"
    assert entry["complete"] is True
    assert "commit" not in entry
    assert entry["regions"]["euw1"]["games"] == 1
    assert entry["regions"]["euw1"]["games_estimated"] is False
    assert entry["regions"]["euw1"]["roles"]["TOP"] == [
        {"name": "Garen", "games": 1, "pick_rate": 50.0, "win_rate": 100.0, "ban_rate": 0.0},
        {"name": "Darius", "games": 1, "pick_rate": 50.0, "win_rate": 0, "ban_rate": 0.0},
    ]
    assert list(entry["regions"]["euw1"]["roles"]["TOP"][0]) == list(
        BACKFILL["regions"]["kr"]["roles"]["TOP"][0]
    )
    # Darius was banned in kr but not picked there: not in the archive.
    assert [c["name"] for c in entry["regions"]["kr"]["roles"]["TOP"]] == ["Garen"]


def test_archive_keeps_champions_beyond_top_15():
    matches = [match([player(n, f"C{n}", 100, "TOP")]) for n in range(20)]
    entry = update_history({"schema": 1, "patches": []}, "16.21", regions_for(matches), "d")
    assert len(entry["patches"][0]["regions"]["euw1"]["roles"]["TOP"]) == 20


def test_second_run_replaces_current_patch_entry():
    first = update_history({"schema": 1, "patches": []}, "16.21", regions_for(two_games()), "a")
    second = update_history(first, "16.21", regions_for(two_games()[:1]), "b")
    [entry] = second["patches"]
    assert entry["snapshot_date"] == "b"
    assert entry["regions"]["kr"]["games"] == 0


def test_backfill_entries_unchanged_and_patches_in_numeric_order(tmp_path):
    path = tmp_path / "history.json"
    later = {**BACKFILL, "patch": "16.10"}
    path.write_text(json.dumps({"schema": 1, "patches": [BACKFILL, later]}))
    before = [json.dumps(p) for p in load_history(str(path))["patches"]]

    main.save_history(
        str(path), update_history(load_history(str(path)), "16.9", regions_for(two_games()), "d")
    )
    patches = load_history(str(path))["patches"]
    assert [p["patch"] for p in patches] == ["16.2", "16.9", "16.10"]
    assert [json.dumps(patches[0]), json.dumps(patches[2])] == before


def test_zero_matches_leaves_history_unchanged():
    history = {"schema": 1, "patches": [BACKFILL]}
    assert update_history(history, "16.2", regions_for([]), "d") is history


def test_missing_history_file_starts_empty(tmp_path):
    assert load_history(str(tmp_path / "none.json")) == {"schema": 1, "patches": []}


def test_unusable_history_stops_run_before_writing(monkeypatch, tmp_path):
    for content in ("{not json", '{"schema": 2, "patches": []}'):
        (tmp_path / "history.json").write_text(content)
        code, col, out = run_main(monkeypatch, tmp_path, fake_watcher())
        assert code != 0
        assert not out.exists()
        assert col.docs == [] and col.deletes == []
        assert (tmp_path / "history.json").read_text() == content


def test_successful_run_writes_history(monkeypatch, tmp_path):
    code, _, _ = run_main(monkeypatch, tmp_path, fake_watcher())
    assert code == 0
    history = json.loads((tmp_path / "history.json").read_text())
    assert [p["patch"] for p in history["patches"]] == ["15.22"]
    assert history["patches"][0]["regions"]["kr"]["games"] == 1
    assert not os.path.exists(tmp_path / "history.json.tmp")
