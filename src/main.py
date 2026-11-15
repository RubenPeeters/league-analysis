import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from riotwatcher import ApiError, LolWatcher, RiotWatcher

logger = logging.getLogger(__name__)

# --- 1. CONFIGURATION ---
REGIONS = [("kr", "Korea"), ("euw1", "Europe West")]
VALID_ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
MATCH_HISTORY_COUNT = 100

# Fallback pro players for season reset (when ladder is empty)
FALLBACK_PRO_PLAYERS = {
    "kr": [
        # Top Korean Pro Players (Riot ID format: GameName#TAG)
        "Faker#KR1",
        "Zeus#0000",
        "Oner#KR1",
        "Gumayusi#KR1",
        "Keria#KR1",
        "Chovy#KR1",
        "Doran#KR1",
        "Peanut#KR1",
        "Viper#KR1",
        "Delight#KR1",
        "ShowMaker#KR1",
        "Canyon#KR1",
        "Aiming#KR1",
        "Kellin#KR1",
        "Kiin#KR1",
        "Peyz#KR1",
        "Zeka#KR1",
        "Ruler#KR1",
        "Deft#KR1",
        "BeryL#KR1",
        "Canna#KR1",
        "Teddy#KR1",
        "Effort#KR1",
        "Life#KR1",
    ],
    "euw1": [
        # Top European Pro Players
        "Caps#EUW",
        "Upset#EUW",
        "Mikyx#EUW",
        "Jankos#EUW",
        "Rekkles#3737",
        "Inspired#EUW",
        "Hans sama#EUW",
        "Elyoya#EUW",
        "Humanoid#EUW",
        "Razork#EUW",
        "Comp#EUW",
        "Targamas#EUW",
        "Odoamne#EUW",
        "Larssen#EUW",
        "Irrelevant#EUW",
        "Labrov#EUW",
        "Alphari#EUW",
        "Jun#EUW",
        "Kaiser#EUW",
        "Crownie#EUW",
        "Nuclearint#EUW",
        "Bwipo#EUW",
        "Nisqy#EUW",
        "Hylissang#EUW",
    ],
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FOLDER = os.path.join(SCRIPT_DIR, "..", "data")
OUTPUT_FILE = os.path.join(DATA_FOLDER, "data.json")
HISTORY_FILE = os.path.join(DATA_FOLDER, "history.json")


# --- 2. DATABASE ---
def connect_db(mongo_uri):
    try:
        import certifi

        client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
        db = client["league_tracker"]
        matches_col = db["matches"]
        matches_col.create_index("metadata.matchId", unique=True)
        logger.info("Connected to MongoDB Atlas")
        return matches_col
    except Exception as e:
        logger.critical(f"DB Error: {e}")
        raise


# --- 3. STATIC DATA (Context & Item Filtering) ---
# Cheapest finished non-boot item in 16.22.1 is Mejai's Soulstealer (1500); the dearest
# starting item is a Guardian's item (950). 1000 sits between them.
FINISHED_MIN_GOLD = 1000


def select_items(item_data):
    """Return (finished, boots), sets of item ids, from the "data" of Data Dragon item.json."""
    pending = {d["specialRecipe"] for d in item_data.values() if d.get("specialRecipe")}

    def transformed(i_id, d):
        # Reached without a purchase: a special recipe (Muramana) or built only from items
        # that cannot be bought (support upgrades from Bounty of Worlds). Only the end of the
        # chain counts, so Runic Compass (which turns into Bounty of Worlds) is dropped.
        frm = d.get("from") or []
        from_unbuyable = frm and all(
            not item_data.get(f, {}).get("gold", {}).get("purchasable", True) for f in frm
        )
        return bool(d.get("specialRecipe") or from_unbuyable) and int(i_id) not in pending

    finished, boots = set(), set()
    for i_id, d in item_data.items():
        tags = d.get("tags", [])
        if not d.get("maps", {}).get("11") or {"Consumable", "Trinket"} & set(tags):
            continue
        gold = d.get("gold", {})
        is_transformed = transformed(i_id, d)
        if "Boots" in tags:
            # Tier 1 boots are the ones built from nothing; tier 2 boots keep their `into`.
            if d.get("from") and (gold.get("purchasable") or is_transformed):
                boots.add(int(i_id))
        elif not d.get("into") and (
            is_transformed
            or (gold.get("purchasable") and gold.get("total", 0) >= FINISHED_MIN_GOLD)
        ):
            finished.add(int(i_id))
    return finished, boots


def load_static_data():
    """Return (latest_ver, tank_champs, valid_items, boots) from Data Dragon.

    Whatever was loaded before a failure is kept: latest_ver None, tank_champs empty,
    valid_items None (no item filtering) and boots empty are the fallbacks.
    """
    latest_ver = None
    tank_champs = set()
    valid_items = None
    boots = set()

    try:
        # 1. Get Version
        ver_res = requests.get("https://ddragon.leagueoflegends.com/api/versions.json")
        if ver_res.ok:
            latest_ver = ver_res.json()[0]

            # 2. Get Champions (For Tank Context)
            champ_res = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{latest_ver}/data/en_US/champion.json"
            )
            if champ_res.ok:
                c_data = champ_res.json()["data"]
                tank_champs = {name for name, d in c_data.items() if "Tank" in d["tags"]}
                logger.info(f"Loaded {len(tank_champs)} Tank definitions.")

            # 3. Get Items (For Filtering Components)
            item_res = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{latest_ver}/data/en_US/item.json"
            )
            if item_res.ok:
                finished, boots = select_items(item_res.json()["data"])
                valid_items = finished | boots

                logger.info(f"Loaded {len(valid_items)} Valid Items (Filtered out components).")

    except Exception as e:  # noqa: BLE001 - static data is optional, the run goes on without it
        logger.error(f"Static Data Error: {e}")

    return latest_ver, tank_champs, valid_items, boots


