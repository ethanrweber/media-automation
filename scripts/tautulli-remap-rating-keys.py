#!/usr/bin/env python3
"""Remap imported Tautulli history onto the current Plex server's rating keys.

Plex rating keys are server-local integers, so history imported from another
server carries keys from a numbering that the current server reuses for
unrelated content. Tautulli looks items up by rating key (history click-
throughs, and its session-continuation grouping), so imported rows point at
the wrong items and new plays can collide with old ones.

This rewrites the keys on imported history rows (identified by a section_id
that does not exist on the current server) so they refer to the current
server, in three tiers:

  remap    the row's guid (plex://...) exists in the current library: point
           rating_key / parent_rating_key / grandparent_rating_key / section_id
           at the current item. Old history becomes fully correct, including
           click-throughs.
  fallback the guid is gone but the item is still findable: episodes matched
           by (show, season, episode) with the show anchored via a sibling
           episode's guid or by exact title, accepted only if the episode
           title also matches; movies matched by exact title + year. The
           title check guards against silently linking to the wrong content
           when a show was re-matched with a different ordering. Keys and
           section_id are updated just like a guid remap.
  shift    content no longer in the library: add OFFSET (100,000,000) to the
           keys so they can never collide with a real key. These rows keep
           correct titles and stay in history; only their click-through is
           dead, which it already was.

Rows native to this server are normally untouched, with one exception: a
native row whose guid resolves to a different key (its item was deleted and
re-added since the play) is remapped the same way, healing the split.

Run scripts/tautulli-regroup-history.py first if grouped history displays the
wrong titles; this script does not change grouping. After the remap, new plays
group correctly against old history because both use current keys.

Reads the Plex address and token from Tautulli's config.ini. Reports what
would change and exits without writing unless --apply is given.

Usage:
  tautulli-remap-rating-keys.py [--apply] [options]

Options:
  --apply            write the changes (default: report only)
  --force            with --apply, proceed even if the container is running
  --verbose          list every changed row, not just a summary
  --json             emit the full change set as json
"""

import argparse
import configparser
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

OFFSET = 100_000_000

ROWS_QUERY = """
SELECT h.id, h.section_id, h.media_type, h.rating_key, h.parent_rating_key,
       h.grandparent_rating_key, h.user, h.started,
       m.guid, m.grandparent_title, m.parent_media_index, m.media_index,
       m.title, m.year, m.live
FROM session_history h
JOIN session_history_metadata m ON m.id = h.id
ORDER BY h.id
"""


def config_root(repo_root):
    for line in open(os.path.join(repo_root, ".env")):
        if line.startswith("CONFIG_ROOT="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"error: CONFIG_ROOT not found in {repo_root}/.env")


def norm(text):
    return (text or "").strip().casefold()


def intish(value):
    return str(value).isdigit()


