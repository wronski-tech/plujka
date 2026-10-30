#!/usr/bin/env python3
"""Scrape the results of Senate by-elections (wybory uzupełniające do Senatu) from pkw.gov.pl.

A by-election fills one seat that became vacant during a term, in one Senate district. The KBW data
files do not cover them; PKW publishes each result only as an announcement (obwieszczenie o wynikach
wyborów uzupełniających) on pkw.gov.pl: as page text (2003-2006), RTF (2004, four districts) or PDF
(2004 onwards). This script reads every announcement listed under
https://pkw.gov.pl/wybory-i-referenda/wybory-do-sejmu-i-do-senatu/wybory-uzupelniajace-do-senatu-rp
and writes ``data/pkw_html/senat_byelections.csv`` (committed source data, read by ``derived.py``).

Output columns: election_date, district_no, wojewodztwo, eligible_voters, ballots_issued, valid_votes
(per district, repeated on every candidate row), candidate_no (ballot order), last_name, first_names,
committee_name, votes, elected, source_url. Every district is checked before writing: candidate votes
sum to the valid votes and the elected candidate (named in the announcement) has the most votes.
Announcements of by-elections that have not taken place yet are skipped.

Needs ``pypdf`` (``pip install pypdf``); any HTTP or parsing error stops the run.

Usage:
  python3 scripts/scrape_senat_byelections.py [--out data/pkw_html/senat_byelections.csv] [--delay 0.3]
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://pkw.gov.pl"
INDEX = "/wybory-i-referenda/wybory-do-sejmu-i-do-senatu/wybory-uzupelniajace-do-senatu-rp"
USER_AGENT = "plujka-senat-byelections/1.0 (+https://github.com/wronski-tech/plujka)"
OUT = ROOT / "data" / "pkw_html" / "senat_byelections.csv"
MONTHS = {m: i for i, m in enumerate(("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia",
                                      "września", "października", "listopada", "grudnia"), 1)}
NUMBER = r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)"
UPPER = "A-ZĄĆĘŁŃÓŚŹŻ"


class Fetcher:
    """Sequential HTTP GET with a fixed delay between requests."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def get(self, path: str) -> bytes:
        url = path if path.startswith("http") else BASE_URL + quote(path)
        time.sleep(self.delay)
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as response:
            return response.read()

    def page(self, path: str) -> str:
        return self.get(path).decode("utf-8")


def links(page: str, prefix: str) -> list[str]:
    """Distinct links of ``page`` below ``prefix``, in page order."""
    found = [html.unescape(h) for h in re.findall(r'href="([^"]+)"', page)]
    return list(dict.fromkeys(h for h in found if h.startswith(prefix + "/")))


def page_text(page: str) -> str:
    """Text of the article part of a pkw.gov.pl page (title to the change register)."""
    start = page.find("<h1")
    end = page.find("Rejestr zmian", start)
    body = page[start:end if end > 0 else len(page)]
    body = re.sub(r"(?i)<(br|/p|/tr|/li|/div|/h\d)[^>]*>", "\n", body)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def rtf_text(data: bytes) -> str:
    """Plain text of a simple RTF document in code page 1250 (control words dropped)."""
    text = data.decode("latin-1").replace("\r", "").replace("\n", "")
    text = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes([int(m[1], 16)]).decode("cp1250"), text)
    text = re.sub(r"\\par(?![a-z])", "\n", text.replace("\\~", " ").replace("\\tab", " "))
    text = re.sub(r"\\[a-z]+-?\d* ?", "", text)
    return re.sub(r"[{}]|\\\*", "", text)


def to_int(value: str) -> int:
    return int(re.sub(r"\D", "", value))


