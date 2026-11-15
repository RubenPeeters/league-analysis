# league-analysis — Elite Meta Scout

A static site showing which champions the top-ranked League of Legends solo-queue players in
Korea (KR) and Europe West (EUW) play, per role (top, jungle, mid, bot, support), on the
current patch: pick rate, win rate, ban rate, KDA, common items, and per-champion player
leaderboards.

Live at https://rubenpeeters.github.io/league-analysis/

## How the data is produced

`src/main.py`:

1. takes the top `PLAYER_COUNT` players per region from the Challenger ladder (topped up
   from Grandmaster and Master if it is short) via the Riot API;
2. fetches their last 100 ranked solo matches and stores, in MongoDB Atlas, those on the
   current patch or the `KEEP_PREVIOUS_PATCHES` (default 2) patches before it; older matches
   are recorded by id so they are not downloaded again, and purged once outside that window;
3. aggregates the stored current-patch matches into `data/data.json`, which the front end in
   `ui/` reads.

It also keeps `data/history.json`, the per-patch archive behind the meta-over-patches view:
one entry per patch with, per region and role, every champion picked and its games, pick,
win and ban rate. MongoDB keeps only a few patches, so each run replaces the current patch's
entry and leaves the others alone. The file lives on the `data` branch; the workflow copies it
into `data/` before the run and commits it back afterwards. Entries for 16.2 to 16.20 are a
backfill (`"source": "backfill"`, top 15 per role, estimated game counts).

## How it is published

`.github/workflows/update_data.yml` runs every 6 hours, on manual dispatch, and on pushes to
`main`. It runs `src/main.py`, commits the new `data.json` to the `data` branch (the snapshot
history), and publishes `ui/` plus `data/data.json` to the `gh-pages` branch, which GitHub
Pages serves. `data/` is not tracked on `main` or `dev`.

## Environment variables

- `RIOT_API_KEY` (required)
- `MONGODB_URI` (required)
- `PLAYER_COUNT`
- `KEEP_PREVIOUS_PATCHES` (default 2)
- `PLAYER_MATCH_COUNT` (default 100; `src/player.py` only)
- `LOG_LEVEL`

Locally they are read from a `.env` file in the repo root. In CI, `RIOT_API_KEY` and
`MONGODB_URI` come from Actions secrets.

## Running locally

```sh
uv sync
uv run python src/main.py        # writes data/data.json
uv run python src/player.py "name#tag" euw1   # stores one player's ranked matches
python -m http.server            # serve the repo root
```

Then open http://localhost:8000/ui/.

## Checks

```sh
make check
```

Runs ruff (lint and format check) and pytest through uv; CI runs the same target.
Decisions are recorded in [`docs/decisions.md`](docs/decisions.md).
