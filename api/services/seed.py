from __future__ import annotations

import csv
import json
import re
import threading
from pathlib import Path

from api.services import db
from api.services.config import SEED_SAMPLE_CSV

FIXED_COLUMNS = {
    "Nr komisji",
    "Siedziba",
    "TERYT Gminy",
    "Gmina",
    "Powiat",
    "Województwo",
    "Nr okręgu",
    "Liczba komisji",
    "Liczba uwzględnionych komisji",
    "Komisja otrzymała kart do głosowania",
    "Liczba wyborców uprawnionych do głosowania",
    "Nie wykorzystano kart do głosowania",
    "Liczba wyborców, którym wydano karty do głosowania",
    "Liczba wyborców, którym wysłano pakiety wyborcze",
    "Liczba wyborców, którym wydano karty do głosowania w lokalu wyborczym oraz w głosowaniu korespondencyjnym (łącznie)",
    "Liczba wyborców głosujących przez pełnomocnika",
    "Liczba wyborców głosujących na podstawie zaświadczenia o prawie do głosowania",
    "Liczba otrzymanych kopert zwrotnych",
    "Liczba kopert zwrotnych, w których nie było oświadczenia o osobistym i tajnym oddaniu głosu",
    "Liczba kopert zwrotnych, w których oświadczenie nie było podpisane",
    "Liczba kopert zwrotnych, w których nie było koperty na kartę do głosowania",
    "Liczba kopert zwrotnych, w których znajdowała się niezaklejona koperta na kartę do głosowania",
    "Liczba kopert na kartę do głosowania wrzuconych do urny",
    "Liczba kart wyjętych z urny",
    "W tym liczba kart wyjętych z kopert na kartę do głosowania",
    "Liczba kart nieważnych",
    "Liczba kart ważnych",
    "Liczba głosów nieważnych",
    "W tym z powodu postawienia znaku „X” obok nazwiska dwóch lub większej liczby kandydatów z różnych list",
    "W tym z powodu niepostawienia znaku „X” obok nazwiska żadnego kandydata",
    "W tym z powodu postawienia znaku „X” wyłącznie obok nazwiska kandydata na liście, której rejestracja została unieważniona",
    "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
}


ELECTION_PATH_RE = re.compile(r"(sejmsenat20\d{2})")
LIST_POSITION_RE = re.compile(r"\s-\s*nr na liście\s*(\d+)\s*$", re.IGNORECASE)
SEJM_2019_DISTRICT_SEATS = {
    "1": 12, "2": 8, "3": 14, "4": 12, "5": 13, "6": 15, "7": 12, "8": 12, "9": 14, "10": 9,
    "11": 12, "12": 8, "13": 14, "14": 10, "15": 9, "16": 10, "17": 12, "18": 12, "19": 9, "20": 12,
    "21": 20, "22": 11, "23": 15, "24": 14, "25": 12, "26": 14, "27": 9, "28": 7, "29": 9, "30": 9,
    "31": 10, "32": 9, "33": 8, "34": 8, "35": 9, "36": 12, "37": 9, "38": 9, "39": 13, "40": 8, "41": 12,
}