# --- 4. HELPER FUNCTIONS ---
class RiotAuthError(Exception):
    """The Riot API rejected the key (401/403); no later call can succeed."""


def smart_request(func, *args, **kwargs):
    while True:
        try:
            return func(*args, **kwargs)
        except ApiError as e:
            if e.response.status_code == 429:
                time.sleep(int(e.response.headers.get("Retry-After", 10)) + 1)
                continue
            elif e.response.status_code == 404:
                return None
            elif e.response.status_code in (401, 403):
                raise RiotAuthError(f"Riot API rejected the key: {e}") from e
            else:
                raise
        except Exception as e:
            logger.error(f"Error: {e}")
            raise


def get_short_version(game_version):
    if not game_version:
        return "0.0"
    parts = game_version.split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else parts[0]


def extract_bans(match_info):
    return [
        b["championId"] for t in match_info["teams"] for b in t["bans"] if b["championId"] != -1
    ]


def slim_perks(perks):
    """The rune page of a participant's perks, or None when the response has no perks."""
    if not perks:
        return None
    styles = {s.get("description"): s for s in perks.get("styles", [])}
    primary = styles.get("primaryStyle", {})
    sub = styles.get("subStyle", {})
    shards = perks.get("statPerks", {})
    return {
        "primaryStyle": primary.get("style"),
        "primary": [s.get("perk") for s in primary.get("selections", [])],
        "subStyle": sub.get("style"),
        "secondary": [s.get("perk") for s in sub.get("selections", [])],
        "shards": [shards.get("offense"), shards.get("flex"), shards.get("defense")],
    }


def slim_match(match_detail, region_code):
    """Extract only the fields needed for analysis to reduce storage."""
    info = match_detail.get("info", {})

    slim_participants = []
    for p in info.get("participants", []):
        slim_participants.append(
            {
                "puuid": p.get("puuid"),
                "championId": p.get("championId"),
                "championName": p.get("championName"),
                "teamId": p.get("teamId"),
                "teamPosition": p.get("teamPosition"),
                "win": p.get("win"),
                "kills": p.get("kills"),
                "deaths": p.get("deaths"),
                "assists": p.get("assists"),
                "item0": p.get("item0", 0),
                "item1": p.get("item1", 0),
                "item2": p.get("item2", 0),
                "item3": p.get("item3", 0),
                "item4": p.get("item4", 0),
                "item5": p.get("item5", 0),
                "riotIdGameName": p.get("riotIdGameName"),
                "riotIdTagline": p.get("riotIdTagline"),
                "summonerName": p.get("summonerName"),
                "physicalDamageDealtToChampions": p.get("physicalDamageDealtToChampions", 0),
                "magicDamageDealtToChampions": p.get("magicDamageDealtToChampions", 0),
                "trueDamageDealtToChampions": p.get("trueDamageDealtToChampions", 0),
                "perks": slim_perks(p.get("perks")),
                "summoner1Id": p.get("summoner1Id"),
                "summoner2Id": p.get("summoner2Id"),
                "gameEndedInEarlySurrender": p.get("gameEndedInEarlySurrender"),
            }
        )

    slim_teams = []
    for team in info.get("teams", []):
        slim_teams.append(
            {
                "teamId": team.get("teamId"),
                "bans": team.get("bans", []),
            }
        )

    return {
        "_region": region_code,
        "metadata": {
            "matchId": match_detail.get("metadata", {}).get("matchId"),
        },
        "info": {
            "gameVersion": info.get("gameVersion"),
            "gameCreation": info.get("gameCreation"),
            # Always written, also as None: its presence marks a match slimmed with these fields.
            "gameDuration": info.get("gameDuration"),
            "teams": slim_teams,
            "participants": slim_participants,
        },
    }


