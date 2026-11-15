# CLAUDE.md

## What this repo is

league-analysis: a static site ("Elite Meta Scout") showing which champions the top-ranked
League of Legends players in KR and EUW play, per role. `src/main.py` reads ranked matches
from the Riot API, stores them in MongoDB Atlas, and writes `data/data.json`; `ui/` is the
plain HTML/CSS/JS front end that reads that file. Stack: see `stack.toml` (adopted from
forge's `python-package` template).

## How it is published

`.github/workflows/update_data.yml` runs every 6 hours and on pushes to `main`: it runs
`src/main.py`, commits the new `data.json` to the `data` branch (the snapshot history),
and pushes `ui/` plus `data/data.json` to `gh-pages`
(https://rubenpeeters.github.io/league-analysis/). `data/` is not tracked on `main` or `dev`.
The run needs the Actions secrets `RIOT_API_KEY` and `MONGODB_URI`; locally they come from
`.env`.

## Working agreement (forge)

- `main` is released code, `dev` is integration. Neither is pushed to directly.
- Every change is one issue -> one branch (`feat/<n>-slug`, `fix/<n>-slug`, ...) -> one PR into `dev`.
- PRs are reviewed by forge's reviewers; blocking findings must be fixed or rebutted.
- `make check` must pass before a commit is pushed. It runs exactly what CI runs.
- Keep PRs small and focused. No speculative abstractions, no drive-by refactors.
- Comments explain *why*, never *what*.
- Conventional Commit messages.
- Tests never call the Riot API or MongoDB; use recorded or hand-written fixtures.

## Ask before doing

- Adding a dependency
- Changing CI, deployment, or anything under `.github/` (the forge bot cannot push workflow files)
- Anything needing a secret or credential
- Changing the shape of `data.json` without changing `ui/` in the same PR
