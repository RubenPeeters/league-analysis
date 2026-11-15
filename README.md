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
2. fetches their last 100 matches, keeps those on the current patch, and stores them in
   MongoDB Atlas (matches from older patches are purged);
3. aggregates the stored matches into `data/data.json`, which the front end in `ui/` reads.

## How it is published

`.github/workflows/update_data.yml` runs every 6 hours, on manual dispatch, and on pushes to
`main`. It runs `src/main.py`, commits the new `data.json` to the `data` branch (the snapshot
history), and publishes `ui/` plus `data/data.json` to the `gh-pages` branch, which GitHub
Pages serves. `data/` is not tracked on `main` or `dev`.

## Environment variables

- `RIOT_API_KEY` (required)
- `MONGODB_URI` (required)
- `PLAYER_COUNT`
- `LOG_LEVEL`

Locally they are read from a `.env` file in the repo root. In CI, `RIOT_API_KEY` and
`MONGODB_URI` come from Actions secrets.

## Running locally

```sh
uv sync
uv run python src/main.py        # writes data/data.json
python -m http.server            # serve the repo root
```

Then open http://localhost:8000/ui/.

## Checks

```sh
make check
```

Runs ruff (lint and format check) and pytest through uv; CI runs the same target.
Decisions are recorded in [`docs/decisions.md`](docs/decisions.md).