MIN_SHARE_GAMES = 5
MATCHUP_MIN_GAMES = 10
MATCHUP_MIN_DIFF = 20  # percentage points


def champion_damage_shares(matches):
    """Per champion with at least MIN_SHARE_GAMES stored games (any role, any region): its
    (physical, magic) damage to champions as shares of its total damage to champions."""
    totals = {}
    for m in matches:
        for p in m.get("info", {}).get("participants", []):
            t = totals.setdefault(p["championName"], [0, 0, 0, 0])
            phys = p.get("physicalDamageDealtToChampions", 0)
            magic = p.get("magicDamageDealtToChampions", 0)
            t[0] += 1
            t[1] += phys
            t[2] += magic
            t[3] += phys + magic + p.get("trueDamageDealtToChampions", 0)
    return {
        c: (phys / total, magic / total)
        for c, (games, phys, magic, total) in totals.items()
        if games >= MIN_SHARE_GAMES and total > 0
    }


def analyze_enemy_comp(match_info, my_team_id, tank_champs, damage_shares):
    """Enemy type from the enemy champions, not from the damage they dealt in this game (#22)."""
    enemies = [p["championName"] for p in match_info["participants"] if p["teamId"] != my_team_id]
    known = [damage_shares[c] for c in enemies if c in damage_shares]

    tags = []
    if known:
        phys = sum(s[0] for s in known) / len(known)
        magic = sum(s[1] for s in known) / len(known)
        if phys > 0.65:
            tags.append("Heavy AD")
        elif magic > 0.60:
            tags.append("Heavy AP")

    if sum(1 for c in enemies if c in tank_champs) >= 2:
        tags.append("Tank Heavy")

    return tags


def matchup_cards(s):
    """Per enemy type: the items (boots included) whose share against that type differs by at
    least MATCHUP_MIN_DIFF points from their share in the champion's other games."""
    all_items = s["items"] + s["boots"]
    cards = {}
    for tag, ctx in s["context_data"].items():
        games, other = ctx["g"], s["ig"] - ctx["g"]
        if games < MATCHUP_MIN_GAMES or other < MATCHUP_MIN_GAMES:
            continue
        rows = []
        for i, total in all_items.items():
            n, o = ctx["items"][i], total - ctx["items"][i]
            # Integer form of |n/games - o/other| * 100 >= MATCHUP_MIN_DIFF, exact at the edge.
            if abs(n * other - o * games) * 100 >= MATCHUP_MIN_DIFF * games * other:
                rows.append(
                    {
                        "id": i,
                        "games": n,
                        "share": round(n / games * 100, 1),
                        "other_games": o,
                        "other_share": round(o / other * 100, 1),
                    }
                )
        rows.sort(key=lambda r: (-abs(r["share"] - r["other_share"]), r["id"]))
        if rows:
            cards[tag] = {"games": games, "other_games": other, "items": rows}
    return cards


# --- 5. DB MAINTENANCE ---
def purge_old_patches(matches_col, target_patch):
    if target_patch:
        logger.info("--- DB MAINTENANCE ---")
        logger.info(f"Target Patch: {target_patch}.")

        try:
            delete_result = matches_col.delete_many(
                {"info.gameVersion": {"$not": {"$regex": f"^{target_patch}"}}}
            )

            if delete_result.deleted_count > 0:
                logger.info(f"Purged {delete_result.deleted_count} matches from older patches.")
            else:
                logger.info("Database is already clean (no old patches found).")
        except Exception as e:  # noqa: BLE001 - a failed purge must not stop the run
            logger.error(f"Cleanup Error: {e}")
    else:
        logger.warning("Skipping DB cleanup: Could not determine current patch from Riot API.")


