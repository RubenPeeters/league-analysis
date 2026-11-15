"""How the tracked top-ladder meta changed across patches.

Source: every version of data/data.json committed to `main` (the "Auto-update data"
commits, 2025-12-06 to 2026-11-15). For each patch the last committed snapshot with
games is used: the database is purged at each patch change, so that snapshot holds the
most games of the patch.

Scope: patches whose snapshot has the current shape (regions -> role -> champion list),
i.e. produced by the generator as of commit 3b77d4b (2026-01-24). Earlier snapshots
(15.24, 16.1) have another shape and 16.1 was written while the purge was disabled.

Deterministic for a fixed `main` history. Run from the repo root:
    python3 analysis/meta_over_patches.py
Writes analysis/out/*.csv and prints a summary. Also writes analysis/out/history.json, the
per-patch archive in the shape the site's history page reads; it seeded `history.json` on
the `data` branch on 2026-11-15. Its entries are marked `"source": "backfill"`: they hold
only the 15 most-picked champions per role, count the blue-side player only, and their
region game counts are estimates (see n_games below).
"""

import csv
import json
import math
import subprocess
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"
ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
    ).stdout


def last_snapshot_per_patch():
    last = {}
    for line in git(
        "log", "--format=%H %cI", "--reverse", "main", "--", "data/data.json"
    ).splitlines():
        sha, date = line.split()
        try:
            data = json.loads(git("show", f"{sha}:data/data.json"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        meta = data.get("meta", {})
        if not meta.get("total_games"):
            continue
        regions = data.get("regions", {})
        if not all(
            isinstance(regions.get(r, {}).get(role), list) for r in regions for role in ROLES
        ):
            continue
        last[meta["current_patch"]] = (sha, date, data)
    return dict(sorted(last.items(), key=lambda kv: tuple(int(x) for x in kv[0].split("."))))


def main():
    OUT.mkdir(exist_ok=True)
    snaps = last_snapshot_per_patch()
    patches = list(snaps)

    # long table; n_games = games of the region in the snapshot, recovered as the median of
    # count / pick_rate over the listed champions (the file does not store it per region)
    rows, table, n_games = [], {}, {}
    for patch, (sha, date, data) in snaps.items():
        for region, roles in data["regions"].items():
            for role in ROLES:
                champs = roles[role]
                est = sorted(
                    c["count"] / (c["pick_rate"] / 100)
                    for c in champs
                    if c["pick_rate"] and c["count"]
                )
                n = round(est[len(est) // 2]) if est else 0
                n_games[patch, region, role] = n
                table[patch, region, role] = {c["name"]: c for c in champs}
                for rank, c in enumerate(champs, 1):
                    rows.append(
                        [
                            patch,
                            date[:10],
                            sha[:7],
                            region,
                            role,
                            rank,
                            c["name"],
                            c["count"],
                            n,
                            c["pick_rate"],
                            c["win_rate"],
                            c["ban_rate"],
                            c["kda"],
                        ]
                    )
    with open(OUT / "champion_by_patch.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "patch",
                "snapshot_date",
                "commit",
                "region",
                "role",
                "rank",
                "champion",
                "games",
                "region_games_est",
                "pick_rate",
                "win_rate",
                "ban_rate",
                "kda",
            ]
        )
        w.writerows(rows)

    regions = sorted({k[1] for k in table})

    history = {"schema": 1, "patches": []}
    for patch, (sha, date, _) in snaps.items():
        entry = {
            "patch": patch,
            "snapshot_date": date[:10],
            "source": "backfill",
            "complete": False,
            "commit": sha[:7],
            "regions": {},
        }
        for region in regions:
            est = sorted(n_games[patch, region, role] for role in ROLES)
            entry["regions"][region] = {
                "games": est[len(est) // 2],
                "games_estimated": True,
                "roles": {
                    role: [
                        {
                            "name": c["name"],
                            "games": c["count"],
                            "pick_rate": c["pick_rate"],
                            "win_rate": c["win_rate"],
                            "ban_rate": c["ban_rate"],
                        }
                        for c in table[patch, region, role].values()
                    ]
                    for role in ROLES
                },
            }
        history["patches"].append(entry)
    with open(OUT / "history.json", "w") as f:
        json.dump(history, f, separators=(",", ":"))

    # turnover between consecutive patches: champions entering the top 15, and of the top 5
    turn = []
    for region in regions:
        for role in ROLES:
            for a, b in pairwise(patches):
                ta, tb = table[a, region, role], table[b, region, role]
                new15 = [c for c in tb if c not in ta]
                top5a, top5b = list(ta)[:5], list(tb)[:5]
                turn.append(
                    [
                        b,
                        region,
                        role,
                        len(new15),
                        len([c for c in top5b if c not in top5a]),
                        " ".join(new15),
                    ]
                )
    with open(OUT / "turnover.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["patch", "region", "role", "new_in_top15", "new_in_top5", "entrants"])
        w.writerows(turn)

    # pick-rate movers between consecutive patches, champion listed in both; z from a
    # two-proportion test on games / region_games_est
    movers = []
    for region in regions:
        for role in ROLES:
            for a, b in pairwise(patches):
                ta, tb = table[a, region, role], table[b, region, role]
                na, nb = n_games[a, region, role], n_games[b, region, role]
                for name in ta.keys() & tb.keys():
                    xa, xb = ta[name]["count"], tb[name]["count"]
                    pa, pb = xa / na, xb / nb
                    p = (xa + xb) / (na + nb)
                    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb))
                    movers.append(
                        [
                            b,
                            region,
                            role,
                            name,
                            round(100 * pa, 1),
                            round(100 * pb, 1),
                            round(100 * (pb - pa), 1),
                            xa,
                            xb,
                            round((pb - pa) / se, 1),
                        ]
                    )
    movers.sort(key=lambda r: -abs(r[-1]))
    with open(OUT / "movers.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "patch",
                "region",
                "role",
                "champion",
                "pick_before",
                "pick_after",
                "delta_pp",
                "games_before",
                "games_after",
                "z",
            ]
        )
        w.writerows(movers)

    # ---- summary ----
    print(f"patches: {patches[0]} .. {patches[-1]} ({len(patches)})")
    print("\nregion games per patch (JUNGLE estimate):")
    for p in patches:
        print(
            f"  {p:>6} {snaps[p][1][:10]} "
            + " ".join(f"{r}={n_games[p, r, 'JUNGLE']}" for r in regions)
        )

    print("\nmean champions entering the top 15 per patch, by role and region:")
    agg = defaultdict(list)
    for patch, region, role, n15, n5, _ in turn:
        agg[region, role].append((n15, n5))
    for role in ROLES:
        print(
            f"  {role:8}"
            + "  ".join(
                f"{r}: top15 {sum(x[0] for x in agg[r, role]) / len(agg[r, role]):.1f}, "
                f"top5 {sum(x[1] for x in agg[r, role]) / len(agg[r, role]):.1f}"
                for r in regions
            )
        )

    print("\ntop-15 entrants per patch, summed over roles:")
    per_patch = defaultdict(lambda: defaultdict(int))
    for patch, region, role, n15, n5, _ in turn:
        per_patch[patch][region] += n15
    for p in patches[1:]:
        print(f"  {p:>6} " + " ".join(f"{r}={per_patch[p][r]}" for r in regions))

    print("\nmost-picked champion per role and patch:")
    for region in regions:
        for role in ROLES:
            seq, prev = [], None
            for p in patches:
                top = next(iter(table[p, region, role]), "-")
                if top != prev:
                    seq.append(f"{p}:{top}")
                prev = top
            print(f"  {region} {role:8} " + " ".join(seq))

    print(f"\npatches in the top 15 (of {len(patches)}), champions present in >= 17:")
    for region in regions:
        for role in ROLES:
            cnt = defaultdict(int)
            for p in patches:
                for c in table[p, region, role]:
                    cnt[c] += 1
            keep = sorted(((n, c) for c, n in cnt.items() if n >= 17), reverse=True)
            print(
                f"  {region} {role:8} distinct={len(cnt):3} "
                + " ".join(f"{c}({n})" for n, c in keep)
            )

    print("\nKR/EUW overlap of the top 15 (mean shared champions per patch):")
    for role in ROLES:
        shared = [
            len(table[p, regions[0], role].keys() & table[p, regions[1], role].keys())
            for p in patches
        ]
        print(f"  {role:8} {sum(shared) / len(shared):.1f}  min {min(shared)} max {max(shared)}")

    print("\n25 largest pick-rate changes between consecutive patches (by z):")
    for m in movers[:25]:
        print(
            "  {:>6} {:5} {:8} {:14} {:5.1f} -> {:5.1f}  ({:+.1f} pp, games {} -> {}, z {:+.1f})".format(
                *m[:6], m[6], m[7], m[8], m[9]
            )
        )
    print(
        f"\nmover pairs: {len(movers)}; |z| >= 3: {sum(abs(m[-1]) >= 3 for m in movers)}; "
        f"|z| >= 4: {sum(abs(m[-1]) >= 4 for m in movers)}"
    )


if __name__ == "__main__":
    main()
