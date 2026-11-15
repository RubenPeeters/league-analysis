"""Download one player's ranked solo matches into the ladder's store.

Usage: python src/player.py "name#tag" kr|euw1
"""

import logging
import os
import sys
from collections import Counter

from dotenv import load_dotenv
from riotwatcher import LolWatcher, RiotWatcher

from main import (
    RANKED_SOLO_QUEUE,
    REGIONS,
    RiotAuthError,
    connect_db,
    load_static_data,
    retention_window,
    scan_match_list,
    smart_request,
)

logger = logging.getLogger(__name__)

ROUTING = {"kr": "asia", "euw1": "europe"}
# Riot's largest page of a match list.
MATCHLIST_PAGE = 100


class UnknownRiotIdError(LookupError):
    """Riot has no account with this Riot ID."""


def player_match_count():
    return int(os.getenv("PLAYER_MATCH_COUNT", "100"))


def download_player_matches(
    watcher,
    riot_watcher,
    matches_col,
    old_ids_col,
    window,
    riot_id,
    region_code,
    valid_items=None,
    progress=None,
):
    """Store the ranked solo matches of riot_id ("name#tag") on region_code that are inside
    the window and not stored yet, at most PLAYER_MATCH_COUNT, the way the ladder crawl does.

    progress, when given, is called with (matches done, matches total).
    Return (puuid, ids of the player's matches inside the window, newest first).
    Raise UnknownRiotIdError for an unknown Riot ID, ValueError for a malformed one or an
    unknown region, RiotAuthError for a rejected key.
    """
    if region_code not in ROUTING:
        raise ValueError(f"region must be one of {', '.join(ROUTING)}, not {region_code!r}")
    game_name, _, tag_line = riot_id.partition("#")
    if not game_name or not tag_line:
        raise ValueError(f"Riot ID must look like name#tag, not {riot_id!r}")

    account = smart_request(
        riot_watcher.account.by_riot_id, ROUTING[region_code], game_name, tag_line
    )
    if not account:
        raise UnknownRiotIdError(f"No account {riot_id} on {ROUTING[region_code]}")
    puuid = account["puuid"]

    count = player_match_count()
    match_ids = []
    while len(match_ids) < count:
        page = smart_request(
            watcher.match.matchlist_by_puuid,
            region_code,
            puuid,
            start=len(match_ids),
            count=min(MATCHLIST_PAGE, count - len(match_ids)),
            queue=RANKED_SOLO_QUEUE,
        )
        match_ids += page or []
        if not page or len(page) < MATCHLIST_PAGE:
            break

    counts = Counter(listed=len(match_ids), stored=0, downloaded=0, too_old=0, timelines=0)
    found = scan_match_list(
        watcher,
        matches_col,
        old_ids_col,
        window,
        region_code,
        match_ids,
        valid_items,
        counts,
        progress,
    )
    logger.info(
        f"{riot_id}: {counts['listed']} listed, {counts['stored']} already stored, "
        f"{counts['downloaded']} downloaded, {counts['too_old']} recorded as too old, "
        f"{counts['timelines']} timelines fetched."
    )
    return puuid, found


def main(argv):
    if len(argv) != 2 or argv[1] not in ROUTING:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    riot_id, region_code = argv
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING").upper())
    api_key = os.getenv("RIOT_API_KEY")
    mongo_uri = os.getenv("MONGODB_URI")
    if not api_key or not mongo_uri:
        print("RIOT_API_KEY and MONGODB_URI must be set", file=sys.stderr)
        return 1

    _, _, valid_items, _, versions = load_static_data()
    window = retention_window(versions, int(os.getenv("KEEP_PREVIOUS_PATCHES", "2")))
    if not window:
        print("Could not read the patch list from Data Dragon", file=sys.stderr)
        return 1
    matches_col, old_ids_col = connect_db(mongo_uri)

    def progress(done, total):
        print(f"\r{done}/{total} matches", end="", flush=True)

    try:
        puuid, found = download_player_matches(
            LolWatcher(api_key),
            RiotWatcher(api_key),
            matches_col,
            old_ids_col,
            window,
            riot_id,
            region_code,
            valid_items,
            progress,
        )
    except (UnknownRiotIdError, ValueError, RiotAuthError) as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    region_name = dict(REGIONS)[region_code]
    print(f"\n{riot_id} ({region_name}, {puuid}): {len(found)} matches on {', '.join(window)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