# --- 6. CRAWLER ---
def crawl(watcher, riot_watcher, matches_col, target_patch, player_count):
    """Download new ranked matches of the top players into matches_col.

    Return (new matches saved, regions whose scan did not fail). RiotAuthError propagates.
    """
    new_count = 0
    regions_ok = 0

    for region_code, region_name in REGIONS:
        logger.info(f"=== Scan: {region_name} ({region_code}) ===")
        try:
            # Tiered fallback: Challenger → Grandmaster → Master → Database → Pro Players
            entries = []

            # Try Challenger
            challenger = smart_request(
                watcher.league.challenger_by_queue, region_code, "RANKED_SOLO_5x5"
            )
            if challenger and "entries" in challenger:
                entries = challenger["entries"]  # type: ignore
                logger.info(f"Found {len(entries)} Challenger players")

            # If not enough, try Grandmaster
            if len(entries) < player_count:
                logger.info("Not enough Challenger players, trying Grandmaster...")
                grandmaster = smart_request(
                    watcher.league.grandmaster_by_queue, region_code, "RANKED_SOLO_5x5"
                )
                if grandmaster and "entries" in grandmaster:
                    entries.extend(grandmaster["entries"])  # type: ignore
                    logger.info(f"Added {len(grandmaster['entries'])} Grandmaster players")  # type: ignore

            # If still not enough, try Master
            if len(entries) < player_count:
                logger.info("Not enough GM players, trying Master...")
                master = smart_request(
                    watcher.league.masters_by_queue, region_code, "RANKED_SOLO_5x5"
                )
                if master and "entries" in master:
                    entries.extend(master["entries"])  # type: ignore
                    logger.info(f"Added {len(master['entries'])} Master players")  # type: ignore

            # If STILL not enough (early season reset), use database + pro players
            if len(entries) < player_count:
                logger.info(
                    f"Only {len(entries)} ranked players found. "
                    "Using database + pro player fallback..."
                )

                # Strategy 1: Get PUUIDs from recent matches in database
                pipeline = [
                    {"$match": {"_region": region_code}},
                    {"$sort": {"info.gameCreation": -1}},
                    {"$limit": 500},
                    {"$unwind": "$info.participants"},
                    {"$group": {"_id": "$info.participants.puuid"}},
                    {"$limit": player_count},
                ]
                fallback_puuids = [doc["_id"] for doc in matches_col.aggregate(pipeline)]
                logger.info(f"Found {len(fallback_puuids)} players from database")

                # Strategy 2: If database is also empty, use hardcoded pro players
                if len(fallback_puuids) < player_count and region_code in FALLBACK_PRO_PLAYERS:
                    logger.info("Database empty. Looking up pro player PUUIDs from Riot API...")
                    pro_names = FALLBACK_PRO_PLAYERS[region_code][:player_count]

                    for riot_id in pro_names:
                        try:
                            if "#" not in riot_id:
                                continue
                            game_name, tag_line = riot_id.split("#", 1)

                            routing = "asia" if region_code == "kr" else "europe"
                            account = smart_request(
                                riot_watcher.account.by_riot_id, routing, game_name, tag_line
                            )
                            if account and "puuid" in account:  # type: ignore
                                fallback_puuids.append(account["puuid"])  # type: ignore
                                logger.info(f"  Found: {riot_id}")
                        except RiotAuthError:
                            raise
                        except Exception as e:  # noqa: BLE001 - skip this pro, try the next
                            logger.warning(f"  Could not find {riot_id}: {e}")
                            continue

                # Add fallback PUUIDs as entries
                entries.extend([{"puuid": puuid} for puuid in fallback_puuids[:player_count]])

            # Sort by LP and take top player_count
            entries = sorted(entries, key=lambda x: x.get("leaguePoints", 0), reverse=True)[
                :player_count
            ]

            logger.info(f"Tracking {len(entries)} players total")

            for i, entry in enumerate(entries):
                try:
                    if "puuid" in entry:
                        puuid = entry["puuid"]
                    elif "summonerId" in entry:
                        puuid = smart_request(
                            watcher.summoner.by_id, region_code, entry["summonerId"]
                        )["puuid"]
                    else:
                        continue

                    match_ids = smart_request(
                        watcher.match.matchlist_by_puuid,
                        region_code,
                        puuid,
                        count=MATCH_HISTORY_COUNT,
                    )
                    if not match_ids:
                        continue

                    existing_docs = matches_col.find(
                        {"metadata.matchId": {"$in": match_ids}},
                        {"metadata.matchId": 1},
                    )
                    existing_ids = {doc["metadata"]["matchId"] for doc in existing_docs}
                    new_matches = [mid for mid in match_ids if mid not in existing_ids]

                    if not new_matches:
                        logger.info(f"Player {i + 1}/{player_count}: Up to date.")
                        continue

                    for match_id in new_matches:
                        match_detail = smart_request(watcher.match.by_id, region_code, match_id)
                        if match_detail:
                            # The old script failed here with a NameError when the patch was
                            # unknown; kept as an explicit error so no behaviour changes (#2).
                            if target_patch is None:
                                raise RuntimeError("current patch unknown, not saving matches")

                            # Skip old-patch matches before inserting, to save write ops.
                            match_ver = get_short_version(
                                match_detail.get("info", {}).get("gameVersion")
                            )
                            if match_ver != target_patch:
                                continue  # Skip saving if it's an old patch match

                            # Store only essential fields to reduce storage usage
                            slimmed = slim_match(match_detail, region_code)

                            try:
                                matches_col.insert_one(slimmed)
                                new_count += 1
                                logger.info(f"  [+] Saved: {match_id}")
                            except DuplicateKeyError:
                                pass  # already saved, e.g. by another player's history
                            except Exception as e:  # noqa: BLE001 - skip this match, keep crawling
                                logger.error(f"  Could not save {match_id}: {e}")

                    logger.info(f"Player {i + 1}/{player_count} scanned.")
                except RiotAuthError:
                    raise
                except Exception as e:  # noqa: BLE001 - one bad player must not stop the crawl
                    logger.error(f"Player error: {e}")
            regions_ok += 1
        except RiotAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - one bad region must not stop the crawl
            logger.error(f"Region error: {e}")

    return new_count, regions_ok


