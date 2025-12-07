import json
import time
import os
import logging
from riotwatcher import LolWatcher, ApiError
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

# Path Setup
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

# --- 3. DATABASE CONNECTION ---
try:
    client = MongoClient(MONGO_URI)
    db = client["league_tracker"]
    matches_col = db["matches"]
    # Ensure unique matches to prevent duplicates
    matches_col.create_index("metadata.matchId", unique=True)
    logger.info("Connected to MongoDB Atlas")
except Exception as e:
    logger.critical(f"Failed to connect to DB: {e}")
    raise e


# --- 4. HELPER FUNCTIONS ---
def smart_request(func, *args, **kwargs):
    func_name = func.__name__ if hasattr(func, "__name__") else "API Call"
    while True:
        try:
            logger.debug(f"Requesting: {func_name}")
            return func(*args, **kwargs)
        except ApiError as e:
            if e.response.status_code == 429:
                retry = int(e.response.headers.get("Retry-After", 10))
                logger.warning(f"⚠️ Rate Limit. Sleeping {retry}s...")
                time.sleep(retry + 1)
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


# --- 5. MAIN LOGIC ---
def fetch_data():
    stats = {"new": 0, "skipped": 0}

    # --- PHASE 1: CRAWLER (Riot API -> MongoDB) ---
    for region_code, region_name in REGIONS:
        logger.info(f"=== Scan: {region_name} ({region_code}) ===")

        try:
            challenger = smart_request(
                watcher.league.challenger_by_queue, region_code, "RANKED_SOLO_5x5"
            )
            entries = sorted(
                challenger["entries"], key=lambda x: x["leaguePoints"], reverse=True
            )[:PLAYER_COUNT]

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

                    # 1. Get List of Match IDs
                    match_ids = smart_request(
                        watcher.match.matchlist_by_puuid,
                        region_code,
                        puuid,
                        count=MATCH_HISTORY_COUNT,
                    )
                    if not match_ids:
                        continue

                    # 2. Filter out matches already in MongoDB
                    existing_docs = matches_col.find(
                        {"metadata.matchId": {"$in": match_ids}},
                        {"metadata.matchId": 1},
                    )
                    existing_ids = {doc["metadata"]["matchId"] for doc in existing_docs}
                    new_matches = [mid for mid in match_ids if mid not in existing_ids]

                    if not new_matches:
                        logger.info(
                            f"Player {i + 1}/{PLAYER_COUNT}: All matches up to date."
                        )
                        continue

                    # 3. Fetch & Save New Matches
                    for match_id in new_matches:
                        match_detail = smart_request(
                            watcher.match.by_id, region_code, match_id
                        )

                        if match_detail:
                            # Add region tag for easier querying later
                            match_detail["_region"] = region_code
                            try:
                                matches_col.insert_one(match_detail)
                                stats["new"] += 1
                                logger.info(f"   [+] Saved: {match_id}")
                            except:
                                # Ignore duplicate key errors (race conditions)
                                pass

                    logger.info(
                        f"Player {i + 1}/{PLAYER_COUNT} scanned. +{len(new_matches)} new."
                    )

                except Exception as e:
                    logger.error(f"Player error: {e}")
        except Exception as e:
            logger.error(f"Region error: {e}")

    # --- PHASE 2: ANALYST (MongoDB -> data.json) ---
    logger.info("Generating Frontend JSON from MongoDB...")

    # 1. Determine Current Patch
    all_versions = matches_col.distinct("info.gameVersion")
    valid_versions = [v for v in all_versions if v and v[0].isdigit()]

    def version_key(v):
        try:
            return tuple(map(int, v.split(".")[:2]))
        except:
            return (0, 0)

    if not valid_versions:
        current_patch = "14.1"
    else:
        current_patch = get_short_version(max(valid_versions, key=version_key))

    logger.info(f"Current Patch: {current_patch}")

    # 2. Build Frontend Structure
    total_games_db = matches_col.count_documents({})
    total_patch_games = matches_col.count_documents(
        {"info.gameVersion": {"$regex": f"^{current_patch}"}}
    )

    frontend_data = {
        "meta": {
            "total_games": total_games_db,
            "patch_games": total_patch_games,
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "current_patch": current_patch,
        },
        "regions": {},
        "leaderboards": {},
    }

    # 3. Aggregation Loop
    champ_id_to_name = {}
    player_performance = {}

    for region_code, _ in REGIONS:
        frontend_data["regions"][region_code] = {"season": {}, "patch": {}}
        for r in VALID_ROLES:
            frontend_data["regions"][region_code]["season"][r] = []
            frontend_data["regions"][region_code]["patch"][r] = []

        # Fetch all matches for region
        cursor = matches_col.find({"_region": region_code})

        region_matches = []

        # Pre-process into lightweight objects
        for m in cursor:
            info = m.get("info", {})
            version = get_short_version(info.get("gameVersion", ""))
            bans = extract_bans(info)

            summary = {"patch": version, "bans": bans, "participants": []}

            for p in info.get("participants", []):
                champ_id_to_name[p["championId"]] = p["championName"]

                # Name Resolution
                name = (
                    f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}"
                    if p.get("riotIdGameName")
                    else (p.get("summonerName") or "Unknown")
                )

                # Extract Items (New Feature Prep)
                items = [p.get(f"item{i}", 0) for i in range(6)]

                summary["participants"].append(
                    {
                        "role": p["teamPosition"],
                        "name": name,
                        "champ": p["championName"],
                        "win": p["win"],
                        "k": p["kills"],
                        "d": p["deaths"],
                        "a": p["assists"],
                        "items": items,  # Save items for aggregation
                    }
                )
            region_matches.append(summary)

        # Aggregation Helper
        def aggregate(match_list, role_filter):
            stats = {}  # {ChampName: {g, w, k, d, a, b, items:[]}}
            total = len(match_list)
            if total == 0:
                return []

            # 1. Performance Stats
            for m in match_list:
                p = next(
                    (x for x in m["participants"] if x["role"] == role_filter), None
                )
                if not p:
                    continue

                c = p["champ"]

                # Leaderboard Data
                if c not in player_performance:
                    player_performance[c] = {}
                if p["name"] not in player_performance[c]:
                    player_performance[c][p["name"]] = {
                        "g": 0,
                        "w": 0,
                        "k": 0,
                        "d": 0,
                        "a": 0,
                        "r": region_code,
                    }

                pp = player_performance[c][p["name"]]
                pp["g"] += 1
                pp["k"] += p["k"]
                pp["d"] += p["d"]
                pp["a"] += p["a"]
                if p["win"]:
                    pp["w"] += 1

                # Role Stats
                if c not in stats:
                    stats[c] = {
                        "g": 0,
                        "w": 0,
                        "k": 0,
                        "d": 0,
                        "a": 0,
                        "b": 0,
                        "items": [],
                    }
                s = stats[c]
                s["g"] += 1
                s["k"] += p["k"]
                s["d"] += p["d"]
                s["a"] += p["a"]
                if p["win"]:
                    s["w"] += 1
                s["items"].extend(
                    [i for i in p["items"] if i != 0]
                )  # Add non-empty items

            # 2. Ban Stats
            for m in match_list:
                for bid in m["bans"]:
                    if bid in champ_id_to_name:
                        bn = champ_id_to_name[bid]
                        if bn not in stats:
                            stats[bn] = {
                                "g": 0,
                                "w": 0,
                                "k": 0,
                                "d": 0,
                                "a": 0,
                                "b": 0,
                                "items": [],
                            }
                        stats[bn]["b"] += 1

            # 3. Finalize
            results = []
            for name, s in stats.items():
                if s["g"] == 0 and s["b"] == 0:
                    continue

                pick_rate = round((s["g"] / total) * 100, 1)
                ban_rate = round((s["b"] / total) * 100, 1)
                win_rate = round((s["w"] / s["g"]) * 100, 1) if s["g"] > 0 else 0
                kda = round((s["k"] + s["a"]) / (s["d"] if s["d"] > 0 else 1), 2)

                # Calculate Top 3 Items
                top_items = [i[0] for i in Counter(s["items"]).most_common(3)]

                if s["g"] > 0 or ban_rate > 1.0:
                    results.append(
                        {
                            "name": name,
                            "count": s["g"],
                            "pick_rate": pick_rate,
                            "win_rate": win_rate,
                            "ban_rate": ban_rate,
                            "kda": kda,
                            "top_items": top_items,  # New Field
                        }
                    )

            return sorted(results, key=lambda x: x["pick_rate"], reverse=True)[:15]

        patch_subset = [m for m in region_matches if m["patch"] == current_patch]

        for r in VALID_ROLES:
            frontend_data["regions"][region_code]["season"][r] = aggregate(
                region_matches, r
            )
            frontend_data["regions"][region_code]["patch"][r] = aggregate(
                patch_subset, r
            )

    # 4. Finalize Leaderboards
    for champ, players in player_performance.items():
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
        frontend_data["leaderboards"][champ] = sorted(
            lb, key=lambda x: (x["games"], x["win_rate"]), reverse=True
        )

    with open(OUTPUT_FILE, "w") as f:
        json.dump(frontend_data, f)

    logger.info(f"Complete. New Matches Saved: {stats['new']}")


if __name__ == "__main__":
    fetch_data()
