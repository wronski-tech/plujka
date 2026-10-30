#!/usr/bin/env python3
"""Fetch the results of local recall referendums (referenda w sprawie odwołania organów) from wybory.gov.pl.

PKW publishes, for each term since 2014, the local referendums held during the term to recall a
gmina or powiat council or a mayor (wójt, burmistrz, prezydent miasta): one site per term
(``referendum_lokalne_2014_2018``, ``..._2018_2024``, ``..._2024_2029``). Other local referendums
(on local matters) are not published there. Each site keeps its data as protobuf blobs: ``data/list.blob``
(the referendums) and ``data/details/<id>.blob`` (one referendum with the result protocol of the
territorial commission). This script decodes them without a schema (field numbers below, integers
in zigzag encoding) and writes ``data/pkw_html/local_referenda.csv`` (committed source data, read by
``derived.py``): one row per question (a referendum can ask to recall both the mayor and the council).

Output columns: term, referendum_id, referendum_date, teryt_wojewodztwo, wojewodztwo, teryt_powiat,
powiat, teryt_gmina, gmina (empty for powiat referendums), organ, question_no, question,
eligible_voters, ballots_cast (ballot papers taken from the ballot box), valid_votes, yes_votes,
no_votes, recall_election_voters and recall_election_date (voters in the election of the organ to
be recalled: a recall referendum is valid when at least 3/5 of that number take part), validity and
outcome (the commission's wording), source_url. Every question is checked before writing: yes + no
= valid votes <= ballots cast <= eligible voters, and the validity wording agrees with the 3/5 rule.
Referendums without a result protocol (not held yet, or called off) are skipped and listed.

Usage:
  python3 scripts/fetch_local_referenda.py [--out data/pkw_html/local_referenda.csv] [--delay 0.1]
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://wybory.gov.pl"
TERMS = ("referendum_lokalne_2014_2018", "referendum_lokalne_2018_2024", "referendum_lokalne_2024_2029")
USER_AGENT = "plujka-local-referenda/1.0 (+https://github.com/wronski-tech/plujka)"
MONTHS = {m: i for i, m in enumerate(("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia",
                                       "września", "października", "listopada", "grudnia"), start=1)}
COLUMNS = ["term", "referendum_id", "referendum_date", "teryt_wojewodztwo", "wojewodztwo", "teryt_powiat", "powiat",
           "teryt_gmina", "gmina", "organ", "question_no", "question", "eligible_voters", "ballots_cast", "valid_votes",
           "yes_votes", "no_votes", "recall_election_voters", "recall_election_date", "validity", "outcome", "source_url"]

# Field numbers. List entry: 1 id, 3 start of voting (Unix time), 6 unit (1/2 voivodeship code and
# name, 3/4 powiat, 5/6 gmina; codes are 6-digit TERYT with zero padding), 9 referendum (6 organ).
# Details: 16 result protocol, with one 33 per question: 1 question, 2 number, 4 valid votes,
# 5 yes, 6 no, 8 eligible, 24 ballot papers from the ballot box, 28 validity, 29 outcome,
# 31 voters in the election of the organ, 32 date of that election (Polish text).


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, pos


def decode(data: bytes) -> list[tuple[int, int | bytes]]:
    """Fields of one protobuf message as (number, value): varints as int, length-delimited as bytes."""
    fields: list[tuple[int, int | bytes]] = []
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        value: int | bytes
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos:pos + size], pos + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, pos = data[pos:pos + size], pos + size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.append((number, value))
    return fields


def zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def message(data: bytes) -> dict[int, list]:
    """Fields of a message grouped by number (repeated fields keep their order)."""
    grouped: dict[int, list] = {}
    for number, value in decode(data):
        grouped.setdefault(number, []).append(value)
    return grouped


def number(msg: dict[int, list], field: int) -> int:
    """A zigzag integer field (0 when absent, as protobuf omits zeros)."""
    return zigzag(msg[field][0]) if field in msg else 0


def text(msg: dict[int, list], field: int) -> str:
    return msg[field][0].decode("utf-8").strip() if field in msg else ""


def fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        if "html" in (response.headers.get("Content-Type") or ""):
            raise LookupError(url)
        return response.read()


def polish_date(value: str) -> str:
    """'21 października 2018' -> '2018-10-21'."""
    day, month, year = value.split()
    return f"{int(year):04d}-{MONTHS[month]:02d}-{int(day):02d}"


def check(row: dict) -> list[str]:
    """Problems of one question row (empty when it reconciles)."""
    problems = []
    if row["yes_votes"] + row["no_votes"] != row["valid_votes"]:
        problems.append("yes + no != valid votes")
    if not row["valid_votes"] <= row["ballots_cast"] <= row["eligible_voters"]:
        problems.append("valid votes <= ballots cast <= eligible voters does not hold")
    valid = re.match(r"(?i)referendum jest ważne", row["validity"]) is not None
    if not valid and re.match(r"(?i)referendum jest nieważne", row["validity"]) is None:
        problems.append(f"unknown validity wording {row['validity']!r}")
    elif valid != (5 * row["ballots_cast"] >= 3 * row["recall_election_voters"] > 0):
        problems.append("validity wording disagrees with the 3/5 rule")
    return problems


def referendum_rows(term: str, entry: dict[int, list], details: dict[int, list]) -> list[dict]:
    """One row per question of a referendum with a result protocol."""
    unit = message(entry[6][0])
    started = datetime.fromtimestamp(zigzag(entry[3][0]), ZoneInfo("Europe/Warsaw")).date().isoformat()
    referendum_id = zigzag(entry[1][0])
    organ = text(message(entry[9][0]), 6)
    gmina_code = number(unit, 5)
    base = {
        "term": term.removeprefix("referendum_lokalne_").replace("_", "-"), "referendum_id": referendum_id,
        "referendum_date": started, "teryt_wojewodztwo": f"{number(unit, 1):06d}"[:2], "wojewodztwo": text(unit, 2),
        "teryt_powiat": f"{number(unit, 3):06d}"[:4], "powiat": text(unit, 4),
        "teryt_gmina": f"{gmina_code:06d}" if gmina_code else "", "gmina": text(unit, 6), "organ": organ,
        "source_url": f"{BASE_URL}/{term}/pl/{referendum_id}/home",
    }
    rows = []
    for protocol in details[16]:
        for raw in message(protocol)[33]:
            q = message(raw)
            rows.append({
                **base, "question_no": int(text(q, 2)), "question": text(q, 1), "eligible_voters": number(q, 8),
                "ballots_cast": number(q, 24), "valid_votes": number(q, 4), "yes_votes": number(q, 5), "no_votes": number(q, 6),
                "recall_election_voters": number(q, 31), "recall_election_date": polish_date(text(q, 32)),
                "validity": text(q, 28), "outcome": text(q, 29),
            })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "pkw_html" / "local_referenda.csv")
    parser.add_argument("--delay", type=float, default=0.1)
    args = parser.parse_args()
    rows: list[dict] = []
    for term in TERMS:
        entries = [message(v) for n, v in decode(fetch(f"{BASE_URL}/{term}/data/list.blob")) if n == 1]
        for entry in entries:
            referendum_id = zigzag(entry[1][0])
            try:
                details = message(fetch(f"{BASE_URL}/{term}/data/details/{referendum_id}.blob"))
            except LookupError:
                details = {}
            time.sleep(args.delay)
            if 16 not in details:
                print(f"[skip] {term} {referendum_id}: no result protocol ({text(message(entry[9][0]), 2)})")
                continue
            rows += referendum_rows(term, entry, details)
    errors = [(r["term"], r["referendum_id"], r["question_no"], p) for r in rows for p in check(r)]
    if errors:
        raise SystemExit(f"{len(errors)} questions do not reconcile: {errors[:5]}")
    rows.sort(key=lambda r: (r["referendum_date"], r["teryt_powiat"], r["teryt_gmina"], r["question_no"]))
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len({(r['term'], r['referendum_id']) for r in rows})} referendums, {len(rows)} questions -> {args.out}")


if __name__ == "__main__":
    main()
