#!/usr/bin/env python3
"""Scrape the 2006 list blocs (grupy list) of the local elections from the PKW results website.

In the 2006 local elections (Dz. U. 2006 Nr 159, poz. 1127) two or more committees could form a bloc
of lists (grupa list) in a council or voivodeship assembly elected proportionally. A bloc that got at
least 10% of the council's valid votes (15% in sejmiki) took part in the seat allocation as one list;
its seats were then split among its lists that passed 5%. The KBW data files
(danewyborcze.kbw.gov.pl) do not record blocs at all: PKW published them only on its results pages
at wybory2006.pkw.gov.pl, so this script reads them from there and writes
``data/pkw_html/list_groups_2006.csv`` (committed source data, read by the mart build).

Pages read:
- sejmiki: the results page of each voivodeship assembly (``wynikiSejmik…?jdn=XX0000``, section
  "Następujące listy komitetów wyborczych … tworzą grupy list"),
- councils: the page of every council elected proportionally (``wybRadaPowiatu``, ``wybRadaMiasta``,
  ``wybRadaGminy``, ``wybRadaDzielnicy``; same section). The councils come from the staged KBW
  candidate file (levels GM, GP, PO, DZ); the pages are found by following the site's geography
  pages, because their URLs carry a hash that cannot be derived from the TERYT code.

Output columns: year, election ('sejmik' or 'rada'), council_level ('' for sejmik; 'gmina', 'powiat',
'dzielnica'), unit (voivodeship name for sejmik, TERYT of the council: 4 digits for powiats, 6 for
gminas and Warsaw districts), group_no (lowest list number of the bloc), list_no, committee_name (as
on the page), source_url. Councils whose page has no bloc section are printed at the end (they are
listed in ``build_rady.NO_LIST_GROUPS``). A bloc page may name a list that PKW invalidated before the
ballots were printed; such lists have no candidates in the KBW files (``build_rady.INVALIDATED_GROUP_LISTS``).

Every page is fetched once with a delay between requests; any HTTP or parsing error stops the run.

Usage:
  python3 scripts/scrape_list_groups_2006.py [--out data/pkw_html/list_groups_2006.csv] [--delay 0.3]
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import duckdb

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://wybory2006.pkw.gov.pl/kbw/"
USER_AGENT = "plujka-list-groups/1.0 (+https://github.com/wronski-tech/plujka)"
CANDIDATES = ROOT / "data" / "kbw_stage_parquet" / "year=2006" / "2006" / "samorzad" / "2006-kand-rady-new.parquet"
OUT = ROOT / "data" / "pkw_html" / "list_groups_2006.csv"
VOIVODESHIPS = {
    "02": "dolnośląskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie", "10": "łódzkie",
    "12": "małopolskie", "14": "mazowieckie", "16": "opolskie", "18": "podkarpackie", "20": "podlaskie",
    "22": "pomorskie", "24": "śląskie", "26": "świętokrzyskie", "28": "warmińsko-mazurskie", "30": "wielkopolskie",
    "32": "zachodniopomorskie",
}
SECTION = "tworzą grupy list"


class Fetcher:
    """Sequential HTTP GET with a fixed delay between requests (the site is an archive; be polite)."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.count = 0

    def get(self, path: str) -> str:
        url = BASE_URL + path
        for attempt in range(3):
            time.sleep(self.delay)
            try:
                with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as response:
                    self.count += 1
                    return response.read().decode("utf-8")
            except URLError as exc:  # retried twice (network errors, HTTP 5xx); a missing page stops at once
                if attempt == 2 or getattr(exc, "code", None) == 404:
                    raise RuntimeError(f"cannot fetch {url}: {exc}") from exc
        raise AssertionError("unreachable")


def links(page: str, pattern: str) -> list[re.Match]:
    """Links of ``page`` (HTML-unescaped) matching ``pattern`` in full."""
    return [m for href in re.findall(r'href="([^"]+)"', page) if (m := re.fullmatch(pattern, html.unescape(href)))]


def section(page: str) -> str:
    """The bloc section of a page ('' when the page lists no blocs)."""
    start = page.find(SECTION)
    return page[start:page.find("</table>", start)] if start >= 0 else ""


def member_rows(block: str) -> list[tuple[int, str]]:
    """(list number, committee name) rows of one bloc's part of the section."""
    rows = re.findall(r'(?s)<td[^>]*><a href="komitet[^"]*">\s*(\d+)\s*</a></td>\s*<td[^>]*><a href="komitet[^"]*">(.*?)</a></td>', block)
    return [(int(no), html.unescape(re.sub(r"\s+", " ", name)).strip()) for no, name in rows]


def sejmik_groups(page: str) -> list[list[tuple[int, str]]]:
    """Blocs on a sejmik results page: a header row 'Grupa list nr …' followed by its member rows."""
    sec = section(page)
    heads = list(re.finditer(r'(?s)<tr[^>]*id="g\d+"[^>]*>.*?Grupa list(.*?)</b>', sec))
    return _groups(sec, heads)


