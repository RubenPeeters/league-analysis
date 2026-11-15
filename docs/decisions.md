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
