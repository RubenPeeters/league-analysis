import json
import time
import os
import logging
import requests
from riotwatcher import LolWatcher, RiotWatcher, ApiError
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from collections import Counter

# --- 1. SETUP ---
load_dotenv()
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level_str, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- 2. CONFIGURATION ---
API_KEY = os.getenv("RIOT_API_KEY")
MONGO_URI = os.getenv("MONGODB_URI")
REGIONS = [("kr", "Korea"), ("euw1", "Europe West")]
VALID_ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
PLAYER_COUNT = int(os.getenv("PLAYER_COUNT", 10))
MATCH_HISTORY_COUNT = 100

# Fallback pro players for season reset (when ladder is empty)
FALLBACK_PRO_PLAYERS = {
    "kr": [
        # Top Korean Pro Players (Riot ID format: GameName#TAG)
        "Faker#KR1", "Zeus#0000", "Oner#KR1", "Gumayusi#KR1", "Keria#KR1",
        "Chovy#KR1", "Doran#KR1", "Peanut#KR1", "Viper#KR1", "Delight#KR1",
        "ShowMaker#KR1", "Canyon#KR1", "Aiming#KR1", "Kellin#KR1",
        "Kiin#KR1", "Peyz#KR1", "Zeka#KR1", "Ruler#KR1", "Deft#KR1",
        "BeryL#KR1", "Canna#KR1", "Teddy#KR1", "Effort#KR1", "Life#KR1"
    ],
    "euw1": [
        # Top European Pro Players
        "Caps#EUW", "Upset#EUW", "Mikyx#EUW", "Jankos#EUW", "Rekkles#3737",
        "Inspired#EUW", "Hans sama#EUW", "Elyoya#EUW", "Humanoid#EUW",
        "Razork#EUW", "Comp#EUW", "Targamas#EUW", "Odoamne#EUW",
        "Larssen#EUW", "Irrelevant#EUW", "Labrov#EUW", "Alphari#EUW",
        "Jun#EUW", "Kaiser#EUW", "Crownie#EUW", "Nuclearint#EUW",
        "Bwipo#EUW", "Nisqy#EUW", "Hylissang#EUW"
    ]
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FOLDER = os.path.join(SCRIPT_DIR, "..", "data")
if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)
OUTPUT_FILE = os.path.join(DATA_FOLDER, "data.json")

if not API_KEY:
    raise ValueError("Missing RIOT_API_KEY")
if not MONGO_URI:
    raise ValueError("Missing MONGODB_URI")

watcher = LolWatcher(API_KEY)
riot_watcher = RiotWatcher(API_KEY)

# --- 3. DATABASE ---
try:
    import certifi

    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client["league_tracker"]
    matches_col = db["matches"]
    matches_col.create_index("metadata.matchId", unique=True)
    logger.info("Connected to MongoDB Atlas")
except Exception as e:
    logger.critical(f"DB Error: {e}")
    raise e

# --- 4. STATIC DATA (Context & Item Filtering) ---
TANK_CHAMPS = set()
VALID_ITEMS = None  # If None, we won't filter (fallback)

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
            TANK_CHAMPS = {name for name, d in c_data.items() if "Tank" in d["tags"]}
            logger.info(f"Loaded {len(TANK_CHAMPS)} Tank definitions.")

        # 3. Get Items (For Filtering Components)
        item_res = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{latest_ver}/data/en_US/item.json"
        )
        if item_res.ok:
            i_data = item_res.json()["data"]
            VALID_ITEMS = set()
            for i_id, d in i_data.items():
                depth = d.get("depth", 1)
                is_boot = "Boots" in d.get("tags", [])

                # RULE: Keep item if Depth >= 3 (Legendary) OR Depth 2 Boots (Plated Steelcaps etc)
                if depth >= 3 or (depth == 2 and is_boot):
                    VALID_ITEMS.add(int(i_id))

            logger.info(
                f"Loaded {len(VALID_ITEMS)} Valid Items (Filtered out components)."
            )

except Exception as e:
    logger.error(f"Static Data Error: {e}")


# --- 5. HELPER FUNCTIONS ---
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
            else:
                raise e
        except Exception as e:
            logger.error(f"Error: {e}")
            raise e


def get_short_version(game_version):
    if not game_version:
        return "0.0"
    parts = game_version.split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else parts[0]


