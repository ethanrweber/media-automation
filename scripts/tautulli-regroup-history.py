#!/usr/bin/env python3
"""Recompute Tautulli's session_history.reference_id grouping, keyed on guid.

Tautulli decides whether a play continues an earlier one by looking for the
previous play of the same rating_key by the same user, with no time bound
(plexpy/activity_processor.py, group_history). rating_key is server-local, so
after a server migration the imported history's keys collide with keys the new
server handed to unrelated items, and a new play can be grouped onto a play of
completely different content from the old server. The grouped history view then
shows the reference row's title while the row's own link resolves to the real
item -- e.g. Apocalypse Hotel S1E2 displayed as Adventure Time S8E7.

Tautulli's own "Regroup play history" cannot fix this: it replays the same
rating_key match and re-derives the same wrong grouping.

This rebuilds the grouping on m.guid (plex://episode/... ), which identifies
content independently of which server indexed it. local:// guids are the
exception -- Plex numbers those per server, so they are scoped to the rating
key as well rather than trusted as identity. Two deliberate differences from
Tautulli:

  * matches on (user_id, guid) instead of (user_id, rating_key)
  * judges "was the previous play finished?" using that play's own duration and
    its own credits markers. Tautulli measures the previous row's view_offset
    against the *new* row's duration, which is what let a fully-watched 11-min
    Adventure Time episode read as 50% of a 23-min Apocalypse Hotel episode and
    so look like an unfinished play being resumed.

Otherwise the resume test mirrors Tautulli's, including reading
movie/tv/music_watched_percent and watched_marker from its config.ini.

Reports what would change and exits without writing unless --apply is given.

Usage:
  tautulli-regroup-history.py [--apply] [options]

Options:
  --apply            write the new reference_ids (default: report only)
  --force            with --apply, proceed even if the container is running
  --verbose          list every changed row, not just mis-grouped ones
  --json             emit the full change set as json
"""

import argparse
import configparser
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

WATCHED_PERCENT_KEYS = {
    "movie": "movie_watched_percent",
    "episode": "tv_watched_percent",
    "track": "music_watched_percent",
    "clip": "tv_watched_percent",
}

ROWS_QUERY = """
SELECT h.id, h.reference_id, h.user_id, h.user, h.view_offset, h.media_type,
       h.started, h.rating_key,
       m.guid, m.duration, m.marker_credits_first, m.marker_credits_final,
       m.grandparent_title, m.parent_media_index, m.media_index, m.title, m.live
FROM session_history h
JOIN session_history_metadata m ON m.id = h.id
ORDER BY h.started, h.id
"""


def config_root(repo_root):
    for line in open(os.path.join(repo_root, ".env")):
        if line.startswith("CONFIG_ROOT="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"error: CONFIG_ROOT not found in {repo_root}/.env")


def read_settings(config_ini):
    """Pull the watched-threshold settings out of Tautulli's config.ini."""
    if not os.path.exists(config_ini):
        # configparser.read silently ignores a missing file, which would
        # regroup with default thresholds instead of the configured ones.
        sys.exit(f"error: {config_ini} not found")
    parser = configparser.ConfigParser()
    parser.read(config_ini)
    section = "Monitoring"
    get = lambda key, default: parser.getint(section, key, fallback=default)
    return {
        "movie_watched_percent": get("movie_watched_percent", 85),
        "tv_watched_percent": get("tv_watched_percent", 85),
        "music_watched_percent": get("music_watched_percent", 85),
        "watched_marker": get("watched_marker", 0),
    }


def check_watched(row, settings):
    """Mirror of helpers.check_watched, using the row's own duration."""
    key = WATCHED_PERCENT_KEYS.get(row["media_type"])
    percent = settings[key] if key else 0
    threshold = percent / 100 * (row["duration"] or 0)
    if not threshold:
        return False

    marker = settings["watched_marker"]
    first, final = row["marker_credits_first"], row["marker_credits_final"]
    offset = row["view_offset"] or 0
    if marker == 1 and final:
        return offset >= final
    if marker == 2 and first:
        return offset >= first
    if marker == 3 and first:
        return offset >= min(threshold, first)
    return offset >= threshold


def label(row):
    if row["grandparent_title"]:
        return (f"{row['grandparent_title']} "
                f"S{row['parent_media_index']}E{row['media_index']}")
    return row["title"] or "(unknown)"


def regroup(rows, settings):
    """Assign each row a reference_id, resolving continuations by guid.

    Rows are walked in start-time order — "continues an earlier play" is a
    statement about time, and row ids stop reflecting chronology once history
    has been imported from another server (imports are appended last).
    """
    rows = sorted(rows, key=lambda r: (r["started"], r["id"]))
    previous = {}  # (user_id, guid) -> the last row seen for that item
    changes = []
    for row in rows:
        row = dict(row)
        # Kept because reference_id below is overwritten with the resolved
        # group, and the report needs to say what the row used to point at.
        row["old_reference_id"] = row["reference_id"]
        key = (row["user_id"], row["guid"])
        # local:// guids are Plex's per-server placeholder for unmatched
        # media, so the same string can name different content on history
        # imported from another server; a missing guid identifies nothing at
        # all. Scope both to the rating key too: plays of one item still
        # group, coincidental (or absent) strings cannot.
        if not row["guid"] or row["guid"].startswith("local://"):
            key = key + (row["rating_key"],)
        prev = previous.get(key)
        if prev is not None and not check_watched(prev, settings) and (
            (prev["view_offset"] or 0) <= (row["view_offset"] or 0)
        ):
            new_ref = prev["reference_id"]
        else:
            new_ref = row["id"]

        if new_ref != row["old_reference_id"]:
            changes.append((row, new_ref))
        # Store the resolved reference_id so followers inherit this group.
        row["reference_id"] = new_ref
        previous[key] = row
    return changes


