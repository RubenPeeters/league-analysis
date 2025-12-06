import json
import time
import os
import logging
from riotwatcher import LolWatcher, ApiError
from collections import Counter
from dotenv import load_dotenv

# --- 1. SETUP LOGGING ---
load_dotenv()
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- 2. CONFIGURATION ---
API_KEY = os.getenv("RIOT_API_KEY")
# Regions: API ID -> Display Name
REGIONS = [("kr", "Korea"), ("euw1", "Europe West")]
# Settings
PLAYER_COUNT = int(os.getenv("PLAYER_COUNT", 10))
MATCH_HISTORY_COUNT = 20
DB_FILE = "match_database.json"
OUTPUT_FILE = "data.json"

if not API_KEY:
    logger.critical("No API Key found! Check your .env file.")
    raise ValueError("Missing RIOT_API_KEY")

watcher = LolWatcher(API_KEY)

# --- 3. HELPER FUNCTIONS ---


def smart_request(func, *args, **kwargs):
    """
    Wraps API calls. Automatically handles Rate Limits (429) by sleeping
    exactly as long as Riot tells us to.
    """
    func_name = func.__name__ if hasattr(func, "__name__") else "API Call"
    while True:
        try:
            logger.debug(f"Requesting: {func_name} | Args: {args}")
            return func(*args, **kwargs)
        except ApiError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 10))
                logger.warning(
                    f"⚠️ Rate Limit Hit on {func_name}. Sleeping {retry_after}s..."
                )
                time.sleep(retry_after + 1)
                continue
            elif e.response.status_code == 404:
                return None
            else:
                logger.error(f"❌ API Error {e.response.status_code}: {e}")
                raise e
        except Exception as e:
            logger.error(f"❌ Unexpected Error: {e}")
            raise e


def load_database():
    """Loads the database, handling empty/corrupt files safely."""
    if os.path.exists(DB_FILE):
        logger.info(f"Loading database from {DB_FILE}...")
        try:
            with open(DB_FILE, "r") as f:
                content = f.read()
                if not content:
                    raise ValueError("Empty File")
                data = json.loads(content)
                total = sum(len(matches) for matches in data.values())
                logger.info(f"Database loaded. Contains {total} matches.")
                return data
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"⚠️ Database corrupted or empty. Starting fresh.")

    # Return empty structure if no DB exists
    return {"kr": {}, "euw1": {}}


def save_database(db):
    logger.info(f"Saving database to {DB_FILE}...")
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def get_short_version(game_version):
    """Turns '14.23.456.7890' into '14.23'"""
    parts = game_version.split(".")
    return f"{parts[0]}.{parts[1]}"


def get_latest_patch(db):
    """
    Scans DB for the mathematically highest version number.
    Handles semantic versioning (14.10 > 14.2).
    """
    all_patches = set()
    for r in db:
        for m in db[r].values():
            all_patches.add(m["patch"])

    if not all_patches:
        return "14.1"

    def version_key(v):
        try:
            major, minor = v.split(".")
            return (int(major), int(minor))
        except ValueError:
            return (0, 0)

    latest = max(all_patches, key=version_key)
    logger.info(f"Latest Patch Detected (Semantic Sort): {latest}")
    return latest


def extract_bans(match_info):
    """Returns a list of Champion IDs that were banned in the match."""
    bans = []
    for team in match_info["teams"]:
        for ban in team["bans"]:
            if ban["championId"] != -1:  # -1 means 'No Ban'
                bans.append(ban["championId"])
    return bans


# --- 4. MAIN LOGIC ---


