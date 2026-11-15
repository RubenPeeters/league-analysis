# Decisions

## 2026-11-15: managed by forge

The repo is managed by forge: one issue, one branch, one PR into `dev`; `main` is released code.
`make check` (ruff and pytest through uv) is the gate, locally and in CI.
**Why:** the site had been publishing an empty `data.json` since 2026-10-21 without anyone noticing; Ruben asked to add the repo to forge before fixing and improving it.

## 2026-11-15: data.json lives on the `data` branch

`update_data.yml` commits each generated `data.json` to the `data` branch and no longer to `main`. `data/` is not tracked on `main` or `dev`. The history up to this date remains in the history of `main` (commits "Auto-update data").
**Why:** forge protects `main` against direct pushes, so the 6-hourly commit to `main` could not continue. A separate branch keeps the snapshot history, which is the only archive: MongoDB is purged at every patch.

## 2026-11-15: src/main.py left unchanged at adoption

The adoption commit did not touch `src/main.py`; it is excluded from ruff in `pyproject.toml` until it is restructured.
**Why:** the script runs its database and API calls at import, so it could not be tested; changing it without tests was left to its own issue.

## 2026-11-15: src/main.py restructured, still one file

`src/main.py` does nothing at import; `main()` builds the clients, the collection and the Data Dragon data and passes them down. The crawler (`crawl`) is separate from the analysis (`build_frontend_data` and its helpers), which is pure. It stays one file at the same path, and is linted.
**Why:** to make it testable (#2) without touching `update_data.yml`, which calls `python src/main.py` and which the forge bot cannot change. Tests import it through `pythonpath = ["src"]` in `pyproject.toml`.

## 2026-11-15: a failed Riot run writes nothing and purges nothing

`main()` exits 1 without purging the database or writing `data.json` when the Riot API rejects the key (401/403) or when no region could be scanned. The purge of old-patch matches now runs after the crawl instead of before it.
**Why:** a rejected key produced a green run that published an empty `data.json` every 6 hours (#3). A non-zero exit stops `update_data.yml` before its commit and deploy, so the site keeps its last good data; purging only after the API has answered means a broken key cannot empty the database.

## 2026-11-15: both sides counted; pick_rate is per role slot

`aggregate()` counts both players of the role in every match, for champion statistics, builds, context builds and leaderboards. `pick_rate` is picks of the champion in the role divided by the number of role slots (2 per match), as a percentage. `ban_rate` is unchanged: bans divided by matches.
**Why:** only the blue-side player of each role was counted, so half the picks were ignored (#10). Dividing by role slots instead of matches keeps a role's pick rates summing to about 100, the scale the site and the per-patch archive on the `data` branch already use.

## 2026-11-15: per-patch archive in history.json on the `data` branch

The meta of each patch is kept in `history.json` on the `data` branch, one entry per patch. `update_data.yml` copies it into `data/` before the generator runs and commits it back afterwards; the site publishes it with `data.json`. The entries for 16.2 to 16.20 were backfilled by `analysis/meta_over_patches.py` from the `data.json` versions in the history of `main`; they are marked `"source": "backfill"` and hold only the 15 most-picked champions per role, blue side only, with estimated game counts.
**Why:** Ruben asked for a page on the site showing how the meta changed over the patches. MongoDB is purged at every patch, so the per-patch result has to be stored when it is produced; a file on the `data` branch keeps it in git next to the snapshots and keeps the generator free of extra database state.