def incomplete_matches(docs, target_patch):
    """(region, matchId) of stored current-patch matches slimmed before gameDuration was kept."""
    return [
        (d["_region"], d["metadata"]["matchId"])
        for d in docs
        if "gameDuration" not in d.get("info", {})
        and get_short_version(d.get("info", {}).get("gameVersion")) == target_patch
    ]


def refetch_incomplete(watcher, matches_col, target_patch):
    """Download incomplete stored matches again and replace them; return the number replaced.

    Each match is tried once; one that cannot be fetched stays as it is. RiotAuthError propagates.
    """
    docs = matches_col.find(
        {"info.gameDuration": {"$exists": False}},
        {"_region": 1, "metadata.matchId": 1, "info.gameVersion": 1},
    )
    replaced = 0
    for region_code, match_id in incomplete_matches(docs, target_patch):
        try:
            match_detail = smart_request(watcher.match.by_id, region_code, match_id)
            if not match_detail:
                continue
            matches_col.replace_one(
                {"metadata.matchId": match_id}, slim_match(match_detail, region_code)
            )
            replaced += 1
        except RiotAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - keep the old document, try the next match
            logger.error(f"  Could not download {match_id} again: {e}")
    logger.info(f"Downloaded {replaced} stored matches again with runes and game length.")
    return replaced


# --- 7. ANALYSIS (pure: stored matches in, data.json structure out) ---
def summarize_match(m, champ_id_to_name, tank_champs, damage_shares):
    """Reduce a stored match to what aggregate() needs; records champion names seen."""
    info = m.get("info", {})
    version = get_short_version(info.get("gameVersion", ""))
    bans = extract_bans(info)

    context_map = {
        100: analyze_enemy_comp(info, 100, tank_champs, damage_shares),
        200: analyze_enemy_comp(info, 200, tank_champs, damage_shares),
    }

    summary = {"patch": version, "bans": bans, "participants": []}

    for p in info.get("participants", []):
        champ_id_to_name[p["championId"]] = p["championName"]
        name = (
            f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}"
            if p.get("riotIdGameName")
            else (p.get("summonerName") or "Unknown")
        )
        items = [p.get(f"item{i}", 0) for i in range(6)]

        my_context = context_map.get(p["teamId"], [])

        summary["participants"].append(
            {
                "role": p["teamPosition"],
                "remake": bool(p.get("gameEndedInEarlySurrender")),
                "perks": p.get("perks"),
                "spells": (p.get("summoner1Id"), p.get("summoner2Id")),
                "name": name,
                "champ": p["championName"],
                "win": p["win"],
                "k": p["kills"],
                "d": p["deaths"],
                "a": p["assists"],
                "items": items,
                "context": my_context,
            }
        )
    return summary