def classify(change, by_id):
    """Describe a reference_id change in terms of what the UI was showing."""
    row, _new_ref = change
    old_ref = row["old_reference_id"]
    old = by_id.get(old_ref)
    if old_ref != row["id"] and old and old["guid"] != row["guid"]:
        return "mismatch"
    if old_ref == row["id"]:
        return "newly-grouped"
    return "regrouped"


def container_running():
    """True/False if docker can tell us, None if it can't."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "tautulli"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() == "true"


def backup(db_path):
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = os.path.join(
        os.path.dirname(db_path), "backups", f"tautulli.backup-{stamp}.regroup.db"
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # sqlite's own backup API, so the copy is consistent even mid-write.
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dest


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tautulli_dir = os.path.join(config_root(repo_root), "Tautulli/Config")
    db_path = os.path.join(tautulli_dir, "tautulli.db")
    if not os.path.exists(db_path):
        sys.exit(f"error: {db_path} not found")

    settings = read_settings(os.path.join(tautulli_dir, "config.ini"))
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(ROWS_QUERY).fetchall()
    by_id = {r["id"]: r for r in rows}

    live = sum(1 for r in rows if r["live"])
    if live:
        # Tautulli groups live TV on a different rule (same user within a day,
        # matching guid); this script does not model it.
        print(f"warning: {live} live-tv rows present, which this script "
              f"regroups by the recorded-media rule", file=sys.stderr)

    changes = regroup(rows, settings)
    buckets = {"mismatch": [], "newly-grouped": [], "regrouped": []}
    for change in changes:
        buckets[classify(change, by_id)].append(change)

    if args.json:
        print(json.dumps([
            {
                "id": row["id"],
                "user": row["user"],
                "watched": label(row),
                "date": datetime.fromtimestamp(row["started"]).strftime("%Y-%m-%d"),
                "rating_key": row["rating_key"],
                "old_reference_id": row["old_reference_id"],
                "new_reference_id": new_ref,
                "was_displayed_as": label(by_id[row["old_reference_id"]])
                if row["old_reference_id"] != row["id"]
                and row["old_reference_id"] in by_id else None,
                "kind": classify((row, new_ref), by_id),
            }
            for row, new_ref in changes
        ], indent=2))
    else:
        print(f"{len(rows)} history rows, {len(changes)} reference_id changes\n")
        print(f"  {len(buckets['mismatch']):>4} grouped onto different content "
              f"(the visible bug)")
        print(f"  {len(buckets['newly-grouped']):>4} newly grouped onto the same "
              f"content (resumes split by the migration)")
        print(f"  {len(buckets['regrouped']):>4} moved to a different group of "
              f"the same content")

        shown = changes if args.verbose else buckets["mismatch"]
        if shown:
            print(f"\n{'id':<7}{'date':<12}{'user':<14}{'watched':<34}"
                  f"was displayed as")
            print("-" * 100)
            for row, new_ref in shown:
                old_ref = row["old_reference_id"]
                old = by_id.get(old_ref)
                displayed = label(old) if old and old_ref != row["id"] else "-"
                date = datetime.fromtimestamp(row["started"]).strftime("%Y-%m-%d")
                print(f"{row['id']:<7}{date:<12}{row['user'][:13]:<14}"
                      f"{label(row)[:33]:<34}{displayed}")

    # Status lines go to stderr under --json so stdout stays parseable.
    def notice(message):
        print(message, file=sys.stderr if args.json else sys.stdout)

    if not changes:
        notice("\nnothing to do")
        return
    if not args.apply:
        notice(f"\ndry run — no changes written. re-run with --apply to write "
               f"{len(changes)} reference_ids.")
        return

    running = container_running()
    if running is not False and not args.force:
        state = "running" if running else "in an unknown state (docker unavailable)"
        sys.exit(f"error: the tautulli container is {state}. stop it first "
                 f"(docker compose stop tautulli) so it cannot write history "
                 f"mid-update, or pass --force to override.")

    dest = backup(db_path)
    notice(f"\nbacked up to {dest}")
    write = sqlite3.connect(db_path)
    try:
        with write:
            write.executemany(
                "UPDATE session_history SET reference_id = ? WHERE id = ?",
                [(new_ref, row["id"]) for row, new_ref in changes],
            )
    finally:
        write.close()
    notice(f"updated {len(changes)} rows. restart tautulli to pick up the change.")


if __name__ == "__main__":
    main()
