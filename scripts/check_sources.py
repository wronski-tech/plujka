#!/usr/bin/env python3
"""Detect changed or removed source files on KBW Dane Wyborcze and refresh the affected elections.

PKW occasionally republishes corrected files. ``data/source_manifest.json`` (committed) records, for
every file of the local KBW mirror, its URL, size, SHA-256 and the server's ETag / Last-Modified.
``--check`` asks the server for the headers only (HTTP HEAD, no download) and reports files whose
ETag, Last-Modified or size changed, or that disappeared, grouped by the mart family-years to rebuild.

Examples:
  python3 scripts/check_sources.py                    # check; exit code 1 when something changed
  python3 scripts/check_sources.py --record           # (re)write the manifest from the local mirror
  python3 scripts/check_sources.py --update           # download changed files, re-stage them, update
                                                      # the manifest, print the build_mart command
  python3 scripts/check_sources.py --update --rebuild # ... and run build_mart.py (needs DATABASE_URL)

The weekly workflow ``.github/workflows/sources.yml`` runs ``--check`` with the committed manifest;
it needs no mirror. ``--record`` and ``--update`` need the mirror in ``data/kbw_mirror/dane``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://danewyborcze.kbw.gov.pl/dane/"
USER_AGENT = "plujka-source-check/1.0 (+https://github.com/wronski-tech/plujka)"
MANIFEST = ROOT / "data" / "source_manifest.json"
MIRROR = ROOT / "data" / "kbw_mirror" / "dane"

# Second path segment of a mirror file -> mart family (scripts/build_mart.py FAMILIES).
FAMILY_BY_DIR = {
    "sejmsenat": "parliament", "sejm": "parliament", "senat": "parliament",
    "prezydent": "prezydent", "parlament_eu": "pe", "referendum": "referendum", "samorzad": "samorzad",
}

Head = Callable[[str], dict]


def family_year(rel_path: str) -> tuple[str, int]:
    """Mart family and year a mirror file belongs to (files directly under a year are parliament files)."""
    parts = rel_path.split("/")
    year = int(parts[0])
    family = FAMILY_BY_DIR.get(parts[1], "parliament") if len(parts) > 2 else "parliament"
    return family, year


# Mirror prefixes that differ from the server path: the server keeps the 2010 local-election files
# directly under /dane/2010/, the mirror under 2010/samorzad/ (apart from the 2010 presidential files).
SERVER_PREFIX = {"2010/samorzad/": "2010/"}


def file_url(rel_path: str) -> str:
    """Public URL of a mirror file (the mirror keeps the server's path under /dane/, see SERVER_PREFIX)."""
    for mirror_prefix, server_prefix in SERVER_PREFIX.items():
        if rel_path.startswith(mirror_prefix):
            rel_path = server_prefix + rel_path[len(mirror_prefix):]
    return BASE_URL + quote(rel_path)


def http_head(url: str) -> dict:
    """``{"status", "etag", "last_modified", "bytes"}`` of ``url`` from an HTTP HEAD request."""
    request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=60) as response:
            length = response.headers.get("Content-Length")
            return {
                "status": response.status,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "bytes": int(length) if length else None,
            }
    except HTTPError as exc:
        return {"status": exc.code}
    except (URLError, OSError) as exc:
        return {"status": None, "error": str(exc)}