def fetch_plex(config_ini):
    """Pull the current library from Plex: guid map, episode index, titles."""
    parser = configparser.ConfigParser()
    parser.read(config_ini)
    token = parser.get("PMS", "pms_token", fallback="")
    ip = parser.get("PMS", "pms_ip", fallback="")
    port = parser.get("PMS", "pms_port", fallback="32400")
    if not token or not ip:
        sys.exit("error: could not read pms_ip/pms_token from config.ini")
    ctx = ssl._create_unverified_context()

    def fetch(base, path):
        req = urllib.request.Request(base + path, headers={"X-Plex-Token": token})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            return ET.parse(resp).getroot()

    # Plex may have "secure connections" set to preferred (http works) or
    # required (https only, certificate won't match a bare IP — hence the
    # unverified context; the LAN address comes from Tautulli's own config).
    base = None
    for candidate in (f"http://{ip}:{port}", f"https://{ip}:{port}"):
        try:
            fetch(candidate, "/identity")
            base = candidate
            break
        except (urllib.error.URLError, OSError):
            continue
    if base is None:
        sys.exit(f"error: Plex unreachable at {ip}:{port} over http or https")

    def get(path):
        try:
            return fetch(base, path)
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"error: Plex request {path} failed: {e}")

    guidmap = {}      # guid -> {rk, prk, grk, sec}
    epindex = {}      # (show rk, season idx, ep idx) -> {rk, prk, title}
    show_titles = {}  # normalized show title -> show rk
    movie_titles = {} # (normalized title, year) -> movie rk, ambiguous -> None
    sections = set()

    for directory in get("/library/sections"):
        sec, sectype = directory.get("key"), directory.get("type")
        sections.add(int(sec))
        if sectype == "movie":
            for it in get(f"/library/sections/{sec}/all?type=1"):
                guid, rk = it.get("guid"), it.get("ratingKey")
                if not (guid and rk):
                    continue
                # local:// guids are Plex's placeholder for unmatched media
                # and are numbered per-server, so the same string on two
                # servers is unrelated content. Never treat them as identity;
                # such items are only reachable via the fallback matches.
                if not guid.startswith("local://"):
                    guidmap[guid] = {"rk": int(rk), "prk": None, "grk": None,
                                     "sec": int(sec)}
                year = it.get("year")
                key = (norm(it.get("title")), int(year) if year else None)
                movie_titles[key] = (None if key in movie_titles
                                     else {"rk": int(rk), "sec": int(sec)})
        elif sectype == "show":
            for it in get(f"/library/sections/{sec}/all?type=2"):
                if it.get("title") and it.get("ratingKey"):
                    key = norm(it.get("title"))
                    # Two shows with the same title (remakes, split listings)
                    # make a title anchor ambiguous — poison it rather than
                    # letting last-wins pick one silently.
                    show_titles[key] = (None if key in show_titles
                                        else int(it.get("ratingKey")))
            for it in get(f"/library/sections/{sec}/all?type=4"):
                guid, rk = it.get("guid"), it.get("ratingKey")
                prk, grk = it.get("parentRatingKey"), it.get("grandparentRatingKey")
                if not (guid and rk):
                    continue
                # Skip local:// (see the movie branch) and episodes missing
                # parent keys — a guid remap that can only fix the episode key
                # would leave old colliding season/show keys behind, so let
                # those rows take the fallback or shift path instead.
                if not guid.startswith("local://") and prk and grk:
                    guidmap[guid] = {"rk": int(rk), "prk": int(prk),
                                     "grk": int(grk), "sec": int(sec)}
                if grk and it.get("parentIndex") and it.get("index"):
                    epindex[(int(grk), int(it.get("parentIndex")),
                             int(it.get("index")))] = {
                        "rk": int(rk), "prk": int(prk) if prk else None,
                        "title": it.get("title") or "", "sec": int(sec),
                    }
    return {"guidmap": guidmap, "epindex": epindex, "show_titles": show_titles,
            "movie_titles": movie_titles, "sections": sections}