def council_groups(page: str) -> list[list[tuple[int, str]]]:
    """Blocs on a council page: one table per bloc headed 'Grupa list nr …'."""
    start = page.find(SECTION)
    if start < 0:
        return []
    sec = page[start:page.find('id="breadcrumb"', start)]
    heads = list(re.finditer(r'(?s)<th colspan="2" class="tl largeFont">\s*Grupa list(.*?)</th>', sec))
    return _groups(sec, heads)


def _groups(sec: str, heads: list[re.Match]) -> list[list[tuple[int, str]]]:
    """Member rows of each bloc; the rows must list exactly the numbers in the bloc's header."""
    groups = []
    for i, head in enumerate(heads):
        numbers = [int(n) for n in re.findall(r"nr (\d+)", head[1])]
        rows = member_rows(sec[head.end():heads[i + 1].start() if i + 1 < len(heads) else len(sec)])
        if sorted(no for no, _ in rows) != sorted(numbers) or len(numbers) < 2:
            raise RuntimeError(f"bloc header {numbers} does not match its rows {rows}")
        groups.append(rows)
    if SECTION in sec and not groups:
        raise RuntimeError("bloc section without blocs")
    return groups


def proportional_councils() -> set[tuple[str, str]]:
    """(council_level, unit) of every council elected proportionally, from the staged candidate file."""
    rows = duckdb.sql(
        f"""SELECT DISTINCT "Szczebel", lpad(trim("TERYT"), 6, '0') FROM read_parquet('{CANDIDATES}')
            WHERE "Szczebel" IN ('GM', 'GP', 'PO', 'DZ')"""
    ).fetchall()
    level = {"GM": "gmina", "GP": "gmina", "PO": "powiat", "DZ": "dzielnica"}
    return {(level[s], t[:4] if s == "PO" else t) for s, t in rows}


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between requests")
    args = parser.parse_args()
    fetch = Fetcher(args.delay)
    councils = proportional_councils()
    gminas = {unit for level, unit in councils if level in ("gmina", "dzielnica")}

    rows: list[tuple] = []
    pages: dict[tuple[str, str], str] = {}
    country = fetch.get("geoKrajd41d.html?")
    # The country page links every voivodeship twice (under different hashes): visit each once.
    voivodeships = {m[2]: m[1] for m in links(country, r"(geoWojewodztwo[\w-]+\.html\?id=(\d{6})&type=0)")}
    for woj_id, woj_link in sorted(voivodeships.items()):
        woj_page = fetch.get(woj_link)
        sejmik = fetch.get(links(woj_page, r"(wybSejmik[\w-]+\.html\?jdn=\d{6})")[0][1])
        results = links(sejmik, r"(wynikiSejmik[\w-]+\.html\?jdn=\d{6})")[0][1]
        name = VOIVODESHIPS[woj_id[:2]]
        for group in sejmik_groups(fetch.get(results)):
            rows += [(2006, "sejmik", "", name, min(no for no, _ in group), no, committee, BASE_URL + results) for no, committee in group]
        for powiat in sorted({m[1] for m in links(woj_page, r"(geoPowiat[\w-]+\.html\?id=\d{6}&type=0)")}):
            powiat_page = fetch.get(powiat)
            for m in links(powiat_page, r"(wybRada(Powiatu|Miasta)[\w-]+\.html\?jdn=(\d{6}))"):
                pages[("powiat", m[3][:4]) if m[2] == "Powiatu" else ("gmina", m[3])] = m[1]
            for gmina in sorted({m[1] for m in links(powiat_page, r"(geoGmina[\w-]+\.html\?id=(\d{6})&type=0)") if m[2] in gminas}):
                for m in links(fetch.get(gmina), r"(wybRada(Gminy|Miasta|Dzielnicy)[\w-]+\.html\?jdn=(\d{6}))"):
                    pages[("dzielnica" if m[3].startswith("1465") and m[3] != "146501" else "gmina", m[3])] = m[1]
        print(f"{name}: {len(pages)} council pages so far, {fetch.count} requests", file=sys.stderr)

    missing = councils - set(pages)
    if missing:
        raise RuntimeError(f"no PKW page found for councils {sorted(missing)}")
    without = []
    for level, unit in sorted(councils):
        url = pages[(level, unit)]
        groups = council_groups(fetch.get(url))
        if not groups:
            without.append(f"{level}/{unit}")
        for group in groups:
            rows += [(2006, "rada", level, unit, min(no for no, _ in group), no, committee, BASE_URL + url) for no, committee in group]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["year", "election", "council_level", "unit", "group_no", "list_no", "committee_name", "source_url"])
        writer.writerows(sorted(rows, key=lambda r: (r[1], r[2], r[3], r[4], r[5])))
    print(f"{len(rows)} rows -> {args.out}; {fetch.count} requests; {len(councils)} councils, {len(without)} without blocs:",
          file=sys.stderr)
    print(" ".join(without), file=sys.stderr)


if __name__ == "__main__":
    main()
