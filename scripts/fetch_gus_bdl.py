#!/usr/bin/env python3
"""Fetch GUS context statistics per territorial unit and year from the Local Data Bank (BDL) API.

The mart family ``gus`` (``api/services/mart/build_gus.py``) relates election results to official
statistics of Statistics Poland (GUS): population (total, urban, rural; 31 December), area, population
density, registered unemployment (gmina counts and share of the working-age population; powiat
unemployment rate) and the average gross monthly salary (powiat). The variables are listed in
``build_gus.INDICATORS``; this script downloads every published year in ``--first-year``..``--last-year``
for Poland, voivodeships, powiats and (where BDL publishes them) gminas, and writes committed source data:

- ``data/gus/bdl_values.parquet``: bdl_variable_id, year, unit_level (BDL level: 0 Poland,
  2 voivodeship, 5 powiat, 6 gmina), bdl_unit_id, unit_name, teryt (from the unit id, see
  ``build_gus.bdl_unit_teryt``; 7 digits with the type digit for gmina-level units, NULL for Poland), value, attr_id
  (BDL attribute of the value);
- ``data/gus/bdl_variables.csv``: one row per variable with its BDL subject, name, measure unit,
  levels and years fetched, record counts, retrieval date and source/licence note.

Records whose BDL attribute means "no value" (``build_gus.MISSING_ATTRIBUTES``) are dropped and counted
in ``bdl_variables.csv``; an unknown attribute, a page that cannot be fetched after the retries, a level
without data or a requested year without any value stops the run (nothing is skipped silently).

API limits (https://api.stat.gov.pl/Home/BdlApi): anonymous clients may send 5 requests per second, 100
per 15 minutes, 1,000 per 12 hours and 10,000 per 7 days; with a free key (header ``X-ClientId``, taken
from the environment variable ``BDL_API_KEY``) 10 / 500 / 5,000 / 50,000. Pages have at most 100 units,
so a full run is about 350 requests: about an hour without a key, about 15 minutes with one. The script
stays below the limits on its own, waits and retries on HTTP 429, 5xx and network errors, and caches
every page under ``--cache-dir`` so that an interrupted run resumes where it stopped. Data are published
by GUS under CC BY 4.0 (source: GUS, Bank Danych Lokalnych).

Usage:
  python3 scripts/fetch_gus_bdl.py [--first-year 1995] [--last-year 2024] [--out-dir data/gus]
                                   [--cache-dir .tmp/gus_bdl] [--refresh]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.mart.build_gus import (  # noqa: E402
    INDICATORS,
    MISSING_ATTRIBUTES,
    VALUE_ATTRIBUTES,
    bdl_unit_teryt,
)

API = "https://bdl.stat.gov.pl/api/v1/"
USER_AGENT = "plujka-gus-bdl/1.0 (+https://github.com/wronski-tech/plujka)"
PAGE_SIZE = 100  # the API maximum
# BDL levels fetched per variable: gmina-level variables also have powiat, voivodeship and country
# aggregates; the unemployment rate and salaries are published down to powiats only.
POWIAT_ONLY = {60270, 64428}
# Requests allowed per window (seconds), kept a little below the published limits.
ANONYMOUS_LIMITS = {1: 4, 900: 95, 43200: 950}
KEY_LIMITS = {1: 9, 900: 480, 43200: 4800}
SOURCE_NOTE = "GUS, Bank Danych Lokalnych (https://bdl.stat.gov.pl), API v1; licence CC BY 4.0"


class Client:
    """Sequential BDL API client with sliding-window rate limits, retries and a page cache."""

    def __init__(self, cache_dir: Path, refresh: bool) -> None:
        self.key = os.getenv("BDL_API_KEY")
        self.limits = KEY_LIMITS if self.key else ANONYMOUS_LIMITS
        self.sent: deque[float] = deque()
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.requests = 0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _wait_for_slot(self) -> None:
        while True:
            now = time.monotonic()
            longest = max(self.limits)
            while self.sent and now - self.sent[0] > longest:
                self.sent.popleft()
            waits = []
            for window, limit in self.limits.items():
                recent = [t for t in self.sent if now - t < window]
                if len(recent) >= limit:
                    waits.append(window - (now - recent[0]) + 0.05)
            if not waits:
                return
            pause = max(waits)
            if pause > 5:
                print(f"[gus] rate limit: waiting {pause:.0f}s ({self.requests} requests so far)", flush=True)
            time.sleep(pause)

    def get(self, path: str, params: list[tuple[str, object]]) -> dict:
        """GET ``API + path`` as JSON (cached); raises RuntimeError after the retries."""
        query = urlencode([("format", "json"), ("lang", "pl"), *params])
        url = f"{API}{path}?{query}"
        cache = self.cache_dir / f"{path.replace('/', '_')}_{hashlib.sha1(query.encode()).hexdigest()[:16]}.json"
        if cache.exists() and not self.refresh:
            return json.loads(cache.read_text(encoding="utf-8"))
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.key:
            headers["X-ClientId"] = self.key
        delay = 5.0
        for attempt in range(1, 7):
            self._wait_for_slot()
            self.sent.append(time.monotonic())
            self.requests += 1
            try:
                with urlopen(Request(url, headers=headers), timeout=120) as response:
                    payload = response.read().decode("utf-8")
                data = json.loads(payload)
                cache.write_text(payload, encoding="utf-8")
                return data
            except HTTPError as exc:
                if exc.code == 429:
                    pause = float(exc.headers.get("Retry-After") or 120)
                    print(f"[gus] HTTP 429 on {url}; waiting {pause:.0f}s", flush=True)
                    time.sleep(pause)
                    continue
                if exc.code < 500 or attempt == 6:
                    body = exc.read().decode("utf-8", "replace")[:500]
                    raise RuntimeError(f"cannot fetch {url}: HTTP {exc.code} {body}") from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == 6:
                    raise RuntimeError(f"cannot fetch {url}: {exc}") from exc
            print(f"[gus] attempt {attempt} failed for {url}; retrying in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"cannot fetch {url}: too many HTTP 429 responses")


def fetch_variable(client: Client, variable_id: int, first: int, last: int) -> tuple[dict, list[tuple]]:
    """Metadata row and value rows of one variable at every configured level."""
    meta = client.get(f"variables/{variable_id}", [])
    subject = client.get(f"subjects/{meta['subjectId']}", [])
    years = [y for y in meta["years"] if first <= y <= last]
    if not years:
        raise RuntimeError(f"variable {variable_id}: no published year in {first}-{last}")
    levels = (0, 2, 5) if variable_id in POWIAT_ONLY else (0, 2, 5, 6)
    rows: list[tuple] = []
    missing = 0
    for level in levels:
        units, page, total = 0, 0, None
        years_with_values: set[int] = set()
        while total is None or page * PAGE_SIZE < total:
            params = [("unit-level", level), *[("year", y) for y in years], ("page-size", PAGE_SIZE), ("page", page)]
            data = client.get(f"data/by-variable/{variable_id}", params)
            total = data["totalRecords"]
            for unit in data["results"]:
                units += 1
                teryt = bdl_unit_teryt(unit["id"], level)
                for v in unit["values"]:
                    attr = int(v["attrId"])
                    if attr in MISSING_ATTRIBUTES:
                        missing += 1
                        continue
                    if attr not in VALUE_ATTRIBUTES:
                        raise RuntimeError(f"variable {variable_id}, unit {unit['id']}, {v['year']}: unknown attribute {attr}")
                    years_with_values.add(int(v["year"]))
                    rows.append((variable_id, int(v["year"]), level, unit["id"], unit["name"], teryt, v["val"], attr))
            page += 1
        if not total or units != total:
            raise RuntimeError(f"variable {variable_id}, level {level}: read {units} of {total} units")
        if absent := sorted(set(years) - years_with_values):
            raise RuntimeError(f"variable {variable_id}, level {level}: no value published for {absent}")
        print(f"[gus] variable {variable_id} level {level}: {units} units, {len(years)} years", flush=True)
    names = " / ".join(meta[k] for k in ("n1", "n2", "n3", "n4", "n5") if meta.get(k))
    info = {
        "bdl_variable_id": variable_id,
        "indicator": next(k for k, v in INDICATORS.items() if v[0] == variable_id),
        "subject_id": meta["subjectId"],
        "subject_name": subject["name"],
        "variable_name": names,
        "measure_unit": meta["measureUnitName"],
        "unit_levels": " ".join(map(str, levels)),
        "first_year": years[0],
        "last_year": years[-1],
        "records": len(rows),
        "dropped_missing": missing,
        "retrieved_at": dt.date.today().isoformat(),
        "source": f"{SOURCE_NOTE}; {API}data/by-variable/{variable_id}",
    }
    return info, rows


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--first-year", type=int, default=1995)
    parser.add_argument("--last-year", type=int, default=2024)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "gus")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".tmp" / "gus_bdl")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached pages and download again")
    args = parser.parse_args()

    client = Client(args.cache_dir, args.refresh)
    infos, rows = [], []
    for indicator, (variable_id, _, _) in INDICATORS.items():
        print(f"[gus] {indicator}: BDL variable {variable_id}", flush=True)
        info, variable_rows = fetch_variable(client, variable_id, args.first_year, args.last_year)
        infos.append(info)
        rows.extend(variable_rows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    values = args.out_dir / "bdl_values.parquet"
    tmp_csv, tmp = values.with_suffix(".csv.tmp"), values.with_suffix(".parquet.tmp")
    with tmp_csv.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    columns = {
        "bdl_variable_id": "INTEGER", "year": "SMALLINT", "unit_level": "TINYINT", "bdl_unit_id": "VARCHAR",
        "unit_name": "VARCHAR", "teryt": "VARCHAR", "value": "DOUBLE", "attr_id": "SMALLINT",
    }
    duckdb.connect().execute(
        f"""COPY (SELECT * FROM read_csv('{tmp_csv}', header = false, columns = {columns})
                  ORDER BY bdl_variable_id, unit_level, bdl_unit_id, year)
            TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )
    tmp_csv.unlink()
    tmp.replace(values)
    with (args.out_dir / "bdl_variables.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(infos[0]))
        writer.writeheader()
        writer.writerows(infos)
    print(f"[gus] wrote {len(rows)} values to {values} ({values.stat().st_size / 1e6:.1f} MB); "
          f"{client.requests} requests", flush=True)


if __name__ == "__main__":
    main()
