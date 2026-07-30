# scripts

helper scripts for the stack. all of them are read-only reports — none change
container state. paths below are relative to the repo root, but each script
resolves the repo root itself, so it can be run from anywhere.

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