def plan_changes(rows, plex, imported_sections):
    """Decide the new keys for every row. Pure; returns a list of changes.

    Each change: {id, action, rating_key, parent_rating_key,
    grandparent_rating_key, section_id} where the key fields are the new
    values (None = leave the stored value alone).
    """
    guidmap, epindex = plex["guidmap"], plex["epindex"]

    def guid_hit(row):
        # local:// guids are per-server placeholders, not identity (see
        # fetch_plex); an imported one matching a current item is coincidence.
        if (row["guid"] or "").startswith("local://"):
            return None
        return guidmap.get(row["guid"])

    def as_int(value):
        # Index/year columns are TEXT in places; a stray '5' must still match
        # the int-keyed Plex lookups rather than silently failing to a shift.
        return int(value) if intish(value) else None

    # Shows resolved via a sibling episode whose guid still exists; used to
    # anchor the (show, season, episode) fallback for episodes whose own guid
    # changed (e.g. the show was re-matched with a different agent/ordering).
    # Keyed by (section_id, grandparent key): grandparent keys are only
    # meaningful within the server that issued them, and each dead section's
    # rows came from one server — scoping by section keeps namespaces apart
    # even if a current library is someday deleted and its rows turn
    # "imported", so a numeric coincidence can never anchor the wrong show.
    sibling_show = {}
    for row in rows:
        if row["section_id"] not in imported_sections or row.get("live"):
            continue
        hit = guid_hit(row)
        if hit and hit["grk"] and row["media_type"] == "episode" and intish(
                row["grandparent_rating_key"]):
            sibling_show.setdefault(
                (row["section_id"], int(row["grandparent_rating_key"])),
                hit["grk"])

    def resolve(row, imported):
        hit = guid_hit(row)
        if hit:
            return hit, "remap"
        if imported and row["media_type"] == "episode":
            # Two candidate anchors for the show; accept whichever has this
            # (season, episode) with a matching, non-empty title. A wrong
            # anchor (a mis-matched show in either library) fails the check.
            candidates = []
            if intish(row["grandparent_rating_key"]):
                candidates.append(sibling_show.get(
                    (row["section_id"], int(row["grandparent_rating_key"]))))
            candidates.append(
                plex["show_titles"].get(norm(row["grandparent_title"])))
            for show in candidates:
                ep = epindex.get((show, as_int(row["parent_media_index"]),
                                  as_int(row["media_index"]))) if show else None
                if ep and norm(row["title"]) and (
                        norm(ep["title"]) == norm(row["title"])):
                    return {"rk": ep["rk"], "prk": ep["prk"], "grk": show,
                            "sec": ep["sec"]}, "fallback"
        if imported and row["media_type"] == "movie":
            movie = plex["movie_titles"].get(
                (norm(row["title"]), as_int(row["year"])))
            if movie:
                return {"rk": movie["rk"], "prk": None, "grk": None,
                        "sec": movie["sec"]}, "fallback"
        return None, None

    # First pass: resolve each row on its own. Remember the verdict per
    # imported (section, guid) so plays of one old item always land together —
    # metadata snapshots of the same item can differ (retitled episodes), and
    # without this one snapshot could remap while its sibling gets shifted.
    resolved = []
    guid_targets = {}
    for row in rows:
        if row.get("live"):
            # Live-TV rows reference broadcasts, not library items; healing
            # one onto a library episode would rewrite a live play into a
            # library play. Leave them alone entirely.
            resolved.append((None, None))
            continue
        imported = row["section_id"] in imported_sections
        target, action = resolve(row, imported)
        resolved.append((target, action))
        if imported and target and row["guid"]:
            guid_targets.setdefault(
                (row["section_id"], row["guid"]), (target, action))

    changes = []
    for row, (target, action) in zip(rows, resolved):
        if row.get("live"):
            continue
        imported = row["section_id"] in imported_sections
        if target is None and imported and row["guid"]:
            adopted = guid_targets.get((row["section_id"], row["guid"]))
            if adopted:
                target, action = adopted

        if target:
            new = {"id": row["id"], "action": action,
                   "rating_key": target["rk"],
                   "parent_rating_key": target["prk"],
                   "grandparent_rating_key": target["grk"],
                   "section_id": target["sec"]}
        elif imported:
            # Content gone: push the keys out of the live range, once.
            shifted = {}
            for field in ("rating_key", "parent_rating_key",
                          "grandparent_rating_key"):
                value = row[field]
                if intish(value) and int(value) < OFFSET:
                    shifted[field] = int(value) + OFFSET
            if not shifted:
                continue
            new = {"id": row["id"], "action": "shift", "section_id": None,
                   "rating_key": shifted.get("rating_key"),
                   "parent_rating_key": shifted.get("parent_rating_key"),
                   "grandparent_rating_key": shifted.get("grandparent_rating_key")}
        else:
            continue

        same = all(
            new[field] is None or (
                intish(row[field]) and int(row[field]) == new[field])
            for field in ("rating_key", "parent_rating_key",
                          "grandparent_rating_key")
        ) and (new["section_id"] is None
               or new["section_id"] == row["section_id"])
        if not same:
            changes.append(new)
    return changes


def container_running():
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
        os.path.dirname(db_path), "backups", f"tautulli.backup-{stamp}.remap.db"
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dest