def extract_bans(match_info):
    return [
        b["championId"]
        for t in match_info["teams"]
        for b in t["bans"]
        if b["championId"] != -1
    ]


def slim_match(match_detail, region_code):
    """Extract only the fields needed for analysis to reduce storage."""
    info = match_detail.get("info", {})

    slim_participants = []
    for p in info.get("participants", []):
        slim_participants.append({
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
        })

    slim_teams = []
    for team in info.get("teams", []):
        slim_teams.append({
            "teamId": team.get("teamId"),
            "bans": team.get("bans", []),
        })

    return {
        "_region": region_code,
        "metadata": {
            "matchId": match_detail.get("metadata", {}).get("matchId"),
        },
        "info": {
            "gameVersion": info.get("gameVersion"),
            "gameCreation": info.get("gameCreation"),
            "teams": slim_teams,
            "participants": slim_participants,
        },
    }


def analyze_enemy_comp(match_info, my_team_id):
    enemies = [p for p in match_info["participants"] if p["teamId"] != my_team_id]
    total_phys = sum(p.get("physicalDamageDealtToChampions", 0) for p in enemies)
    total_magic = sum(p.get("magicDamageDealtToChampions", 0) for p in enemies)
    total_dmg = (
        total_phys
        + total_magic
        + sum(p.get("trueDamageDealtToChampions", 0) for p in enemies)
    )

    if total_dmg == 0:
        return []

    tags = []
    if (total_phys / total_dmg) > 0.65:
        tags.append("Heavy AD")
    elif (total_magic / total_dmg) > 0.60:
        tags.append("Heavy AP")

    tank_count = sum(1 for p in enemies if p["championName"] in TANK_CHAMPS)
    if tank_count >= 2:
        tags.append("Tank Heavy")

    return tags


