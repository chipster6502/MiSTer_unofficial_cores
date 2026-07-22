# MiSTer Unofficial Cores

Automated discovery and installation of unofficial MiSTer cores — the ones
published on GitHub that live in **no database at all**.

## The loop

1. **Mondays** a GitHub scan finds new unofficial core repos. Anything already
   covered by the 92 databases Update_All indexes — including theypsilon's
   unofficial distribution — is filtered out by construction.
2. Findings land in the tracking issue, and convention-matching ones arrive as
   a PR against `tools/unofficial_cores.json`. **Merging the PR is the
   approval.** To reject a core, delete its registry entry but keep its
   `tools/known_repos.json` line.
3. On merge (and weekly, because upstream repos ship new `.rbf` builds),
   `db/db.json.zip` is rebuilt — a standard custom database for the MiSTer
   Downloader.
4. Any MiSTer following it installs/updates everything on the next
   Downloader/update_all run. Removing a core from the registry removes its
   files from the SD: the database is authoritative in both directions.

## Enable it on the MiSTer

Append to `/media/fat/downloader.ini` (create it if missing):

```ini
[chipster6502/MiSTer_unofficial_cores]
db_url = https://raw.githubusercontent.com/chipster6502/MiSTer_unofficial_cores/main/db/db.json.zip
```

## Where things land

```
_Unofficial Cores/<Core>_<date>.rbf            non-arcade cores
_Unofficial Cores/_Arcade/<game>.mra           arcade
_Unofficial Cores/_Arcade/cores/<core>.rbf
```

Arcade ROM zips are **not** distributed — provision them as for any arcade core.

## Manual additions and local builds

Anything the scan cannot vouch for (the *maybe* bucket, or your own finds) is
added by hand: one entry in `tools/unofficial_cores.json`, commit, done. To
build locally first:

```bash
GITHUB_TOKEN=your_pat python3 tools/build_unofficial_db.py
```

The builder handles the three distribution layouts seen in the wild
(`releases/` folder, GitHub Releases assets, repo root), keeps only the newest
`.rbf` per name, hashes real bytes with a cache, and — because the Downloader
deletes files that vanish from a database — carries a repo's previous files
forward if its API calls fail rather than uninstalling it from every SD card
that follows.