def _parse_int(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


def _detect_year_from_path(path: Path) -> int | None:
    match = ELECTION_PATH_RE.search(str(path))
    if not match:
        return None
    year_match = re.search(r"20\d{2}", match.group(1))
    return int(year_match.group(0)) if year_match else None


def _is_committee_column(name: str) -> bool:
    lowered = name.strip().lower()
    if not lowered:
        return False
    if name in FIXED_COLUMNS:
        return False
    non_committee_tokens = (
        "kod",
        "teryt",
        "gmina",
        "powiat",
        "wojew",
        "okręg",
        "okreg",
        "nr ",
        "numer",
        "symbol",
        "rodzaj",
        "typ",
        "siedziba",
        "komisj",
        "kart",
        "kopert",
        "urn",
        "głosów",
        "glosow",
        "mandat",
    )
    if any(token in lowered for token in non_committee_tokens):
        return False

    committee_markers = (
        "komitet",
        "koalicyjny",
        "wyborców",
        "wyborcow",
        "bezpartyjni",
        "mniejszość",
        "mniejszosc",
    )
    return any(marker in lowered for marker in committee_markers)


def profile_all_csv_sources() -> None:
    root = Path("data/pkw_all")
    if not root.exists():
        return

    csv_paths = sorted(root.rglob("*.csv"))
    if not csv_paths:
        return

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            for csv_path in csv_paths:
                try:
                    with csv_path.open("r", encoding="utf-8", newline="") as file:
                        reader = csv.reader(file, delimiter=";")
                        header = next(reader, [])
                except Exception:
                    continue

                normalized_header = [column.replace("\ufeff", "").strip('"') for column in header]
                year = _detect_year_from_path(csv_path)
                election_key_match = ELECTION_PATH_RE.search(str(csv_path))
                election_key = election_key_match.group(1) if election_key_match else "unknown"
                rel_path = str(csv_path)

                cur.execute(
                    """
                    INSERT INTO source_files (election_key, year, file_path, file_name, column_count, header)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (file_path) DO UPDATE
                    SET election_key = EXCLUDED.election_key,
                        year = EXCLUDED.year,
                        file_name = EXCLUDED.file_name,
                        column_count = EXCLUDED.column_count,
                        header = EXCLUDED.header,
                        profiled_at = NOW()
                    RETURNING id
                    """,
                    (
                        election_key,
                        year,
                        rel_path,
                        csv_path.name,
                        len(normalized_header),
                        json.dumps(normalized_header),
                    ),
                )
                source_file_id = cur.fetchone()[0]
                cur.execute("DELETE FROM source_columns WHERE source_file_id = %s", (source_file_id,))

                for index, column_name in enumerate(normalized_header):
                    cur.execute(
                        """
                        INSERT INTO source_columns (source_file_id, column_index, column_name, is_committee)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (source_file_id, index, column_name, _is_committee_column(column_name)),
                    )


def _dataset_candidates() -> list[tuple[int, Path]]:
    candidates: list[tuple[int, Path]] = []
    p2019 = Path("data/pkw_all/sejmsenat2019/csv/wyniki_gl_na_listy_po_obwodach_sejm.csv")
    p2023 = Path("data/pkw_all/sejmsenat2023/csv/wyniki_gl_na_listy_po_obwodach_sejm.csv")
    p2023_utf8 = Path("data/pkw_all/sejmsenat2023/csv/wyniki_gl_na_listy_po_obwodach_sejm_utf8.csv")

    if p2019.exists():
        candidates.append((2019, p2019))
    if p2023.exists():
        candidates.append((2023, p2023))
    elif p2023_utf8.exists():
        candidates.append((2023, p2023_utf8))

    sample = Path(SEED_SAMPLE_CSV)
    if sample.exists():
        candidates.append((2023, sample))
    return candidates


def _extract_2019_candidate_votes_by_district() -> dict[tuple[str, str], list[tuple[str, int, int]]]:
    base = Path("data/pkw_all/sejmsenat2019/csv")
    result: dict[tuple[str, str], dict[tuple[str, int], int]] = {}
    for csv_path in sorted(base.glob("wyniki_gl_na_kand_po_gminach_sejm_okr_*_utf8.csv")):
        district_match = re.search(r"_okr_(\d+)_", csv_path.name)
        if not district_match:
            continue
        district = district_match.group(1)
        with csv_path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.reader(file, delimiter=";")
            header = [c.replace("\ufeff", "").strip('"') for c in next(reader, [])]

            committee_for_col: dict[int, str] = {}
            candidate_for_col: dict[int, tuple[str, int]] = {}
            current_committee = ""
            for idx, col in enumerate(header):
                if _is_committee_column(col):
                    current_committee = col
                    committee_for_col[idx] = col
                    continue
                pos_match = LIST_POSITION_RE.search(col)
                if pos_match and current_committee:
                    candidate_name = LIST_POSITION_RE.sub("", col).strip()
                    candidate_for_col[idx] = (candidate_name, int(pos_match.group(1)))

            for row in reader:
                for idx, (candidate_name, position) in candidate_for_col.items():
                    if idx >= len(row):
                        continue
                    votes = _parse_int(row[idx])
                    if votes <= 0:
                        continue
                    committee = current_committee
                    # Find nearest committee column to the left.
                    left_idxs = [c_idx for c_idx in committee_for_col.keys() if c_idx < idx]
                    if not left_idxs:
                        continue
                    committee = committee_for_col[max(left_idxs)]
                    key = (district, committee)
                    result.setdefault(key, {})
                    cand_key = (candidate_name, position)
                    result[key][cand_key] = result[key].get(cand_key, 0) + votes

    flattened: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    for key, data in result.items():
        flattened[key] = [
            (name, position, votes)
            for (name, position), votes in sorted(data.items(), key=lambda item: (-item[1], item[0][1], item[0][0]))
        ]
    return flattened


def _seed_elected_candidates_2019() -> None:
    candidate_votes = _extract_2019_candidate_votes_by_district()
    if not candidate_votes:
        return

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.district, c.name, SUM(r.votes) AS votes
                FROM results r
                JOIN elections e ON e.id = r.election_id
                JOIN candidates c ON c.id = r.candidate_id
                WHERE e.year = 2019 AND e.type = 'sejm'
                GROUP BY r.district, c.name
                """
            )
            district_committee_votes_rows = cur.fetchall()

            cur.execute(
                """
                SELECT c.name, SUM(r.votes) AS votes
                FROM results r
                JOIN elections e ON e.id = r.election_id
                JOIN candidates c ON c.id = r.candidate_id
                WHERE e.year = 2019 AND e.type = 'sejm'
                GROUP BY c.name
                """
            )
            national_rows = cur.fetchall()

            total_national_votes = sum(row[1] for row in national_rows) or 1
            eligible_committees: set[str] = set()
            for committee, votes in national_rows:
                upper = committee.upper()
                if "MNIEJSZOŚĆ NIEMIECKA" in upper or "MNIEJSZOSC NIEMIECKA" in upper:
                    eligible_committees.add(committee)
                    continue
                threshold = 0.08 if "KOALICYJNY" in upper else 0.05
                if votes / total_national_votes >= threshold:
                    eligible_committees.add(committee)

            district_votes: dict[str, dict[str, int]] = {}
            for district, committee, votes in district_committee_votes_rows:
                if committee not in eligible_committees:
                    continue
                district_votes.setdefault(str(district), {})
                district_votes[str(district)][committee] = int(votes)

            cur.execute("DELETE FROM elected_candidates WHERE year = 2019")

            for district, seats in SEJM_2019_DISTRICT_SEATS.items():
                committee_votes = district_votes.get(district, {})
                if not committee_votes:
                    continue
                quotients: list[tuple[float, str]] = []
                for committee, votes in committee_votes.items():
                    for divisor in range(1, seats + 1):
                        quotients.append((votes / divisor, committee))
                quotients.sort(key=lambda item: item[0], reverse=True)
                top = quotients[:seats]
                mandates_by_committee: dict[str, int] = {}
                for _, committee in top:
                    mandates_by_committee[committee] = mandates_by_committee.get(committee, 0) + 1

                for committee, mandates in mandates_by_committee.items():
                    ranked = candidate_votes.get((district, committee), [])
                    winners = ranked[:mandates]
                    for candidate_name, position, votes in winners:
                        cur.execute(
                            """
                            INSERT INTO elected_candidates
                                (year, district, committee_name, candidate_name, candidate_votes, list_position)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (year, district, committee_name, candidate_name) DO UPDATE
                            SET candidate_votes = EXCLUDED.candidate_votes,
                                list_position = EXCLUDED.list_position
                            """,
                            (2019, district, committee, candidate_name, votes, position),
                        )


def _import_dataset(csv_path: Path, year: int) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        rows = list(reader)
        if not rows:
            return

    committees = [col for col in reader.fieldnames or [] if _is_committee_column(col)]

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM elections WHERE year = %s AND type = %s LIMIT 1", (year, "sejm"))
            existing = cur.fetchone()
            if existing:
                election_id = existing[0]
            else:
                cur.execute(
                    "INSERT INTO elections (year, type) VALUES (%s, %s) RETURNING id",
                    (year, "sejm"),
                )
                election_id = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM results WHERE election_id = %s", (election_id,))
            if cur.fetchone()[0] > 0:
                return

            committee_to_id: dict[str, int] = {}
            for committee in committees:
                cur.execute(
                    """
                    INSERT INTO candidates (name)
                    VALUES (%s)
                    ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id
                    """,
                    (committee,),
                )
                committee_to_id[committee] = cur.fetchone()[0]

            for row in rows:
                district = row.get("Nr okręgu", "").strip() or "unknown"
                for committee in committees:
                    candidate_id = committee_to_id.get(committee)
                    if not candidate_id:
                        continue
                    votes = _parse_int(row.get(committee, "0"))
                    cur.execute(
                        """
                        INSERT INTO results (candidate_id, election_id, votes, district, list_position)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (candidate_id, election_id, votes, district, 1),
                    )


seed_complete = threading.Event()


def seed_if_empty() -> None:
    try:
        profile_all_csv_sources()
        for year, csv_path in _dataset_candidates():
            if csv_path.exists():
                _import_dataset(csv_path, year)
        _seed_elected_candidates_2019()
    finally:
        seed_complete.set()