def parse_district(text: str, district_no: int) -> dict:
    """Totals and candidates of one district's part of an announcement."""
    flat = re.sub(r"\s+", " ", text)
    eligible = re.search(r"uprawnionych do głosowania wyn\w+:? " + NUMBER, flat)
    issued = (re.search(r"\(łącznie\):? (?:wyniosła:? )?" + NUMBER, flat)
              or re.search(r"Karty do głosowania wydano " + NUMBER, flat)
              or re.search(r"wydano karty do głosowania\)?(?: w lokalach wyborczych)?:? (?:wyniosła:? )?" + NUMBER, flat))
    valid = re.search(r"(?:kart ważnych z głosami ważnymi|[Ll]iczba głosów ważnych) wyn\w+:? " + NUMBER, flat)
    block = re.search(r"(?:uzyskali|uzyskał|otrzymali|otrzymał)\s+następującą\s+liczbę\s+głosów\s+ważnych:(.*?)"
                      r"(?:\n\s*(?:\d+\.\s+)?W\s+województwie|wybran[ya]|$)", text, re.S)
    if not (eligible and valid and block):
        raise RuntimeError(f"district {district_no}: totals or candidate list not found")
    candidates: list[dict] = []
    # Join a bare 'N.' with the name after it, and a name with the votes on the next line (2007, 2008).
    lines: list[str] = []
    for line in (raw.strip() for raw in block[1].splitlines()):
        if line and lines and (re.fullmatch(r"\d+[.)]", lines[-1]) or (
                re.fullmatch(NUMBER, line) and not re.search(r"\d$", lines[-1]) and not lines[-1].startswith("zgłoszon"))):
            lines[-1] += " " + line
        elif line:
            lines.append(line)
    for line in lines:
        # 'N. Name votes', 'N) Name votes' or 'Name votes' (2003), then 'zgłoszony przez <committee>' lines.
        m = re.fullmatch(rf"(?:\d+[.)]\s*)?([{UPPER}][^\d]*?)\s+{NUMBER}", line)
        if not m or m[1].startswith(("Komitet", "Koalicyjny", "KW")):
            if candidates:  # committee text, possibly over several lines
                candidates[-1]["committee_name"] = " ".join(filter(None, [candidates[-1]["committee_name"], line]))
            continue
        votes = m[2]
        name = m[1].replace(",", " ").split()
        # 'JUREK Marek' (surname in capitals) or 'Jurgiel Krzysztof' (surname first, written like the given names).
        surname_end = next((j for j, part in enumerate(name) if not part.isupper()), len(name)) if len(name[0]) > 1 and name[0].isupper() else 1
        candidates.append({"candidate_no": len(candidates) + 1, "last_name": " ".join(name[:surname_end]).upper(),
                           "first_names": " ".join(name[surname_end:]), "committee_name": None, "votes": to_int(votes)})
    for c in candidates:
        # Drop the next chapter heading ('III. Wyniki wyborów') and page numbers ('3', '- 3 -') after the last committee.
        committee = re.sub(r"(?:\s+-?\s*\d+\s*-?)?(?:\s+III\. Wyniki wyborów)?$", "", c["committee_name"] or "")
        c["committee_name"] = re.sub(r"^zgłoszon[ya]\s+przez\s*", "", committee).strip(" .,;") or None
    tail = re.sub(r"\s+", " ", text[block.end(1):])
    elected_text = re.sub(r"\s+", " ", tail[:400]).upper()
    winner = max(candidates, key=lambda c: c["votes"]) if candidates else None
    if not winner or winner["last_name"] not in elected_text:
        raise RuntimeError(f"district {district_no}: elected candidate not found ({winner})")
    if sum(c["votes"] for c in candidates) != to_int(valid[1]):
        raise RuntimeError(f"district {district_no}: candidate votes {[c['votes'] for c in candidates]} do not sum to {valid[1]}")
    woj = re.search(r"[Ww] województwie ([\w-]+)", flat)
    return {
        "district_no": district_no, "wojewodztwo": re.sub(r"im$", "ie", woj[1]) if woj else None,
        "eligible_voters": to_int(eligible[1]), "ballots_issued": to_int(issued[1]) if issued else None,
        "valid_votes": to_int(valid[1]), "candidates": [{**c, "elected": c is winner} for c in candidates],
    }


def parse_announcement(text: str) -> tuple[str, list[dict]]:
    """Election date and districts of one results announcement."""
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    day = re.search(r"przeprowadzonych\s+w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if not day:
        raise RuntimeError("election date not found")
    date = f"{day[3]}-{MONTHS[day[2]]:02d}-{int(day[1]):02d}"
    starts = [m.start() for m in re.finditer(r"uprawnionych do głosowania wyn", text)]
    districts = []
    for i, start in enumerate(starts):
        part = text[start:starts[i + 1] if i + 1 < len(starts) else len(text)]
        # The district is named before its part (several districts) or only in the result sentence (2013-2025).
        named = re.findall(r"okręg\w*\s+wyborcz\w*\s+(?:nr\s+)?(\d+)", text[:start]) or re.findall(
            r"okręg\w*\s+wyborcz\w*\s+(?:nr\s+)?(\d+)", part)[:1]
        districts.append(parse_district(part, int(named[-1])))
    return date, districts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args()
    fetch = Fetcher(args.delay)
    events = [e for e in links(fetch.page(INDEX), INDEX) if e.count("/") == INDEX.count("/") + 2]
    rows = []
    for event in events:
        pages, todo = set(), [event]
        while todo:  # the event page, its numbered list pages and its 'obwieszczenia-i-komunikaty' page
            page = todo.pop()
            pages.add(page)
            todo += [p for p in links(fetch.page(page), event)
                     if p not in pages and p not in todo and re.search(r"/(\d+|obwieszczenia-i-komunikaty)$", p)]
        results = sorted({r for p in pages for r in links(fetch.page(p), event) if "o-wynikach-wyborow" in r})
        texts = {}
        for result in results:
            page = fetch.page(result)
            text = page_text(page)
            if "uprawnionych do głosowania" not in text:
                files = re.findall(r'href="([^"]*uploaded_files[^"]*\.(?:pdf|rtf))"', page)
                if not files:
                    continue
                data = fetch.get(html.unescape(files[0]))
                text += "\n" + (rtf_text(data) if files[0].endswith(".rtf") else "\n".join(
                    p.extract_text() for p in PdfReader(io.BytesIO(data)).pages))
            texts.setdefault(text, result)  # the same announcement can be published twice
        if not texts:
            print(f"skipped (no results announcement yet): {event}")
            continue
        for text, url in texts.items():
            try:
                date, districts = parse_announcement(text)
            except RuntimeError as exc:
                raise RuntimeError(f"{url}: {exc}") from exc
            for d in districts:
                if any(r["election_date"] == date and r["district_no"] == d["district_no"] for r in rows):
                    continue  # the same announcement published twice (2014)
                for c in d["candidates"]:
                    rows.append({"election_date": date, **{k: v for k, v in d.items() if k != "candidates"}, **c,
                                 "source_url": BASE_URL + url})
            print(f"{date}: districts {[d['district_no'] for d in districts]}")
    columns = ["election_date", "district_no", "wojewodztwo", "eligible_voters", "ballots_issued", "valid_votes",
               "candidate_no", "last_name", "first_names", "committee_name", "votes", "elected", "source_url"]
    rows.sort(key=lambda r: (r["election_date"], r["district_no"], r["candidate_no"]))
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} candidate rows -> {args.out}")


if __name__ == "__main__":
    main()
