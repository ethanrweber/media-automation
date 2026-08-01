# scripts

helper scripts for the stack. paths below are relative to the repo root, but
each script resolves the repo root itself, so it can be run from anywhere.

everything here is a read-only report except the two `tautulli-*.py` repair
scripts, which can write to tautulli's database — both report and exit unless
`--apply` is passed.

## listing ports

to see which ports are in use across all services:

```
bash scripts/list-ports.sh
```

## season audit

read-only report of sonarr seasons that were downloaded episode-by-episode (mixed release groups, codecs, qualities, import dates spread over weeks) and are good candidates for replacing with a season pack via an interactive season search:

```
bash scripts/season-audit.sh
```

the wrapper resolves the ts-sonarr container ip and reads the sonarr api key from `${CONFIG_ROOT}/Sonarr/Config/config.xml`, so no configuration is needed. arguments pass through to the underlying python script:

```
bash scripts/season-audit.sh --min-score 20   # only the worst offenders
bash scripts/season-audit.sh --json           # full detail (groups, codecs, audio) as json
bash scripts/season-audit.sh --all            # include seasons that look like packs
```

the `MultiAud` column counts files with 2+ audio languages — useful for spotting anime seasons missing dual audio. seasons with missing episodes, specials, unmonitored seasons, and currently-airing seasons are excluded.

## watch history

tautulli's web ui lists individual plays but can't group them, so this rolls episode plays up per series:

```
python3 scripts/watch-history.py --user <name>
```

it reads `${CONFIG_ROOT}/Tautulli/Config/tautulli.db` directly, opened read-only so it is safe to run while the container is live. omit `--user` for all users.

```
python3 scripts/watch-history.py --users                        # list usernames in the history
python3 scripts/watch-history.py --user me --sort hours --top 20
python3 scripts/watch-history.py --user me --since 2026-01-01
python3 scripts/watch-history.py --user me --movies             # group movies instead of episodes
python3 scripts/watch-history.py --user me --json
```

`plays` counts every play event (a rewatched or resumed episode counts more than once); `eps` counts distinct episodes. `hours` is play time minus time paused.

tautulli stores the title as it was at play time, so a show renamed upstream, or removed and re-added to the library, would otherwise split across several rows. each rating key is resolved to its most recent title, and titles differing only in case are folded onto the newest spelling, so a renamed show reports under the title it goes by now.

## regrouping watch history after a server migration

fixes history rows that display the wrong show — the grouped history list shows one title while clicking the row opens a different one (e.g. Apocalypse Hotel S1E2 listed as Adventure Time S8E7).

the cause is that plex `rating_key`s are server-local. tautulli decides whether a play continues an earlier one by finding the previous play of the same rating key by the same user, with no time limit, so once history is imported from an old server the new server's keys collide with the imported ones and a play can be grouped onto unrelated content. tautulli's own "regroup play history" replays that same rating-key match and re-derives the same wrong grouping, so it can't repair it.

this regroups on `guid` (`plex://episode/...`) instead, which identifies content regardless of which server indexed it:

```
python3 scripts/tautulli-regroup-history.py                 # report what would change
python3 scripts/tautulli-regroup-history.py --verbose       # list every changed row
python3 scripts/tautulli-regroup-history.py --json
python3 scripts/tautulli-regroup-history.py --apply         # write the new reference_ids
```

it reports and exits unless `--apply` is given. with `--apply` it refuses to run while the tautulli container is up (pass `--force` to override) and takes a sqlite-level backup into `${CONFIG_ROOT}/Tautulli/Config/backups/` first. restart tautulli afterwards.

the report splits changes three ways: rows **grouped onto different content** (the visible bug), rows **newly grouped** onto the same content (a resume that the migration's key change had split into two entries), and rows **moved to a different group** of the same content.

this fixes the stored grouping but not the cause: tautulli itself still groups new plays by rating key, so while colliding keys remain in the database the bug can recur. `tautulli-remap-rating-keys.py` below removes the collisions themselves; run it after this one.

## remapping rating keys after a server migration

the root-cause companion to the regroup script. plex rating keys are server-local, so history imported from an old server holds keys that the current server hands out to unrelated content — every lookup tautulli does by rating key (click-throughs, play grouping, library filtering) can land on the wrong item, and new plays can collide with imported rows forever.

this rewrites the keys on imported rows (identified by a section_id that no longer exists on the current server) to refer to the current plex server, resolving each row in three tiers:

- **remap** — the row's guid exists in the current library: all keys and the section id are pointed at the current item. history becomes fully correct, click-throughs included.
- **fallback** — the guid is gone but the item is still findable: episodes by (show, season, episode) with the show anchored via a sibling episode's guid or exact title, accepted only when the episode title also matches; movies by exact title + year. the title check stops a re-matched show with different ordering from linking silently to the wrong content.
- **shift** — content no longer in the library: keys are moved up by 100,000,000 so they can never collide with a real key. titles and stats stay intact; the click-through stays dead, which it already was.

native rows are untouched except to heal ones whose item was deleted and re-added since the play. `local://` guids are never trusted as identity — plex numbers them per server, so an imported one matching a current item is coincidence, not the same content. rows of one imported item always share a verdict (matched by guid within their old section), so differing metadata snapshots can't send one play to the live item and its sibling to the dead range. limits: only movie and show libraries are fetched, so imported music/photo history can only shift; live-tv rows are left alone entirely.

```
python3 scripts/tautulli-remap-rating-keys.py               # report what would change
python3 scripts/tautulli-remap-rating-keys.py --verbose     # list every row
python3 scripts/tautulli-remap-rating-keys.py --json
python3 scripts/tautulli-remap-rating-keys.py --apply       # write the new keys
```

same safety rails as the regroup script: dry run by default, refuses to `--apply` while the container runs (`--force` overrides), sqlite-level backup into `backups/` first, and re-running after an apply is a no-op. needs the plex server reachable (address and token are read from tautulli's `config.ini`). stale keys also remain in `recently_added`, which tautulli only consults to dedupe notifications for newly added items — harmless, so it is left alone.
