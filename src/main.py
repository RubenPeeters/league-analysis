import json
import time
import os
import logging
from riotwatcher import LolWatcher, ApiError
from collections import Counter
from dotenv import load_dotenv

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
REGIONS = [("kr", "Korea"), ("euw1", "Europe West")]
# Valid Riot API Roles
VALID_ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
PLAYER_COUNT = int(os.getenv("PLAYER_COUNT", 10))
MATCH_HISTORY_COUNT = 100

# Path Setup
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FOLDER = os.path.join(SCRIPT_DIR, "..", "data")
if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)
DB_FILE = os.path.join(DATA_FOLDER, "match_database.json")
OUTPUT_FILE = os.path.join(DATA_FOLDER, "data.json")

if not API_KEY:
    raise ValueError("Missing RIOT_API_KEY")
watcher = LolWatcher(API_KEY)


# --- 3. HELPER FUNCTIONS ---
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


def load_database():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                content = f.read()
                if not content:
                    return {"kr": {}, "euw1": {}}
                return json.loads(content)
        except:
            pass
    return {"kr": {}, "euw1": {}}


def save_database(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def get_short_version(game_version):
    parts = game_version.split(".")
    return f"{parts[0]}.{parts[1]}"


def get_latest_patch(db):
    all_patches = set()
    for r in db:
        for m in db[r].values():
            # SAFETY CHECK: If the match dict is empty, skip it
            if not m:
                continue

            # Get the first available role key (e.g., 'JUNGLE') to find the patch
            first_role = next(iter(m))
            all_patches.add(m[first_role]["patch"])

    if not all_patches:
        return "14.1"

    def version_key(v):
        try:
            return tuple(map(int, v.split(".")))
        except:
            return (0, 0)

    return max(all_patches, key=version_key)


def extract_bans(match_info):
    return [
        b["championId"]
        for t in match_info["teams"]
        for b in t["bans"]
        if b["championId"] != -1
    ]


# --- 4. MAIN LOGIC ---
def fetch_data():
    db = load_database()
    stats = {"new": 0, "skipped": 0}
    champ_id_to_name = {}

    # PHASE 1: CRAWLER
    for region_code, region_name in REGIONS:
        if region_code not in db:
            db[region_code] = {}
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

                    matches = smart_request(
                        watcher.match.matchlist_by_puuid,
                        region_code,
                        puuid,
                        count=MATCH_HISTORY_COUNT,
                    )
                    if not matches:
                        continue

                    for match_id in matches:
                        if match_id in db[region_code]:
                            stats["skipped"] += 1
                            continue

                        match_detail = smart_request(
                            watcher.match.by_id, region_code, match_id
                        )
                        if match_detail:
                            info = match_detail["info"]
                            version = get_short_version(info["gameVersion"])
                            match_bans = extract_bans(info)

                            # --- ATOMIC SAVE FIX ---
                            temp_match_data = {}

                            for p in info["participants"]:
                                champ_id_to_name[p["championId"]] = p["championName"]
                                role = p["teamPosition"]

                                if role in VALID_ROLES:
                                    temp_match_data[role] = {
                                        "champ": p["championName"],
                                        "win": p["win"],
                                        "patch": version,
                                        "k": p["kills"],
                                        "d": p["deaths"],
                                        "a": p["assists"],
                                        "bans": match_bans,
                                    }

                            if temp_match_data:
                                db[region_code][match_id] = temp_match_data
                                stats["new"] += 1
                                logger.info(f"   [+] Added Match: {match_id}")

                    logger.info(f"Player {i + 1}/{PLAYER_COUNT} scanned.")
                except Exception as e:
                    logger.error(f"Player error: {e}")
        except Exception as e:
            logger.error(f"Region error: {e}")

    save_database(db)

    # PHASE 2: AGGREGATION
    logger.info("Generating Frontend JSON...")
    current_patch = get_latest_patch(db)

    # Calculate Meta
    total_games = sum(len(db[r]) for r in db)

    # Count patch games SAFELY
    patch_games = 0
    for r in db:
        for m in db[r].values():
            if not m:
                continue
            first_role = next(iter(m))
            if m[first_role]["patch"] == current_patch:
                patch_games += 1

    frontend_data = {
        "meta": {
            "total_games": total_games,
            "patch_games": patch_games,
            "last_updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "current_patch": current_patch,
        },
        "regions": {},
    }

    for region in db:
        frontend_data["regions"][region] = {"season": {}, "patch": {}}

        # Initialize roles in frontend data
        for r in VALID_ROLES:
            frontend_data["regions"][region]["season"][r] = []
            frontend_data["regions"][region]["patch"][r] = []

        matches = db[region].values()

        def get_role_stats(match_list, role_name):
            role_data = [m[role_name] for m in match_list if role_name in m]
            if not role_data:
                return []

            total = len(role_data)
            stats = {}

            # Performance
            for d in role_data:
                name = d["champ"]
                if name not in stats:
                    stats[name] = {"g": 0, "w": 0, "k": 0, "d": 0, "a": 0, "b": 0}
                s = stats[name]
                s["g"] += 1
                if d["win"]:
                    s["w"] += 1
                s["k"] += d.get("k", 0)
                s["d"] += d.get("d", 0)
                s["a"] += d.get("a", 0)

            # Bans
            for m in match_list:
                if role_name in m:
                    first_entry = m[role_name]
                    if "bans" in first_entry:
                        for bid in first_entry["bans"]:
                            if bid in champ_id_to_name:
                                bname = champ_id_to_name[bid]
                                if bname not in stats:
                                    stats[bname] = {
                                        "g": 0,
                                        "w": 0,
                                        "k": 0,
                                        "d": 0,
                                        "a": 0,
                                        "b": 0,
                                    }
                                stats[bname]["b"] += 1

            # Finalize
            results = []
            for name, s in stats.items():
                if s["g"] == 0 and s["b"] == 0:
                    continue

                pick_rate = round((s["g"] / total * 100), 1)
                ban_rate = round((s["b"] / total * 100), 1)
                win_rate = round((s["w"] / s["g"] * 100), 1) if s["g"] > 0 else 0
                kda = round((s["k"] + s["a"]) / (s["d"] if s["d"] > 0 else 1), 2)

                if s["g"] > 0 or ban_rate > 1.0:
                    results.append(
                        {
                            "name": name,
                            "count": s["g"],
                            "pick_rate": pick_rate,
                            "win_rate": win_rate,
                            "ban_rate": ban_rate,
                            "kda": kda,
                        }
                    )
            return sorted(results, key=lambda x: x["pick_rate"], reverse=True)[:15]

        # Process per role
        # 1. Filter out empty "Ghost" matches
        valid_matches = [m for m in matches if m]
        season_matches = valid_matches

        # 2. Safely filter for patch matches (FIXED LINE BELOW)
        patch_matches = [
            m for m in valid_matches if m[next(iter(m))]["patch"] == current_patch
        ]

        for role in VALID_ROLES:
            frontend_data["regions"][region]["season"][role] = get_role_stats(
                season_matches, role
            )
            frontend_data["regions"][region]["patch"][role] = get_role_stats(
                patch_matches, role
            )

    with open(OUTPUT_FILE, "w") as f:
        json.dump(frontend_data, f)
    logger.info(f"Complete. New matches: {stats['new']}")


if __name__ == "__main__":
    fetch_data()