def apply_changes(db_path, changes):
    db = sqlite3.connect(db_path)
    try:
        with db:
            for c in changes:
                sets, args = [], []
                for field in ("rating_key", "parent_rating_key",
                              "grandparent_rating_key"):
                    if c[field] is not None:
                        sets.append(f"{field} = ?")
                        args.append(c[field])
                if c["section_id"] is not None:
                    db.execute(
                        "UPDATE session_history SET section_id = ? WHERE id = ?",
                        (c["section_id"], c["id"]))
                for table in ("session_history", "session_history_metadata"):
                    db.execute(
                        f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?",
                        args + [c["id"]])
                if c["rating_key"] is not None:
                    db.execute(
                        "UPDATE session_history_media_info SET rating_key = ? "
                        "WHERE id = ?", (c["rating_key"], c["id"]))
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def notice(message):
        print(message, file=sys.stderr if args.json else sys.stdout)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tautulli_dir = os.path.join(config_root(repo_root), "Tautulli/Config")
    db_path = os.path.join(tautulli_dir, "tautulli.db")
    if not os.path.exists(db_path):
        sys.exit(f"error: {db_path} not found")

    plex = fetch_plex(os.path.join(tautulli_dir, "config.ini"))
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(ROWS_QUERY)]
    db.close()

    history_sections = {r["section_id"] for r in rows}
    imported_sections = history_sections - plex["sections"]
    notice(f"plex library: {len(plex['guidmap'])} items in sections "
           f"{sorted(plex['sections'])}")
    notice(f"history sections {sorted(history_sections)} -> imported rows are "
           f"sections {sorted(imported_sections)}")

    changes = plan_changes(rows, plex, imported_sections)
    by_id = {r["id"]: r for r in rows}
    buckets = {"remap": [], "fallback": [], "shift": []}
    for c in changes:
        buckets[c["action"]].append(c)
    native_healed = [c for c in buckets["remap"]
                     if by_id[c["id"]]["section_id"] not in imported_sections]
    leftover = sum(
        1 for r in rows
        if r["section_id"] not in imported_sections
        and r["guid"] not in plex["guidmap"]
    )

    if args.json:
        print(json.dumps([
            dict(c, user=by_id[c["id"]]["user"],
                 title=by_id[c["id"]]["grandparent_title"]
                 or by_id[c["id"]]["title"],
                 old_rating_key=by_id[c["id"]]["rating_key"])
            for c in changes
        ], indent=2))
    else:
        print(f"\n{len(rows)} history rows, {len(changes)} to change:\n")
        print(f"  {len(buckets['remap']):>5} remapped by guid "
              f"({len(native_healed)} of them native rows healed after a "
              f"delete/re-add)")
        print(f"  {len(buckets['fallback']):>5} remapped by fallback match "
              f"(episode S/E + title, or movie title + year)")
        print(f"  {len(buckets['shift']):>5} shifted by {OFFSET:,} "
              f"(content gone; keys can never collide again)")
        print(f"  {leftover:>5} native rows with deleted content left as-is")
        if args.verbose:
            print(f"\n{'id':<7}{'action':<10}{'user':<14}{'title':<40}"
                  f"{'old rk':>8}  new rk")
            print("-" * 92)
            for c in changes:
                r = by_id[c["id"]]
                print(f"{c['id']:<7}{c['action']:<10}{r['user'][:13]:<14}"
                      f"{(r['grandparent_title'] or r['title'] or '')[:39]:<40}"
                      f"{r['rating_key']:>8}  {c['rating_key']}")

    if not changes:
        notice("\nnothing to do")
        return
    if not args.apply:
        notice(f"\ndry run — no changes written. re-run with --apply to "
               f"update {len(changes)} rows.")
        return

    running = container_running()
    if running is not False and not args.force:
        state = "running" if running else "in an unknown state (docker unavailable)"
        sys.exit(f"error: the tautulli container is {state}. stop it first "
                 f"(docker compose stop tautulli) so it cannot write history "
                 f"mid-update, or pass --force to override.")

    dest = backup(db_path)
    notice(f"\nbacked up to {dest}")
    apply_changes(db_path, changes)
    notice(f"updated {len(changes)} rows across session_history, "
           f"session_history_metadata, and session_history_media_info. "
           f"restart tautulli to pick up the change.")


if __name__ == "__main__":
    main()
