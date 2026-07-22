#!/usr/bin/env python3
"""
scan_github.py — find unofficial MiSTer CORES that live in no database.

WHY THIS EXISTS
    audit_cores.py scans the 92 databases Update_All indexes. It cannot see
    z386: installed by hand from its author's repo, in no database at all.
    Verified — nand2mario/z386_MiSTer was created 2026-04-26 and reached us as
    a user report months later. GitHub does see it. This is that, weekly.

SCOPE — CORES ONLY
    Generic ecosystem watching (tools, add-ons, cases, docs) moved to the
    MiSTer_ecosystem repo. What stays here is exactly what MiSTer Monitor
    needs from GitHub, split by what each bucket is FOR:

      mappable  <Core>_MiSTer convention, NOT arcade, NOT from an owner the
                database scan already covers, and its core name is NOT among
                the databases' triaged cores. These are the ones that will
                someday need a CORE_NAME_MAPPING entry — and they are also
                install candidates.
      arcade    Arcade-*_MiSTer outside the official channel. Arcade is
                addressed by .mra, never by CORENAME, so there is nothing to
                map — but they are install candidates all the same.
      maybe     no naming convention, but the description says 'core' next to
                'mister' (pacifax: "NEC PC-FX core for MiSTer FPGA"). Leads
                for a human; never proposed automatically.

    Both mappable and arcade feed --proposals: registry-shaped entries the
    weekly workflow turns into a PR against tools/unofficial_cores.json.
    Merging that PR is the approval gate; the database builder only ever
    reads the merged registry.

WHAT IT CANNOT DO
    Same limit as always: this yields a REPO, not a CORENAME. 'z386_MiSTer'
    does not tell you whether the core writes 'Z386', 'z386' or something else
    to /tmp/CORENAME. /status/unknown_cores remains the only source of the
    literal key.

USAGE
    GITHUB_TOKEN=... python3 tools/scan_github.py --days 8 \
        --baseline tools/known_repos.json --known-cores tools/known_cores.json
    (unauthenticated works but rate-limits at 10 searches/min)

EXIT CODES
    0  nothing new      1  new repos found      2  search unavailable
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.github.com/search/repositories"

# The <Core>_MiSTer convention. Case varies in the wild — MiSTer-devel itself
# ships 'ZX-Spectrum_MISTer', and vinej publishes 'X16_Mister' — so the test is
# case-insensitive or it silently drops real cores.
CORE_NAME_RE = re.compile(r"_mister$", re.I)

# Owners whose output already arrives through audit_cores.py's database scan.
# Their new repos surface via the databases within days; re-reporting them here
# would only add noise to the actionable bucket.
COVERED_OWNERS = {
    "mister-devel", "mister-db9", "mister-llapi", "mister-unstable-nightlies",
    "jotego", "coin-opcollection", "theypsilon", "atrac17", "mikes11",
    "zaparooproject", "misterfpga",
}

# Arcade is addressed by .mra and never by CORENAME: nothing to map, but still
# an install candidate — hence its own bucket instead of being dropped.
ARCADE_NAME_RE = re.compile(r"^arcade[-_]", re.I)

# The 'maybe' test. Deliberately tighter than the old ecosystem filter: only
# the word 'core(s)' counts, because 'fpga'/'retro'/'de10' next to 'mister'
# match half the hobby. This is the pacifax net — cores that skipped the
# naming convention entirely.
MAYBE_CORE_RE = re.compile(r"\bcores?\b", re.I)


def core_name(repo_name):
    """'z386_MiSTer' -> 'z386'. What we compare against the databases."""
    return CORE_NAME_RE.sub("", repo_name)


def search(query, token, pages=3):
    """Repos matching `query`, newest first. Stops at `pages` x 100."""
    out = []
    for page in range(1, pages + 1):
        url = f"{API}?q={urllib.parse.quote(query)}&sort=updated&order=desc&per_page=100&page={page}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "MiSTer-Monitor-core-scan",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except Exception as e:
            # 403 here is almost always the rate limit: 10 searches/min without
            # a token, 30 with one. Report what we have rather than crash.
            print(f"!! search failed on page {page}: {e}")
            break
        items = data.get("items", [])
        out.extend(items)
        if len(items) < 100:
            break
        time.sleep(2)          # stay under the secondary rate limit
    return out


def classify(repo, triaged_lc):
    """'mappable' | 'arcade' | 'maybe' | None (drop).

    Dropped and why:
      - convention repo from a COVERED owner: the database scan reports it
      - convention repo whose core name is already triaged in the databases:
        official core, already mapped (or deliberately not) — a third-party
        re-upload of it is not an unofficial core
      - everything that says neither the convention nor 'core' about itself
    """
    name = repo.get("name") or ""
    owner = (repo.get("owner") or {}).get("login", "")
    desc = repo.get("description") or ""

    if CORE_NAME_RE.search(name):
        if owner.lower() in COVERED_OWNERS:
            return None
        if ARCADE_NAME_RE.match(name):
            return "arcade"
        if core_name(name).lower() in triaged_lc:
            return None
        return "mappable"

    if "mister" in (name + " " + desc).lower() and MAYBE_CORE_RE.search(desc):
        return "maybe"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8,
                    help="how far back to look; a weekly cron wants overlap")
    ap.add_argument("--baseline", help="JSON of repos already reported")
    ap.add_argument("--known-cores", default="tools/known_cores.json",
                    help="databases' triaged cores; convention repos matching "
                         "one of these names are official, not unofficial")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--proposals",
                    help="write registry-shaped entries (mappable+arcade) "
                         "here, for the PR against tools/unofficial_cores.json")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    print(f"searching GitHub {'with' if token else 'WITHOUT'} a token "
          f"({'30' if token else '10'} searches/min)\n")

    triaged_lc = set()
    if args.known_cores and os.path.isfile(args.known_cores):
        with io.open(args.known_cores, encoding="utf-8") as f:
            triaged_lc = {c.lower() for c in json.load(f).get("triaged", [])}

    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - args.days * 86400))
    repos = search(f"MiSTer in:name,description fork:false created:>{since}", token)
    if not repos:
        print("no results (search unavailable, or genuinely nothing new)")
        return 2

    seen = set()
    if args.baseline and os.path.isfile(args.baseline):
        with io.open(args.baseline, encoding="utf-8") as f:
            seen = {r.lower() for r in json.load(f).get("reported", [])}

    buckets = {"mappable": [], "arcade": [], "maybe": []}
    for r in repos:
        kind = classify(r, triaged_lc)
        if not kind:
            continue
        buckets[kind].append({
            "full_name": r["full_name"],
            "url": r["html_url"],
            "description": (r.get("description") or "").strip(),
            "created": (r.get("created_at") or "")[:10],
            "stars": r.get("stargazers_count", 0),
        })

    fresh = {k: [x for x in v if x["full_name"].lower() not in seen]
             for k, v in buckets.items()}
    n_all = sum(len(v) for v in buckets.values())
    n_fresh = sum(len(v) for v in fresh.values())

    print(f"created since {since}: {len(repos)} repos -> "
          f"{len(buckets['mappable'])} mappable, {len(buckets['arcade'])} arcade, "
          f"{len(buckets['maybe'])} maybe, {len(repos) - n_all} dropped")
    print(f"not reported before: {len(fresh['mappable'])} mappable, "
          f"{len(fresh['arcade'])} arcade, {len(fresh['maybe'])} maybe\n")

    def show(title, rows):
        if not rows:
            return
        print(f"## {title} ({len(rows)})\n")
        for c in sorted(rows, key=lambda x: -x["stars"]):
            print(f"  {c['full_name']:<44} {c['stars']:>4}* {c['created']}")
            if c["description"]:
                print(f"      {c['description'][:88]}")
        print()

    show("Unofficial cores — mapping candidates", fresh["mappable"])
    show("Unofficial arcade cores — install only, nothing to map",
         fresh["arcade"])
    show("Possible cores without the naming convention — confirm by hand",
         fresh["maybe"])

    if args.json:
        with io.open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"generated": int(time.time()), "since": since, **fresh},
                      f, indent=2)

    if args.proposals:
        today = time.strftime("%Y-%m-%d")
        entries = ([{"repo": c["full_name"], "arcade": False,
                     "notes": c["description"][:80], "added": today}
                    for c in fresh["mappable"]] +
                   [{"repo": c["full_name"], "arcade": True,
                     "notes": c["description"][:80], "added": today}
                    for c in fresh["arcade"]])
        with io.open(args.proposals, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"generated": today, "cores": entries}, f, indent=2)
        print(f"proposals -> {args.proposals} ({len(entries)})")

    if args.update_baseline and args.baseline:
        allseen = sorted(seen | {x["full_name"].lower()
                                 for v in buckets.values() for x in v})
        with io.open(args.baseline, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"comment": "Repos already surfaced by scan_github.py. "
                                  "Presence here means 'reported once', not "
                                  "'relevant'.",
                       "updated": time.strftime("%Y-%m-%d"),
                       "reported": allseen}, f, indent=1)
        print(f"baseline -> {args.baseline} ({len(allseen)} repos)")
        return 0

    return 1 if n_fresh else 0


if __name__ == "__main__":
    sys.exit(main())