def item_shares(counts, games):
    """The 8 items in most games: id, games and share (% of the champion's games)."""
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:8]
    return [{"id": i, "games": n, "share": round(n / games * 100, 1)} for i, n in ranked]


RUNE_PAGES = 3
SPELL_PAIRS = 2


def rune_stats(s):
    """The champion's most frequent rune pages and spell pairs, shares over games with runes."""
    rg = s["rg"]
    pages = sorted(s["pages"].items(), key=lambda x: (-x[1][0], x[0]))[:RUNE_PAGES]
    runes = [
        {
            "keystone": key[0],
            "primary_style": key[1],
            "secondary_style": key[2],
            "games": g,
            "share": round(g / rg * 100, 1),
            "win_rate": round(w / g * 100, 1),
        }
        for key, (g, w) in pages
    ]
    if runes:
        top = (runes[0]["keystone"], runes[0]["primary_style"], runes[0]["secondary_style"])
        (sel, n), *_ = sorted(s["selections"][top].items(), key=lambda x: (-x[1], x[0]))
        runes[0]["selection"] = {
            "primary": list(sel[:3]),
            "secondary": list(sel[3:5]),
            "shards": list(sel[5:]),
            "games": n,
        }
    spells = [
        {"ids": list(pair), "games": g, "share": round(g / rg * 100, 1)}
        for pair, g in sorted(s["spells"].items(), key=lambda x: (-x[1], x[0]))[:SPELL_PAIRS]
    ]
    return {"rune_games": rg, "runes": runes, "spells": spells}


def new_champ_stats():
    return {
        "g": 0,
        "w": 0,
        "k": 0,
        "d": 0,
        "a": 0,
        "b": 0,
        "ig": 0,  # games that are not remakes: the item denominator
        "items": Counter(),
        "boots": Counter(),
        "context_data": {},
        "rg": 0,  # games that are not remakes and have rune data
        "pages": {},  # (keystone, primary style, secondary style) -> [games, wins]
        "selections": {},  # page -> Counter of (3 primary, 2 secondary, 3 shards)
        "spells": Counter(),  # sorted spell pair -> games
    }


def aggregate(
    match_list,
    role_filter,
    region_code,
    champ_id_to_name,
    valid_items,
    player_performance,
    boots=frozenset(),
):
    """Stats of every champion picked or banned in one role, by pick rate; adds each player's
    games to player_performance."""
    stats = {}
    total_matches = len(match_list)
    if total_matches == 0:
        return []

    # 1. Loop through all matches to gather raw stats
    # Both teams' players in the role count, not only the blue-side one (#10).
    for p in (x for m in match_list for x in m["participants"] if x["role"] == role_filter):
        c = p["champ"]

        # --- LEADERBOARD LOGIC (keyed by role:champion) ---
        lb_key = f"{role_filter}:{c}"
        if lb_key not in player_performance:
            player_performance[lb_key] = {}
        if p["name"] not in player_performance[lb_key]:
            player_performance[lb_key][p["name"]] = {
                "g": 0,
                "w": 0,
                "k": 0,
                "d": 0,
                "a": 0,
                "r": region_code,
            }
        pp = player_performance[lb_key][p["name"]]
        pp["g"] += 1
        pp["k"] += p["k"]
        pp["d"] += p["d"]
        pp["a"] += p["a"]
        if p["win"]:
            pp["w"] += 1

        # --- CHAMPION STATS INITIALIZATION ---
        if c not in stats:
            stats[c] = new_champ_stats()

        s = stats[c]
        s["g"] += 1
        s["k"] += p["k"]
        s["d"] += p["d"]
        s["a"] += p["a"]
        if p["win"]:
            s["w"] += 1

        # Remakes count for pick and win rate, but say nothing about builds, runes or spells.
        if p["remake"]:
            continue
        s["ig"] += 1

        # --- ITEM BUILD LOGIC ---
        # 1. Filter: Keep only 'Valid' items (Legendaries)
        valid_player_items = []
        for i in p["items"]:
            if i != 0 and (valid_items is None or i in valid_items):
                valid_player_items.append(i)

        # 2. Count each item once per game; a game with few or no items still counts in "g".
        for i in set(valid_player_items):
            s["boots" if i in boots else "items"][i] += 1

        # --- ENEMY TYPE: games and item counts against each type ---
        for tag in p["context"]:
            ctx = s["context_data"].setdefault(tag, {"g": 0, "items": Counter()})
            ctx["g"] += 1
            ctx["items"].update(set(valid_player_items))

        # --- RUNES AND SPELLS: only games stored with rune data ---
        perks = p["perks"]
        if perks and perks["primary"]:
            s["rg"] += 1
            page = (perks["primary"][0], perks["primaryStyle"], perks["subStyle"])
            pg = s["pages"].setdefault(page, [0, 0])
            pg[0] += 1
            pg[1] += bool(p["win"])
            sel = tuple(perks["primary"][1:] + perks["secondary"] + perks["shards"])
            s["selections"].setdefault(page, Counter())[sel] += 1
            if None not in p["spells"]:
                s["spells"][tuple(sorted(p["spells"]))] += 1

    # 2. Count Bans (Global look at the match set)
    for m in match_list:
        for bid in m["bans"]:
            if bid in champ_id_to_name:
                bn = champ_id_to_name[bid]
                # Init if banned but not played
                if bn not in stats:
                    stats[bn] = new_champ_stats()
                stats[bn]["b"] += 1

    # 3. Finalize and Format Results
    results = []
    for name, s in stats.items():
        if s["g"] == 0 and s["b"] == 0:
            continue

        # Basic Math
        # Two role slots per match, so a role's pick rates still sum to about 100 (#10).
        pick_rate = round((s["g"] / (2 * total_matches)) * 100, 1)
        ban_rate = round((s["b"] / total_matches) * 100, 1)
        win_rate = round((s["w"] / s["g"]) * 100, 1) if s["g"] > 0 else 0
        kda = round((s["k"] + s["a"]) / (s["d"] if s["d"] > 0 else 1), 2)

        # Only include relevant champions
        if s["g"] > 0 or ban_rate > 1.0:
            results.append(
                {
                    "name": name,
                    "count": s["g"],
                    "pick_rate": pick_rate,
                    "win_rate": win_rate,
                    "ban_rate": ban_rate,
                    "kda": kda,
                    "item_games": s["ig"],
                    "items": item_shares(s["items"], s["ig"]),
                    "boots": item_shares(s["boots"], s["ig"]),
                    "matchups": matchup_cards(s),
                    **rune_stats(s),
                }
            )

    return sorted(results, key=lambda x: x["pick_rate"], reverse=True)