# --- 6. MAIN LOGIC ---
def fetch_data():
    # === NEW: DB CLEANUP PHASE ===
    # We use 'globals().get' to safely access 'latest_ver' from Section 4
    # without crashing if the static data request failed previously.
    current_full_ver = globals().get("latest_ver")

    if current_full_ver:
        # 1. Calculate the short patch (e.g. "14.23")
        target_patch = get_short_version(current_full_ver)

        logger.info(f"--- DB MAINTENANCE ---")
        logger.info(f"Target Patch: {target_patch}.")

        try:
            delete_result = matches_col.delete_many(
                {"info.gameVersion": {"$not": {"$regex": f"^{target_patch}"}}}
            )

            if delete_result.deleted_count > 0:
                logger.info(
                    f"Purged {delete_result.deleted_count} matches from older patches."
                )
            else:
                logger.info("Database is already clean (no old patches found).")
        except Exception as e:
            logger.error(f"Cleanup Error: {e}")
    else:
        logger.warning(
            "Skipping DB cleanup: Could not determine current patch from Riot API."
        )

    # === EXISTING LOGIC STARTS HERE ===
    stats = {"new": 0}

    # PHASE 1: CRAWLER
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
            if len(entries) < PLAYER_COUNT:
                logger.info(f"Not enough Challenger players, trying Grandmaster...")
                grandmaster = smart_request(
                    watcher.league.grandmaster_by_queue, region_code, "RANKED_SOLO_5x5"
                )
                if grandmaster and "entries" in grandmaster:
                    entries.extend(grandmaster["entries"])  # type: ignore
                    logger.info(f"Added {len(grandmaster['entries'])} Grandmaster players")  # type: ignore

            # If still not enough, try Master
            if len(entries) < PLAYER_COUNT:
                logger.info(f"Not enough GM players, trying Master...")
                master = smart_request(
                    watcher.league.masters_by_queue, region_code, "RANKED_SOLO_5x5"
                )
                if master and "entries" in master:
                    entries.extend(master["entries"])  # type: ignore
                    logger.info(f"Added {len(master['entries'])} Master players")  # type: ignore

            # If STILL not enough (early season reset), use database + pro players
            if len(entries) < PLAYER_COUNT:
                logger.info(f"Only {len(entries)} ranked players found. Using database + pro player fallback...")

                # Strategy 1: Get PUUIDs from recent matches in database
                pipeline = [
                    {"$match": {"_region": region_code}},
                    {"$sort": {"info.gameCreation": -1}},
                    {"$limit": 500},
                    {"$unwind": "$info.participants"},
                    {"$group": {"_id": "$info.participants.puuid"}},
                    {"$limit": PLAYER_COUNT}
                ]
                fallback_puuids = [doc["_id"] for doc in matches_col.aggregate(pipeline)]
                logger.info(f"Found {len(fallback_puuids)} players from database")

                # Strategy 2: If database is also empty, use hardcoded pro players
                if len(fallback_puuids) < PLAYER_COUNT and region_code in FALLBACK_PRO_PLAYERS:
                    logger.info("Database empty. Looking up pro player PUUIDs from Riot API...")
                    pro_names = FALLBACK_PRO_PLAYERS[region_code][:PLAYER_COUNT]

                    for riot_id in pro_names:
                        try:
                            if "#" not in riot_id:
                                continue
                            game_name, tag_line = riot_id.split("#", 1)

                            routing = "asia" if region_code == "kr" else "europe"
                            account = smart_request(
                                riot_watcher.account.by_riot_id, routing,
                                game_name, tag_line
                            )
                            if account and "puuid" in account:  # type: ignore
                                fallback_puuids.append(account["puuid"])  # type: ignore
                                logger.info(f"  Found: {riot_id}")
                        except Exception as e:
                            logger.warning(f"  Could not find {riot_id}: {e}")
                            continue

                # Add fallback PUUIDs as entries
                entries.extend([{"puuid": puuid} for puuid in fallback_puuids[:PLAYER_COUNT]])

            # Sort by LP and take top PLAYER_COUNT
            entries = sorted(
                entries, key=lambda x: x.get("leaguePoints", 0), reverse=True
            )[:PLAYER_COUNT]

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
                        logger.info(f"Player {i + 1}/{PLAYER_COUNT}: Up to date.")
                        continue

                    for match_id in new_matches:
                        match_detail = smart_request(
                            watcher.match.by_id, region_code, match_id
                        )
                        if match_detail:
                            # === OPTIONAL: IMMEDIATE FILTER ===
                            # If you want to avoid saving old matches entirely (saving write Ops),
                            # check the version before inserting.
                            match_ver = get_short_version(
                                match_detail.get("info", {}).get("gameVersion")
                            )
                            if target_patch and match_ver != target_patch:
                                continue  # Skip saving if it's an old patch match

                            # Store only essential fields to reduce storage usage
                            slimmed = slim_match(match_detail, region_code)

                            try:
                                matches_col.insert_one(slimmed)
                                stats["new"] += 1
                                logger.info(f"  [+] Saved: {match_id}")
                            except:
                                pass

                    logger.info(f"Player {i + 1}/{PLAYER_COUNT} scanned.")
                except Exception as e:
                    logger.error(f"Player error: {e}")
        except Exception as e:
            logger.error(f"Region error: {e}")

    # PHASE 2: ANALYST
    # (Rest of your original code remains exactly the same from here down)
    logger.info("Generating Frontend JSON...")

    # Use the same patch version we got from Data Dragon API, not from database
    current_patch = target_patch if current_full_ver else "14.1"

    total_games_db = matches_col.count_documents({})

    frontend_data = {
        "meta": {
            "total_games": total_games_db,
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "current_patch": current_patch,
            "ddragon_version": current_full_ver or "14.23.1",
            "player_count": PLAYER_COUNT,
        },
        "regions": {},
        "leaderboards": {},
    }

    champ_id_to_name = {}
    player_performance = {}

    for region_code, _ in REGIONS:
        frontend_data["regions"][region_code] = {}
        for r in VALID_ROLES:
            frontend_data["regions"][region_code][r] = []

        cursor = matches_col.find({"_region": region_code})
        region_matches = []

        for m in cursor:
            info = m.get("info", {})
            version = get_short_version(info.get("gameVersion", ""))
            bans = extract_bans(info)

            context_map = {
                100: analyze_enemy_comp(info, 100),
                200: analyze_enemy_comp(info, 200),
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
            region_matches.append(summary)

        def aggregate(match_list, role_filter):
            stats = {}
            total_matches = len(match_list)
            if total_matches == 0:
                return []

            # 1. Loop through all matches to gather raw stats
            for m in match_list:
                # Find the player in the specific role (e.g. JUNGLE)
                p = next(
                    (x for x in m["participants"] if x["role"] == role_filter), None
                )
                if not p:
                    continue

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
                    # Note: 'builds' is a list that will store every valid item set found
                    stats[c] = {
                        "g": 0,
                        "w": 0,
                        "k": 0,
                        "d": 0,
                        "a": 0,
                        "b": 0,
                        "builds": [],
                        "context_data": {},
                    }

                s = stats[c]
                s["g"] += 1
                s["k"] += p["k"]
                s["d"] += p["d"]
                s["a"] += p["a"]
                if p["win"]:
                    s["w"] += 1

                # --- ITEM BUILD LOGIC ---
                # 1. Filter: Keep only 'Valid' items (Legendaries)
                valid_player_items = []
                for i in p["items"]:
                    if i != 0 and (VALID_ITEMS is None or i in VALID_ITEMS):
                        valid_player_items.append(i)

                # 2. Signature: Take top 3, SORT them, and tuple them
                # Sorting ensures [Item A, Item B] counts the same as [Item B, Item A]
                if (
                    len(valid_player_items) >= 2
                ):  # Only count if they have at least 2 core items
                    build_signature = tuple(sorted(valid_player_items[:3]))
                    s["builds"].append(build_signature)

                # --- CONTEXT LOGIC (Optional: Gather context stats) ---
                for tag in p["context"]:
                    if tag not in s["context_data"]:
                        s["context_data"][tag] = {"g": 0, "builds": []}

                    ctx = s["context_data"][tag]
                    ctx["g"] += 1
                    if len(valid_player_items) >= 2:
                        ctx["builds"].append(tuple(sorted(valid_player_items[:3])))

            # 2. Count Bans (Global look at the match set)
            for m in match_list:
                for bid in m["bans"]:
                    if bid in champ_id_to_name:
                        bn = champ_id_to_name[bid]
                        # Init if banned but not played
                        if bn not in stats:
                            stats[bn] = {
                                "g": 0,
                                "w": 0,
                                "k": 0,
                                "d": 0,
                                "a": 0,
                                "b": 0,
                                "builds": [],
                                "context_data": {},
                            }
                        stats[bn]["b"] += 1

            # 3. Finalize and Format Results
            results = []
            for name, s in stats.items():
                if s["g"] == 0 and s["b"] == 0:
                    continue

                # Basic Math
                pick_rate = round((s["g"] / total_matches) * 100, 1)
                ban_rate = round((s["b"] / total_matches) * 100, 1)
                win_rate = round((s["w"] / s["g"]) * 100, 1) if s["g"] > 0 else 0
                kda = round((s["k"] + s["a"]) / (s["d"] if s["d"] > 0 else 1), 2)

                # --- CALCULATE TOP BUILD ---
                top_items = []
                if s["builds"]:
                    # Counter finds the most common tuple signature
                    # most_common(1) returns [( (ItemA, ItemB, ItemC), Count )]
                    most_common = Counter(s["builds"]).most_common(1)
                    if most_common:
                        # Convert tuple back to list for JSON
                        top_items = list(most_common[0][0])

                # --- CALCULATE CONTEXT BUILDS ---
                context_builds = {}
                for tag, ctx_data in s.get("context_data", {}).items():
                    if ctx_data["g"] >= 3:  # Minimum sample size filter
                        ctx_top_items = []
                        if ctx_data["builds"]:
                            ctx_common = Counter(ctx_data["builds"]).most_common(1)
                            if ctx_common:
                                ctx_top_items = list(ctx_common[0][0])

                        if ctx_top_items:
                            context_builds[tag] = {
                                "games": ctx_data["g"],
                                "items": ctx_top_items,
                            }

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
                            "top_items": top_items,  # Now a sorted SET of items
                            "context_builds": context_builds,
                        }
                    )

            return sorted(results, key=lambda x: x["pick_rate"], reverse=True)[:15]

        for r in VALID_ROLES:
            frontend_data["regions"][region_code][r] = aggregate(region_matches, r)

    # Leaderboards are keyed by "ROLE:ChampionName"
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
        frontend_data["leaderboards"][lb_key] = sorted(
            lb, key=lambda x: (x["games"], x["win_rate"]), reverse=True
        )

    with open(OUTPUT_FILE, "w") as f:
        json.dump(frontend_data, f)
    logger.info(f"Complete. New Matches: {stats['new']}")


if __name__ == "__main__":
    fetch_data()