def fetch_data():
    db = load_database()
    stats = {"new": 0, "skipped": 0, "errors": 0}

    # Used to convert Ban IDs to Names later
    champ_id_to_name = {}

    # --- PHASE 1: CRAWLER (Get New Data) ---
    for region_code, region_name in REGIONS:
        # Initialize region if missing (Fixes the crash when adding new regions)
        if region_code not in db:
            db[region_code] = {}

        logger.info(f"=== Starting Scan: {region_name.upper()} ({region_code}) ===")
        try:
            logger.info("Fetching Challenger League...")
            challenger = smart_request(
                watcher.league.challenger_by_queue, region_code, "RANKED_SOLO_5x5"
            )
            entries = sorted(
                challenger["entries"], key=lambda x: x["leaguePoints"], reverse=True
            )[:PLAYER_COUNT]

            for i, entry in enumerate(entries):
                try:
                    # Resolve PUUID
                    if "puuid" in entry:
                        puuid = entry["puuid"]
                    elif "summonerId" in entry:
                        summoner_data = smart_request(
                            watcher.summoner.by_id, region_code, entry["summonerId"]
                        )
                        puuid = summoner_data["puuid"]
                    else:
                        continue

                    # Get Match History
                    matches = smart_request(
                        watcher.match.matchlist_by_puuid,
                        region_code,
                        puuid,
                        count=MATCH_HISTORY_COUNT,
                    )
                    if not matches:
                        continue

                    new_count = 0
                    for match_id in matches:
                        # Skip if we already have this match
                        if match_id in db[region_code]:
                            stats["skipped"] += 1
                            continue

                        # Download New Match
                        match_detail = smart_request(
                            watcher.match.by_id, region_code, match_id
                        )
                        if match_detail:
                            info = match_detail["info"]
                            version = get_short_version(info["gameVersion"])
                            match_bans = extract_bans(info)

                            for p in info["participants"]:
                                # Update ID map for Ban lookup later
                                champ_id_to_name[p["championId"]] = p["championName"]

                                if p["teamPosition"] == "JUNGLE":
                                    db[region_code][match_id] = {
                                        "champ": p["championName"],
                                        "win": p["win"],
                                        "patch": version,
                                        "timestamp": info["gameCreation"],
                                        # New Stats
                                        "k": p["kills"],
                                        "d": p["deaths"],
                                        "a": p["assists"],
                                        "bans": match_bans,
                                    }
                                    stats["new"] += 1
                                    new_count += 1
                                    logger.info(
                                        f"   [+] New: {p['championName']} ({p['kills']}/{p['deaths']}/{p['assists']})"
                                    )

                    logger.info(
                        f"Player {i + 1}/{PLAYER_COUNT} done. +{new_count} matches."
                    )

                except Exception as e:
                    stats["errors"] += 1
                    logger.error(f"Player error: {e}")
                    continue
        except Exception as e:
            logger.error(f"Region error: {e}")

    # Save Progress
    save_database(db)

    # --- PHASE 2: ANALYST (Aggregation) ---
    logger.info("Generating frontend JSON...")

    current_patch = get_latest_patch(db)

    # 1. Calculate Meta Counts
    total_games_db = sum(len(matches) for matches in db.values())
    total_patch_games = 0
    for r in db:
        for m in db[r].values():
            if m["patch"] == current_patch:
                total_patch_games += 1

    # 2. Build Frontend Data Structure
    frontend_data = {
        "meta": {
            "total_games": total_games_db,
            "patch_games": total_patch_games,
            "last_updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "current_patch": current_patch,
        },
        "regions": {},
    }

    for region in db:
        frontend_data["regions"][region] = {"season": [], "patch": []}
        matches = db[region].values()

        # Helper: Process a list of matches into final stats
        def process_subset(subset_matches):
            if not subset_matches:
                return []

            total_games_scanned = len(subset_matches)
            champ_stats = {}  # {Name: {games, wins, k, d, a, bans}}

            # 1. Performance Stats (KDA, Win Rate)
            for m in subset_matches:
                name = m["champ"]
                if name not in champ_stats:
                    champ_stats[name] = {
                        "games": 0,
                        "wins": 0,
                        "k": 0,
                        "d": 0,
                        "a": 0,
                        "bans": 0,
                    }

                s = champ_stats[name]
                s["games"] += 1
                if m["win"]:
                    s["wins"] += 1
                s["k"] += m.get("k", 0)
                s["d"] += m.get("d", 0)
                s["a"] += m.get("a", 0)

            # 2. Ban Stats (Loop all matches to see if champ was banned)
            for m in subset_matches:
                if "bans" in m:
                    for banned_id in m["bans"]:
                        if banned_id in champ_id_to_name:
                            b_name = champ_id_to_name[banned_id]
                            if b_name not in champ_stats:
                                champ_stats[b_name] = {
                                    "games": 0,
                                    "wins": 0,
                                    "k": 0,
                                    "d": 0,
                                    "a": 0,
                                    "bans": 0,
                                }
                            champ_stats[b_name]["bans"] += 1

            # 3. Calculate Percentages
            processed = []
            for name, s in champ_stats.items():
                if s["games"] == 0 and s["bans"] == 0:
                    continue

                win_rate = (
                    round((s["wins"] / s["games"] * 100), 1) if s["games"] > 0 else 0
                )
                pick_rate = round((s["games"] / total_games_scanned * 100), 1)
                ban_rate = round((s["bans"] / total_games_scanned * 100), 1)

                # KDA Calculation
                deaths = s["d"] if s["d"] > 0 else 1
                kda = round((s["k"] + s["a"]) / deaths, 2)

                # Filter: Must be played at least once OR have high ban rate (>1%)
                if s["games"] > 0 or ban_rate > 1.0:
                    processed.append(
                        {
                            "name": name,
                            "count": s["games"],
                            "pick_rate": pick_rate,
                            "win_rate": win_rate,
                            "ban_rate": ban_rate,
                            "kda": kda,
                        }
                    )

            # Sort by Pick Rate
            return sorted(processed, key=lambda x: x["pick_rate"], reverse=True)[:15]

        # Process Season (All matches)
        frontend_data["regions"][region]["season"] = process_subset(list(matches))

        # Process Patch (Current only)
        patch_matches = [m for m in matches if m["patch"] == current_patch]
        frontend_data["regions"][region]["patch"] = process_subset(patch_matches)

    with open(OUTPUT_FILE, "w") as f:
        json.dump(frontend_data, f)

    logger.info(f"Run Complete. New: {stats['new']} | Skipped: {stats['skipped']}")


if __name__ == "__main__":
    fetch_data()
