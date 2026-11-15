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
    aggregate,
    connect_db,
    load_static_data,
    retention_window,
    scan_match_list,
    smart_request,
    summarize_match,
)

logger = logging.getLogger(__name__)

ROUTING = {"kr": "asia", "euw1": "europe"}
# Riot's largest page of a match list.
MATCHLIST_PAGE = 100


# A champion (per role and region) needs this many of the player's games to be analysed.
PLAYER_MIN_GAMES = 5
# A lane opponent needs this many games against it.
OPPONENT_MIN_GAMES = 3
STARTING_SETS = 3
# Keys of aggregate()'s champion stats that describe the player; the rest are ladder-only.
PLAYER_KEYS = (
    "win_rate",
    "item_games",
    "items",
    "boots",
    "rune_games",
    "runes",
    "spells",
    "timeline_games",
    "first_items",
    "paths",
    "boots_position",
    "skill_start",
    "skill_max",
)


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


def most_frequent(counts):
    """(key, count) of the largest count, smallest key on a tie; None when counts is empty."""
    return min(counts.items(), key=lambda x: (-x[1], x[0])) if counts else None


def add_ladder_shares(entry, ladder_champ):
    """Give each item, boots, rune page and first item of entry the ladder's share (0.0 when the
    ladder never has it; None without the champion or without games of that kind) and entry the
    ladder's game counts. ladder_champ is an uncapped aggregate() entry."""
    lc = ladder_champ or {}
    by_id = lambda r: r["id"]
    page = lambda r: (r["keystone"], r["primary_style"], r["secondary_style"])
    for rows, ladder_rows, games, key in (
        (
            entry["items"] + entry["boots"],
            lc.get("items", []) + lc.get("boots", []),
            "item_games",
            by_id,
        ),
        (entry["first_items"], lc.get("first_items", []), "timeline_games", by_id),
        (entry["runes"], lc.get("runes", []), "rune_games", page),
    ):
        shares = {key(r): r["share"] for r in ladder_rows}
        for r in rows:
            r["ladder_share"] = shares.get(key(r), 0.0) if lc.get(games) else None
    entry["ladder"] = ladder_champ and {
        "games": ladder_champ["count"],
        **{k: ladder_champ[k] for k in ("item_games", "rune_games", "timeline_games")},
    }


def analyze_player(matches, puuid, ladder_matches, valid_items=None, boots=frozenset()):
    """Builds of the player puuid per champion and role (at least PLAYER_MIN_GAMES games) and per
    lane opponent (at least OPPONENT_MIN_GAMES games), next to the ladder's shares.

    matches are stored match documents; ladder_matches are the stored matches of the current
    patch, the ones data.json is built from. Remakes are left out. Pure: the result is
    JSON-serialisable.
    """
    ladder_summaries = {}
    for m in ladder_matches:
        ladder_summaries.setdefault(m.get("_region"), []).append(summarize_match(m, {}, set(), {}))

    # (region, role, champion) -> the player's summarized games, opponent as the only context
    games = {}
    for m in matches:
        raw = m.get("info", {}).get("participants", [])
        mine = next((i for i, p in enumerate(raw) if p.get("puuid") == puuid), None)
        if mine is None:
            continue
        summary = summarize_match(m, {}, set(), {})
        if summary["participants"][mine]["remake"]:
            continue
        me, my_raw = summary["participants"][mine], raw[mine]
        opponent = next(
            (
                p["championName"]
                for p in raw
                if p["teamId"] != my_raw["teamId"] and p["teamPosition"] == my_raw["teamPosition"]
            ),
            None,
        )
        me["context"] = [opponent] if opponent else []
        start = (me["timeline"] or {}).get("start")
        me["start"] = tuple(sorted(start)) if start is not None else None
        games.setdefault((m.get("_region"), me["role"], me["champ"]), []).append(me)

    champions = []
    for (region, role, champ), mine in games.items():
        if len(mine) < PLAYER_MIN_GAMES:
            continue
        summaries = [{"bans": [], "participants": [p]} for p in mine]
        # One other game is enough: the opponent side already has OPPONENT_MIN_GAMES.
        (stats,) = aggregate(
            summaries, role, region, {}, valid_items, {}, boots, OPPONENT_MIN_GAMES, 1
        )
        entry = {"region": region, "role": role, "champion": champ, "games": stats["count"]}
        entry.update({k: stats[k] for k in PLAYER_KEYS})

        starts = Counter(p["start"] for p in mine if p["start"] is not None)
        entry["start_games"] = starts.total()
        entry["starting_items"] = [
            {"ids": list(ids), "games": g}
            for ids, g in sorted(starts.items(), key=lambda x: (-x[1], x[0]))[:STARTING_SETS]
        ]

        ladder_champ = next(
            (
                c
                for c in aggregate(
                    ladder_summaries.get(region, []),
                    role,
                    region,
                    {},
                    valid_items,
                    {},
                    boots,
                    capped=False,
                )
                if c["name"] == champ and c["count"] > 0
            ),
            None,
        )
        add_ladder_shares(entry, ladder_champ)

        by_opponent = {}
        for p in mine:
            if p["context"]:
                by_opponent.setdefault(p["context"][0], []).append(p)
        opponents = []
        for opp, opp_games in by_opponent.items():
            if len(opp_games) < OPPONENT_MIN_GAMES:
                continue
            keystone = most_frequent(
                Counter(
                    p["perks"]["primary"][0] for p in opp_games if (p["perks"] or {}).get("primary")
                )
            )
            start = most_frequent(Counter(p["start"] for p in opp_games if p["start"] is not None))
            card = stats["matchups"].get(opp)
            opponents.append(
                {
                    "champion": opp,
                    "games": len(opp_games),
                    "win_rate": round(
                        sum(bool(p["win"]) for p in opp_games) / len(opp_games) * 100, 1
                    ),
                    "keystone": keystone and {"id": keystone[0], "games": keystone[1]},
                    "starting_items": start and {"ids": list(start[0]), "games": start[1]},
                    "other_games": len(mine) - len(opp_games),
                    "items": card["items"] if card else [],
                }
            )
        entry["opponents"] = sorted(opponents, key=lambda o: (-o["games"], o["champion"]))
        champions.append(entry)

    champions.sort(key=lambda c: (-c["games"], c["region"] or "", c["role"], c["champion"]))
    return {"puuid": puuid, "champions": champions}


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
