#!/usr/bin/env python3
"""Summarize Tautulli watch history, grouped by show (or movie).

Tautulli's web UI lists individual plays but can't group them, so this reads
its SQLite database directly and rolls episode plays up per series.

Read-only: opens tautulli.db with mode=ro, so it is safe to run while the
container is live.

Usage:
  watch-history.py [--user NAME] [options]

Options:
  --user NAME        only this Tautulli username (default: all users)
  --movies           group movies instead of TV episodes
  --since DATE       only plays on/after DATE (YYYY-MM-DD)
  --sort FIELD       plays (default), episodes, hours, or last
  --top N            only show the top N rows
  --users            list usernames in the history and exit
  --json             emit JSON instead of a table
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

# Tautulli counts a play's duration as stopped - started, minus time paused.
# stopped is NULL for a session that never ended cleanly, so fall back to
# started (a zero-length play) rather than dropping the row.
DURATION = "MAX(IFNULL(h.stopped, h.started) - h.started - IFNULL(h.paused_counter, 0), 0)"

# Tautulli snapshots the title as it was at play time, so a show renamed
# upstream lands under several titles (Pluribus -> Plur1bus -> PLUR1BUS).
# Grouping by rating key alone is worse: a series removed and re-added gets a
# new key, which splits it — and that is the common case here, not the rename.
# So group in two stages:
#
#   canonical  each rating key -> its most recently seen title, collapsing
#              renames onto the title the show goes by now.
#   folded     titles differing only in case -> the newest spelling, which
#              catches a show that was both re-added and recased under keys
#              whose canonical titles no longer match exactly.
#
# Both stages run over all history rather than the filtered subset, so
# --user/--since can't change how a title resolves. LOWER() only folds ASCII,
# same as SQLite's NOCASE; titles differing by non-ASCII case stay split.
QUERY = f"""
WITH ranked AS (
    SELECT {{key_col}} AS key,
           {{title_col}} AS title,
           h.started,
           h.id,
           ROW_NUMBER() OVER (
               PARTITION BY {{key_col}} ORDER BY h.started DESC, h.id DESC
           ) AS rn
    FROM session_history h
    JOIN session_history_metadata m ON m.id = h.id
    WHERE h.media_type = ?
),
canonical AS (
    SELECT key, title, started, id FROM ranked WHERE rn = 1
),
recased AS (
    SELECT title,
           LOWER(title) AS folded_title,
           ROW_NUMBER() OVER (
               PARTITION BY LOWER(title) ORDER BY started DESC, id DESC
           ) AS rn
    FROM canonical
),
folded AS (
    SELECT folded_title, title FROM recased WHERE rn = 1
),
matched AS (
    SELECT COALESCE(f.title, c.title, {{title_col}}) AS name,
           h.rating_key,
           h.started,
           {DURATION} AS seconds
    FROM session_history h
    JOIN session_history_metadata m ON m.id = h.id
    LEFT JOIN canonical c ON c.key = {{key_col}}
    LEFT JOIN folded f
           ON f.folded_title = LOWER(COALESCE(c.title, {{title_col}}))
    WHERE h.media_type = ?
      {{user_clause}}
      {{since_clause}}
)
SELECT name,
       COUNT(*) AS plays,
       COUNT(DISTINCT rating_key) AS items,
       SUM(seconds) AS seconds,
       MIN(started) AS first_played,
       MAX(started) AS last_played
FROM matched
GROUP BY name
"""

SORT_KEYS = {
    "plays": lambda r: -r["plays"],
    "episodes": lambda r: -r["items"],
    "hours": lambda r: -r["seconds"],
    "last": lambda r: -r["last_played"],
}


def config_root(repo_root):
    for line in open(os.path.join(repo_root, ".env")):
        if line.startswith("CONFIG_ROOT="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"error: CONFIG_ROOT not found in {repo_root}/.env")


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--user")
    parser.add_argument("--movies", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--sort", choices=sorted(SORT_KEYS), default="plays")
    parser.add_argument("--top", type=int)
    parser.add_argument("--users", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(config_root(repo_root), "Tautulli/Config/tautulli.db")
    if not os.path.exists(db_path):
        sys.exit(f"error: {db_path} not found")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    if args.users:
        rows = db.execute(
            "SELECT user, COUNT(*) n FROM session_history GROUP BY user ORDER BY n DESC"
        ).fetchall()
        for row in rows:
            print(f"{row['user']:<25}{row['n']:>7} plays")
        return

    media_type = "movie" if args.movies else "episode"
    # media_type is bound twice: once for the canonical-title CTE, once for the
    # filtered rows.
    params = [media_type, media_type]
    user_clause = ""
    if args.user:
        # NOCASE so the username doesn't have to match Plex's capitalization.
        user_clause = "AND h.user = ? COLLATE NOCASE"
        params.append(args.user)
    since_clause = ""
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            sys.exit("error: --since must be YYYY-MM-DD")
        since_clause = "AND h.started >= ?"
        params.append(int(since.timestamp()))

    # Movies group on themselves; episodes group on the series (grandparent).
    title_col = "m.title" if args.movies else "m.grandparent_title"
    key_col = "m.rating_key" if args.movies else "m.grandparent_rating_key"
    query = QUERY.format(
        title_col=title_col,
        key_col=key_col,
        user_clause=user_clause,
        since_clause=since_clause,
    )
    rows = [dict(r) for r in db.execute(query, params)]
    if args.user and not rows:
        sys.exit(f"error: no {media_type} history for user '{args.user}' "
                 f"(try --users to list usernames)")

    rows.sort(key=SORT_KEYS[args.sort])
    if args.top:
        rows = rows[: args.top]

    for row in rows:
        row["hours"] = round(row.pop("seconds") / 3600, 1)
        for key in ("first_played", "last_played"):
            row[key] = datetime.fromtimestamp(row[key]).strftime("%Y-%m-%d")

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    label = "movie" if args.movies else "show"
    count_label = "uniq" if args.movies else "eps"
    print(f"{label:<45}{'plays':>6}{count_label:>6}{'hours':>7}  {'first':<12}last")
    print("-" * 92)
    for row in rows:
        print(
            f"{(row['name'] or '(unknown)')[:44]:<45}{row['plays']:>6}"
            f"{row['items']:>6}{row['hours']:>7.1f}  "
            f"{row['first_played']:<12}{row['last_played']}"
        )
    print("-" * 92)
    print(
        f"{len(rows)} {label}s, {sum(r['plays'] for r in rows)} plays, "
        f"{sum(r['hours'] for r in rows):.1f} hours"
    )


if __name__ == "__main__":
    main()
