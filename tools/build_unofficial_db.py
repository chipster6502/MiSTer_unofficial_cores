#!/usr/bin/env python3
"""
build_unofficial_db.py — turn the curated registry into a Downloader database.

WHY THIS EXISTS
    Unofficial cores are installed by hand: find the repo, find the .rbf,
    scp it to the right folder. The MiSTer Downloader already solves
    distribution — MiSTer Monitor itself ships through a custom database —
    so unofficial cores should too. This reads tools/unofficial_cores.json
    (the human-approved registry, and ONLY that) and emits
    db/db.json[.zip] in the exact format the Downloader
    consumes. One [section] in downloader.ini on the MiSTer, and every
    update_all run keeps the approved cores current.

WHERE FILES COME FROM (three layouts observed in the wild)
    1. releases/ folder in the repo   (MiSTer-devel convention)
    2. GitHub Releases assets          (z386 does this)
    3. .rbf/.mra sitting in the repo root
    Tried in that order. Per core: the newest .rbf per name-stem
    (Foo_20260101.rbf beats Foo_20250101.rbf) plus every .mra.

EXTRAS AND MANUAL STEPS
    A registry entry may name `extras` — auxiliary assets from the same
    release, each with an explicit destination (boot ROMs, for instance).
    They are never inferred: releases routinely also ship files that must not
    be installed automatically. Destinations are confined to `games/` and the
    unofficial cores tree; see validate_dest. Anything that cannot be
    automated goes in `manual`, free text printed on every build.

WHERE FILES GO on the SD card
    non-arcade   _Unofficial Cores/<file>.rbf
    arcade       _Unofficial Cores/_Arcade/<file>.mra
                 _Unofficial Cores/_Arcade/cores/<file>.rbf
    (an entry counts as arcade if flagged in the registry OR if it ships
    any .mra)

SAFETY PROPERTIES
    - The Downloader DELETES files that disappear from a database. So a
      transient API failure on one repo must not empty its entries: on a
      per-repo failure the previous build's files for that repo are carried
      over unchanged, and the run is flagged.
    - md5 hashes are computed from the actual downloaded bytes and cached
      by content key (git blob sha / release asset id) in
      tools/unofficial_db_cache.json, so unchanged cores cost nothing.
    - Output is only rewritten when something other than the timestamp
      changed; the zip is built deterministically so git diffs stay honest.

USAGE
    GITHUB_TOKEN=... python3 tools/build_unofficial_db.py
    (CI uses github.token; unauthenticated works but the core API allows
    only 60 requests/hour)

EXIT CODES
    0 nothing changed   1 database updated   2 finished with per-repo errors
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile

DB_ID = "chipster6502/MiSTer_unofficial_cores"
DB_URL = ("https://raw.githubusercontent.com/chipster6502/"
          "MiSTer_unofficial_cores/main/db/db.json.zip")
ROOT = "_Unofficial Cores"

DATED_RBF_RE = re.compile(r"_(\d{8})\.rbf$", re.I)


def gh(url, token, raw=False):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MiSTer-Monitor-unofficial-db",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read() if raw else json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < 2:
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(0, int(reset or 0) - time.time()) + 2 if reset else 65
                print(f"    .. 403, sleeping {wait:.0f}s", flush=True)
                time.sleep(min(wait, 300))
                continue
            raise
    raise RuntimeError("gave up after repeated 403s")


def list_artifacts(full_name, token):
    """(files, source) — EVERY file the layout distributes, not just cores.

    Extras (boot ROMs and friends) live alongside the .rbf in the same
    release, so the full listing is returned and the filtering happens later.
    A layout still only 'wins' if it contains at least one .rbf/.mra:
    otherwise a repo root full of source files would shadow the real release.
    `key` identifies the exact content version, for the hash cache."""
    def is_core(name):
        return name.lower().endswith((".rbf", ".mra"))

    # 1. releases/ folder
    try:
        items = gh(f"https://api.github.com/repos/{full_name}/contents/releases",
                   token)
        found = [{"name": i["name"], "url": i["download_url"],
                  "key": i["sha"], "size": i.get("size", 0)}
                 for i in items if i["type"] == "file"]
        if any(is_core(f["name"]) for f in found):
            return found, "releases/ folder"
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    # 2. GitHub Releases assets
    try:
        rel = gh(f"https://api.github.com/repos/{full_name}/releases/latest",
                 token)
        found = [{"name": a["name"], "url": a["browser_download_url"],
                  "key": f"asset:{a['id']}:{a.get('updated_at','')}",
                  "size": a.get("size", 0)}
                 for a in rel.get("assets", [])]
        if any(is_core(f["name"]) for f in found):
            return found, f"release {rel.get('tag_name', '?')}"
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    # 3. repo root
    items = gh(f"https://api.github.com/repos/{full_name}/contents/", token)
    found = [{"name": i["name"], "url": i["download_url"],
              "key": i["sha"], "size": i.get("size", 0)}
             for i in items if i["type"] == "file"]
    return (found, "repo root") if any(is_core(f["name"]) for f in found) \
        else ([], "no layout matched")


def select_artifacts(files):
    """Newest .rbf per stem + every .mra. Anything else is ignored here too —
    the listing layer already filters, but this must hold for ANY source
    (a test fixture proved the point by slipping a .txt through)."""
    rbfs, mras = {}, []
    for f in files:
        n = f["name"].lower()
        if n.endswith(".mra"):
            mras.append(f)
            continue
        if not n.endswith(".rbf"):
            continue
        m = DATED_RBF_RE.search(n)
        stem = DATED_RBF_RE.sub("", n).lower() if m else n.lower()
        date = m.group(1) if m else ""
        cur = rbfs.get(stem)
        if cur is None or date > cur[0]:
            rbfs[stem] = (date, f)
    return [f for _, f in rbfs.values()], mras


def validate_dest(dest):
    """Raise unless `dest` is somewhere this database may own.

    The Downloader deletes what a database stops declaring, so every path here
    is a file this repo can also DELETE from someone's SD card. That makes the
    allowed area small on purpose: a core's own data folder and the unofficial
    cores tree, nothing else.

    The concrete danger is not hypothetical. z386's release ships a patched
    `MiSTer` binary — the main menu executable — next to its .rbf. Installing
    that through a database would (a) let a bad rebuild delete /media/fat/MiSTer
    and leave the machine without a menu, and (b) fight the official database,
    which owns that same file. Patched system binaries stay a deliberate,
    manual act; describe them in the registry's `manual` field instead."""
    if not dest or dest.startswith("/") or "\\" in dest:
        raise ValueError(f"dest must be a relative path with '/': {dest!r}")
    parts = dest.split("/")
    if ".." in parts or "" in parts:
        raise ValueError(f"dest must not traverse or contain empty parts: {dest!r}")
    if parts[-1] in ("MiSTer", "menu.rbf"):
        raise ValueError(f"refusing to manage the system binary {parts[-1]!r} "
                         f"— it belongs to the official database and to you")
    if not (dest.startswith("games/") or dest.startswith(f"{ROOT}/")):
        raise ValueError(f"dest must live under 'games/' or '{ROOT}/': {dest!r}")
    return dest


