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
REGIONS = [("kr", "Korea"), ("euw1", "Europe West")]
PLAYER_COUNT = 10
MATCH_HISTORY_COUNT = 20
DB_FILE = "match_database.json"
OUTPUT_FILE = "data.json"

if not API_KEY:
    logger.critical("No API Key found! Exiting.")
    raise ValueError("Check your .env file.")

watcher = LolWatcher(API_KEY)

# --- 3. HELPER FUNCTIONS ---


def smart_request(func, *args, **kwargs):
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
            logger.warning(f"⚠️ Database corrupted/empty. Starting fresh.")
    return {"kr": {}, "euw1": {}}


def save_database(db):
    logger.info(f"Saving database to {DB_FILE}...")
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def get_short_version(game_version):
    parts = game_version.split(".")
    return f"{parts[0]}.{parts[1]}"


def get_latest_patch(db):
    """Returns the mathematically highest version number found in the DB."""
    all_patches = set()
    for r in db:
        for m in db[r].values():
            all_patches.add(m["patch"])

    if not all_patches:
        return "14.1"

    # Sort semantically: (14, 2) < (14, 10)
    def version_key(v):
        try:
            major, minor = v.split(".")
            return (int(major), int(minor))
        except ValueError:
            return (0, 0)

    latest = max(all_patches, key=version_key)
    logger.info(f"Latest Patch Detected (Semantic Sort): {latest}")
    return latest


# --- 4. MAIN LOGIC ---


def fetch_data():
    db = load_database()
    stats = {"new": 0, "skipped": 0, "errors": 0}

    for region_code, region_name in REGIONS:
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
                    if "puuid" in entry:
                        puuid = entry["puuid"]
                    elif "summonerId" in entry:
                        summoner_data = smart_request(
                            watcher.summoner.by_id, region_code, entry["summonerId"]
                        )
                        puuid = summoner_data["puuid"]
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

                    new_count = 0
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
                            for p in info["participants"]:
                                if p["teamPosition"] == "JUNGLE":
                                    db[region_code][match_id] = {
                                        "champ": p["championName"],
                                        "win": p["win"],
                                        "patch": version,
                                        "timestamp": info["gameCreation"],
                                    }
                                    stats["new"] += 1
                                    new_count += 1
                                    logger.info(
                                        f"   [+] New: {p['championName']} ({version})"
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

    save_database(db)

    # --- AGGREGATION ---
    logger.info("Generating frontend JSON...")
    frontend_data = {}

    # USE NEW SEMANTIC VERSION LOGIC
    current_patch = get_latest_patch(db)

    for region in db:
        frontend_data[region] = {"season": [], "patch": []}
        matches = db[region].values()

        for m in matches:
            entry = {"champ": m["champ"], "win": m["win"]}
            frontend_data[region]["season"].append(entry)
            if m["patch"] == current_patch:
                frontend_data[region]["patch"].append(entry)

        for cat in ["season", "patch"]:
            raw = frontend_data[region][cat]
            if not raw:
                continue

            counts = Counter([x["champ"] for x in raw])
            processed = []
            total = len(raw)
            for champ, count in counts.most_common(10):
                c_games = [x for x in raw if x["champ"] == champ]
                wins = sum(1 for x in c_games if x["win"])
                processed.append(
                    {
                        "name": champ,
                        "count": count,
                        "pick_rate": round((count / total) * 100, 1),
                        "win_rate": round((wins / len(c_games)) * 100, 1),
                    }
                )
            frontend_data[region][cat] = processed

    with open(OUTPUT_FILE, "w") as f:
        json.dump(frontend_data, f)

    logger.info(f"Run Complete. New: {stats['new']} | Skipped: {stats['skipped']}")


if __name__ == "__main__":
    fetch_data()