def sha256(path: Path) -> str:
    """SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def entry(rel: str, mirror: Path, head: Head = http_head) -> dict:
    """Manifest entry of one mirror file: local size and hash, server ETag and Last-Modified."""
    path = mirror / rel
    remote = head(file_url(rel))
    if remote.get("status") != 200:
        print(f"warning: {rel}: HTTP {remote.get('status')} {remote.get('error', '')}".rstrip(), file=sys.stderr)
    return {
        "url": file_url(rel),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "etag": remote.get("etag"),
        "last_modified": remote.get("last_modified"),
    }


def record(mirror: Path, head: Head = http_head, delay: float = 0.2) -> dict[str, dict]:
    """Manifest entries for every file of the mirror (one HEAD request per file)."""
    entries = {}
    for path in sorted(p for p in mirror.rglob("*") if p.is_file()):
        rel = path.relative_to(mirror).as_posix()
        entries[rel] = entry(rel, mirror, head)
        time.sleep(delay)
    return entries


def compare(manifest: dict[str, dict], head: Head = http_head, delay: float = 0.2) -> dict[str, list[str]]:
    """Files whose server headers differ from the manifest: ``{"changed": [...], "missing": [...], "unreachable": [...]}``."""
    result: dict[str, list[str]] = {"changed": [], "missing": [], "unreachable": []}
    for rel, entry in sorted(manifest.items()):
        remote = head(entry["url"])
        status = remote.get("status")
        if status == 404 or status == 410:
            result["missing"].append(rel)
        elif status != 200:
            result["unreachable"].append(rel)
        elif any(
            entry.get(field) is not None and remote.get(field) is not None and entry[field] != remote[field]
            for field in ("etag", "last_modified", "bytes")
        ):
            result["changed"].append(rel)
        time.sleep(delay)
    return result


def affected(files: list[str]) -> dict[str, list[int]]:
    """Family -> sorted years to rebuild for the given mirror files."""
    years: dict[str, set[int]] = defaultdict(set)
    for rel in files:
        family, year = family_year(rel)
        years[family].add(year)
    return {family: sorted(ys) for family, ys in sorted(years.items())}


def build_commands(files: list[str]) -> list[list[str]]:
    """``build_mart.py`` invocations that rebuild exactly the affected family-years."""
    return [
        [sys.executable, "scripts/build_mart.py", "--elections", family, "--years", ",".join(map(str, years))]
        for family, years in affected(files).items()
    ]


def download(rel: str, mirror: Path) -> None:
    """Replace the mirror copy of ``rel`` with the server's current file."""
    request = Request(file_url(rel), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=300) as response:
        data = response.read()
    target = mirror / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--mirror", type=Path, default=MIRROR)
    parser.add_argument("--record", action="store_true", help="Rewrite the manifest from the local mirror")
    parser.add_argument("--update", action="store_true", help="Download changed files, re-stage, update the manifest")
    parser.add_argument("--rebuild", action="store_true", help="With --update: run build_mart.py for affected family-years")
    parser.add_argument("--delay", type=float, default=0.2, help="Seconds between requests")
    args = parser.parse_args()

    if args.record:
        entries = record(args.mirror, delay=args.delay)
        args.manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"manifest: {len(entries)} files -> {args.manifest}")
        return 0

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = compare(manifest, delay=args.delay)
    for kind in ("changed", "missing", "unreachable"):
        for rel in result[kind]:
            print(f"{kind}: {rel}")
    stale = result["changed"] + result["missing"]
    print(f"checked {len(manifest)} files: {len(result['changed'])} changed, {len(result['missing'])} missing, "
          f"{len(result['unreachable'])} unreachable")
    if not stale:
        return 0
    for family, years in affected(stale).items():
        print(f"rebuild: {family} {', '.join(map(str, years))}")
    if not args.update:
        return 1

    for rel in result["changed"]:
        download(rel, args.mirror)
        print(f"downloaded: {rel}")
    prefixes = sorted({"/".join(rel.split("/")[:2]) for rel in result["changed"]})
    for prefix in prefixes:
        subprocess.run(
            [sys.executable, "scripts/kbw_stage_duckdb.py", "--root", str(args.mirror), "--out", "data/kbw_stage_parquet",
             "--prefix", prefix],
            cwd=ROOT, check=True,
        )
    manifest.update({rel: entry(rel, args.mirror) for rel in result["changed"]})
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for command in build_commands(result["changed"]):
        print("run:", " ".join(command))
        if args.rebuild:
            subprocess.run(command, cwd=ROOT, check=True)
    if result["missing"]:
        print("missing files are kept in the mirror and the manifest; check the KBW page by hand", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