def load_json(path, default):
    if os.path.isfile(path):
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="tools/unofficial_cores.json")
    ap.add_argument("--cache", default="tools/unofficial_db_cache.json")
    ap.add_argument("--out", default="db")
    ap.add_argument("--allow-empty", action="store_true",
                    help="permit publishing a database with no files; this "
                         "UNINSTALLS everything it owns from every SD card "
                         "following it")
    ap.add_argument("--fixture",
                    help="JSON {full_name: [artifact,...]} replacing the API "
                         "listing — for tests; downloads and hashing stay real")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    registry = load_json(args.registry, {"cores": []})
    cache = load_json(args.cache, {})
    provenance = cache.setdefault("_provenance", {})
    fixture = load_json(args.fixture, {}) if args.fixture else None

    db_path = os.path.join(args.out, "db.json")
    old_db = load_json(db_path, {"files": {}, "folders": {}})

    files, errors = {}, []
    for core in registry.get("cores", []):
        repo = core["repo"]
        try:
            if fixture is not None:
                artifacts, source = fixture.get(repo, []), "fixture"
            else:
                artifacts, source = list_artifacts(repo, token)
            rbfs, mras = select_artifacts(artifacts)
            if not rbfs and not mras:
                raise RuntimeError("no .rbf/.mra found in any known layout")

            arcade = bool(core.get("arcade")) or bool(mras)
            paths = []
            for f in mras:
                paths.append((f"{ROOT}/_Arcade/{f['name']}", f))
            for f in rbfs:
                dest = (f"{ROOT}/_Arcade/cores/{f['name']}" if arcade
                        else f"{ROOT}/{f['name']}")
                paths.append((dest, f))

            # Extras: boot ROMs and similar, named explicitly in the registry.
            # Never inferred — the same release usually also carries things
            # that must NOT be installed automatically (see validate_dest).
            by_name = {f["name"]: f for f in artifacts}
            for extra in core.get("extras", []):
                asset = extra["asset"]
                if asset not in by_name:
                    raise RuntimeError(f"extra asset {asset!r} not in {source}")
                paths.append((validate_dest(extra["dest"]), by_name[asset]))

            for dest, f in paths:
                meta = cache.get(f["key"])
                if not meta:
                    data = gh(f["url"], token, raw=True)
                    meta = {"md5": hashlib.md5(data).hexdigest(),
                            "size": len(data)}
                    cache[f["key"]] = meta
                files[dest] = {"hash": meta["md5"], "size": meta["size"],
                               "url": f["url"]}
            provenance[repo] = [p for p, _ in paths]
            print(f"OK  {repo:<40} {len(paths)} files  ({source})")
        except Exception as e:
            errors.append(repo)
            # Never let a transient failure uninstall a core from every SD
            # card following this database: carry the previous build forward.
            carried = 0
            for p in provenance.get(repo, []):
                if p in old_db.get("files", {}):
                    files[p] = old_db["files"][p]
                    carried += 1
            print(f"!!  {repo:<40} failed ({e}); carried {carried} "
                  f"previous files")

    folders = {}
    for p in files:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            folders["/".join(parts[:i])] = {}

    db = {
        "db_id": DB_ID,
        "db_url": DB_URL,
        "timestamp": int(time.time()),
        "files": dict(sorted(files.items())),
        "folders": dict(sorted(folders.items())),
        "tag_dictionary": {},
    }

    # An empty database is not "nothing to install" — to the Downloader it is
    # an instruction to DELETE everything this database owns, on every SD card
    # following it. Publishing one is therefore never an accident worth
    # honouring: an empty registry, a JSON typo that drops the entries, or a
    # bad merge would all wipe _Unofficial Cores/ for every user. Observed for
    # real: the first CI run fired on a registry that was still empty and
    # committed a 0-file database. Emptying the catalog on purpose stays
    # possible, but it has to be said out loud.
    if not files and not args.allow_empty:
        print(f"\nrefusing to publish an empty database "
              f"({len(registry.get('cores', []))} cores in the registry). "
              f"An empty database uninstalls everything it owns from every SD "
              f"card following it. Pass --allow-empty if that is really the "
              f"intent.")
        return 2

    def comparable(d):
        return {k: v for k, v in d.items() if k != "timestamp"}
    if comparable(db) == comparable(old_db):
        print(f"\nno changes ({len(files)} files); database left untouched")
        return 2 if errors else 0

    text = json.dumps(db, indent=2, ensure_ascii=False) + "\n"
    write_text(db_path, text)
    # Deterministic zip: fixed date and no compression metadata drift, so the
    # committed artifact only changes when its content does.
    zpath = os.path.join(args.out, "db.json.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("db.json", date_time=(2020, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        z.writestr(info, text)
    write_text(args.cache, json.dumps(cache, indent=1, sort_keys=True) + "\n")

    print(f"\ndatabase -> {zpath}  ({len(files)} files, "
          f"{len(registry.get('cores', []))} cores, {len(errors)} errors)")
    # Steps the database deliberately does not perform. Printed every build so
    # a partially-installed core never looks finished.
    manual = [(c["repo"], c["manual"]) for c in registry.get("cores", [])
              if c.get("manual")]
    if manual:
        print("\nMANUAL STEPS still required (not automated on purpose):")
        for repo, note in manual:
            print(f"  {repo}: {note}")
    return 2 if errors else 1


if __name__ == "__main__":
    sys.exit(main())
