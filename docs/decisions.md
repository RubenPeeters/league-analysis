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

## 2026-11-15: meta over patches page reads history.json as is

`ui/history.html` shows `history.json` as a grid of pick rates per champion and patch. Its rows are every champion that was in the top 15 by pick rate in at least one patch. They are ordered by the number of patches in the top 15, then by the mean pick rate over the patches where the champion is listed. A champion that is not listed shows as an empty cell. The logic lives in `buildView` in `ui/history.js`, which `tests/test_history_page.py` runs through node (skipped if node is missing).
**Why:** backfilled patches only list the top 15, so a missing entry is unknown, not 0, and counting it as 0 would distort both the colours and the order (#12). Testing the JavaScript through node keeps `make check` as the only gate without adding a JS toolchain.

## 2026-11-15: finished items chosen by shop data, not recipe depth

`select_items()` picks build items from the parsed Data Dragon `item.json`. A finished item is on Summoner's Rift (`maps["11"]`), is not a consumable or trinket, has no `into`, and is either purchasable for at least 1000 gold or reached without a purchase: it has a `specialRecipe` (Muramana, Seraph's Embrace, Fimbulwinter) or is built only from items that cannot be bought (the support upgrades from Bounty of Worlds), and is not itself the `specialRecipe` of another item. Boots are a separate set: items tagged `Boots` that are built from something (tier 1 excluded), whatever their `into`. Builds count items from both sets, as before. When Data Dragon cannot be reached there is still no filtering.
**Why:** the old `depth >= 3` rule dropped Infinity Edge and Rabadon's Deathcap, which have depth 2 in 16.22.1 (#20). 1000 gold lies between the dearest starting item (Guardian's items, 950) and the cheapest finished non-boot item (Mejai's Soulstealer, 1500) in 16.22.1. Transformed items count because they are what the end-of-game inventory shows.

## 2026-11-15: item frequencies replace the exact set

Per champion, `data.json` has `items` (finished items other than boots) and `boots` in place of `top_items`. Each lists the item id, the number of games the item was in the final inventory (counted once per game) and that number as a percentage of the champion's games (`count`), at most 8, by share. Every game counts in the denominator, also games with few or no finished items. The detail view shows the share under each item and the game count on hover or tap, with item names from Data Dragon. `context_builds` is unchanged for now.
**Why:** the inventory slot order is not purchase order, so "the first three items" was arbitrary, and an exact set fragments at about 35 games per champion: on 2026-11-15, 44 of the 150 champion rows showed only two items and 98 had boots as one of the three (#21).

## 2026-11-15: matchup cards show item differences, enemy type from champions

The enemy type of a game comes from the enemy champions, not from the damage they dealt in that game. Each champion's physical share is its physical damage to champions divided by its total damage to champions (physical, magic and true), summed over all its stored games in any role and both regions; the magic share likewise. A champion with fewer than 5 stored games has no share and is left out of the team mean. A team is Heavy AD when the mean physical share of its champions is above 65%, otherwise Heavy AP when the mean magic share is above 60%, and Tank Heavy (also) with two or more champions tagged `Tank` in Data Dragon. `matchups` replaces `context_builds` in `data.json`: per enemy type, the items (boots included) whose share in games against that type differs from their share in the champion's other games (same role and region) by at least 20 percentage points, with both shares and both game counts, ordered by the difference. A card needs at least 10 games against the type and 10 other games, and at least one item.
**Why:** on 2026-11-15, 218 of the 313 published cards rested on fewer than 10 games and 103 showed the same build as the default, and the damage dealt describes how the game went, not what the player faced in champion select (#22). A difference against the champion's own other games shows only what changes; 10 games per side keeps one game from moving a share by more than 10 points.

## 2026-11-15: runes, summoner spells and game length stored; older matches downloaded again

`slim_match()` also keeps, per participant, the rune page as `perks` (`primaryStyle`, the four `primary` selections, `subStyle`, the two `secondary` selections and the three stat `shards` in the order offense, flex, defense; `None` when the response has no perks), `summoner1Id`, `summoner2Id` and `gameEndedInEarlySurrender`, and per match `gameDuration`. `gameDuration` is always written, also as `None`, so its presence marks a match slimmed with these fields. After the crawl, every stored current-patch match without it is downloaded again through `smart_request` and replaced, one request per match; a match that cannot be fetched is left as it is and tried again only in a later run. `data.json` is unchanged; the analysis does not read the new fields yet.
**Why:** runes, summoner spells and remakes need these fields, and the 1,065 matches stored on 2026-11-15 do not have them (#23). Downloading them again costs one request per match, once; without it, those views would stay partly empty until the next patch purges the old matches.

## 2026-11-15: runes and summoner spells per champion; remakes left out of builds

Per champion, `data.json` has `rune_games` (games with rune data), `runes` and `spells`. A rune page is the keystone, the primary style and the secondary style; `runes` lists the 3 most frequent pages with games, share of `rune_games` and win rate, and the first page has `selection`: its most frequent full selection (the other three primary runes, the two secondary runes, the three shards) with its game count. `spells` lists the 2 most frequent summoner spell pairs, order ignored, with games and share of `rune_games`. Games that ended in an early surrender (remakes) are left out of items, matchup cards, runes and spells, but still count for pick rate, win rate and KDA; item shares are now over `item_games`, the champion's games that are not remakes. The detail view shows runes and spells with names and icons from Data Dragon; stat shard names are fixed in `ui/script.js` because `runesReforged.json` does not list them.
**Why:** Ruben asked for runes and spells per champion (#24). The minor runes and shards vary much more than the keystone and the two trees, so a page defined by all of them fragments the sample the way exact item sets did (#21); the full selection is shown only for the top page. A remake ends before anything is bought or played out, so it would only dilute the shares. Matches stored before #23 have no rune data, so shares are over the games that have it, and the page shows that number.