def build_leaderboards(player_performance):
    # Leaderboards are keyed by "ROLE:ChampionName"
    leaderboards = {}
    for lb_key, players in player_performance.items():
        lb = []
        for pname, s in players.items():
            kda = round((s["k"] + s["a"]) / (s["d"] if s["d"] > 0 else 1), 2)
            wr = round((s["w"] / s["g"]) * 100, 1)
            lb.append(
                {
                    "player": pname,
                    "region": s["r"],
                    "games": s["g"],
                    "win_rate": wr,
                    "kda": kda,
                }
            )
        leaderboards[lb_key] = sorted(lb, key=lambda x: (x["games"], x["win_rate"]), reverse=True)
    return leaderboards


def build_frontend_data(
    matches,
    ddragon_version,
    player_count,
    tank_champs,
    valid_items,
    last_updated,
    boots=frozenset(),
):
    """Turn all stored matches (any region) into the data.json structure."""
    # Use the patch version we got from Data Dragon API, not from database
    current_patch = get_short_version(ddragon_version) if ddragon_version else "14.1"

    frontend_data = {
        "meta": {
            "total_games": len(matches),
            "last_updated": last_updated,
            "current_patch": current_patch,
            "ddragon_version": ddragon_version or "14.23.1",
            "player_count": player_count,
        },
        "regions": {},
        "leaderboards": {},
    }

    # Both are shared across regions on purpose: a region's bans resolve against every champion
    # seen so far, and a player's leaderboard region is the first one they were seen in.
    champ_id_to_name = {}
    player_performance = {}
    damage_shares = champion_damage_shares(matches)

    for region_code, _ in REGIONS:
        region_matches = [
            summarize_match(m, champ_id_to_name, tank_champs, damage_shares)
            for m in matches
            if m.get("_region") == region_code
        ]
        frontend_data["regions"][region_code] = {
            r: aggregate(
                region_matches,
                r,
                region_code,
                champ_id_to_name,
                valid_items,
                player_performance,
                boots,
            )[:15]
            for r in VALID_ROLES
        }

    frontend_data["leaderboards"] = build_leaderboards(player_performance)
    return frontend_data


# --- 8. PER-PATCH ARCHIVE (history.json) ---
def patch_regions(matches, patch):
    """Per region: exact match count on patch and every champion picked per role."""
    # Mirrors build_frontend_data, so ban rates resolve the same way as in data.json.
    champ_id_to_name = {}
    regions = {}
    for region_code, _ in REGIONS:
        region_matches = [
            s
            for s in (
                summarize_match(m, champ_id_to_name, set(), {})
                for m in matches
                if m.get("_region") == region_code
            )
            if s["patch"] == patch
        ]
        regions[region_code] = {
            "games": len(region_matches),
            "games_estimated": False,
            "roles": {
                r: [
                    {
                        "name": c["name"],
                        "games": c["count"],
                        "pick_rate": c["pick_rate"],
                        "win_rate": c["win_rate"],
                        "ban_rate": c["ban_rate"],
                    }
                    for c in aggregate(region_matches, r, region_code, champ_id_to_name, None, {})
                    if c["count"] > 0
                ]
                for r in VALID_ROLES
            },
        }
    return regions


def update_history(history, patch, regions, snapshot_date):
    """Return history with the entry of patch replaced or added; other entries untouched."""
    if sum(r["games"] for r in regions.values()) == 0:
        return history
    entry = {
        "patch": patch,
        "snapshot_date": snapshot_date,
        "source": "generator",
        "complete": True,
        "regions": regions,
    }
    patches = [p for p in history["patches"] if p["patch"] != patch] + [entry]
    patches.sort(key=lambda p: tuple(int(x) for x in p["patch"].split(".")))
    return {**history, "patches": patches}


def load_history(path):
    """Read history.json; raise ValueError if it is not a schema 1 archive."""
    if not os.path.exists(path):
        return {"schema": 1, "patches": []}
    with open(path) as f:
        history = json.load(f)
    if (
        not isinstance(history, dict)
        or history.get("schema") != 1
        or not isinstance(history.get("patches"), list)
    ):
        raise ValueError(f"{path} is not a schema 1 history file")
    return history


def write_history(path, history):
    # Write then rename: history.json is the only copy of past patches.
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(history, f, separators=(",", ":"))
    os.replace(tmp, path)


# --- 9. ENTRY POINT ---
def main():
    load_dotenv()
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level_str, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    api_key = os.getenv("RIOT_API_KEY")
    mongo_uri = os.getenv("MONGODB_URI")
    player_count = int(os.getenv("PLAYER_COUNT", "10"))

    os.makedirs(DATA_FOLDER, exist_ok=True)

    if not api_key:
        raise ValueError("Missing RIOT_API_KEY")
    if not mongo_uri:
        raise ValueError("Missing MONGODB_URI")

    watcher = LolWatcher(api_key)
    riot_watcher = RiotWatcher(api_key)
    matches_col = connect_db(mongo_uri)
    latest_ver, tank_champs, valid_items, boots = load_static_data()

    target_patch = get_short_version(latest_ver) if latest_ver else None

    # Read before anything is written: an unreadable archive must stop the run, not be replaced.
    try:
        history = load_history(HISTORY_FILE)
    except (OSError, ValueError) as e:
        logger.error(f"Cannot read history.json: {e}. Nothing written.")
        return 1

    # On failure return before the purge and before writing data.json, so the workflow stops
    # and the published site keeps its last good data.
    try:
        new_count, regions_ok = crawl(
            watcher, riot_watcher, matches_col, target_patch, player_count
        )
        if target_patch and regions_ok:
            refetch_incomplete(watcher, matches_col, target_patch)
    except RiotAuthError as e:
        logger.error(f"{e}. Check the RIOT_API_KEY secret; data.json not written.")
        return 1
    if regions_ok == 0:
        logger.error("Could not read the ladder for any region; data.json not written.")
        return 1

    purge_old_patches(matches_col, target_patch)

    logger.info("Generating Frontend JSON...")
    matches = list(matches_col.find({}))
    now = datetime.now(timezone.utc)
    frontend_data = build_frontend_data(
        matches,
        latest_ver,
        player_count,
        tank_champs,
        valid_items,
        now.strftime("%Y-%m-%d %H:%M UTC"),
        boots,
    )

    with open(OUTPUT_FILE, "w") as f:
        json.dump(frontend_data, f)
    if target_patch:
        write_history(
            HISTORY_FILE,
            update_history(
                history,
                target_patch,
                patch_regions(matches, target_patch),
                now.strftime("%Y-%m-%d"),
            ),
        )
    logger.info(f"Complete. New Matches: {new_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
