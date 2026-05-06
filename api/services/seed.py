from __future__ import annotations

import csv
import json
import re
import threading
from pathlib import Path

from api.services import db, kbw_catalog, kbw_import
from api.services.config import SEED_SAMPLE_CSV

_seed_lock = threading.Lock()

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
# Mandates per Sejm district (2019 apportionment; unchanged for 2023 — 41 districts, 460 seats).
SEJM_DISTRICT_SEATS = {
    "1": 12, "2": 8, "3": 14, "4": 12, "5": 13, "6": 15, "7": 12, "8": 12, "9": 14, "10": 9,
    "11": 12, "12": 8, "13": 14, "14": 10, "15": 9, "16": 10, "17": 12, "18": 12, "19": 9, "20": 12,
    "21": 20, "22": 11, "23": 15, "24": 14, "25": 12, "26": 14, "27": 9, "28": 7, "29": 9, "30": 9,
    "31": 10, "32": 9, "33": 8, "34": 8, "35": 9, "36": 12, "37": 9, "38": 9, "39": 13, "40": 8, "41": 12,
}

# 2023 candidate CSV uses abbreviated committee labels in column titles; list-level data uses full PKW names.
SEJM_2023_COLUMN_COMMITTEE_TO_LIST_NAME: dict[str, str] = {
    "KW BEZPARTYJNI SAMORZĄDOWCY": "KOMITET WYBORCZY BEZPARTYJNI SAMORZĄDOWCY",
    "KKW TRZECIA DROGA PSL-PL2050 SZYMONA HOŁOWNI": (
        "KOALICYJNY KOMITET WYBORCZY TRZECIA DROGA POLSKA 2050 SZYMONA HOŁOWNI - POLSKIE STRONNICTWO LUDOWE"
    ),
    "KW NOWA LEWICA": "KOMITET WYBORCZY NOWA LEWICA",
    "KW PRAWO I SPRAWIEDLIWOŚĆ": "KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ",
    "KW KONFEDERACJA WOLNOŚĆ I NIEPODLEGŁOŚĆ": "KOMITET WYBORCZY KONFEDERACJA WOLNOŚĆ I NIEPODLEGŁOŚĆ",
    "KKW KOALICJA OBYWATELSKA PO .N IPL ZIELONI": (
        "KOALICYJNY KOMITET WYBORCZY KOALICJA OBYWATELSKA PO .N IPL ZIELONI"
    ),
    "KW POLSKA JEST JEDNA": "KOMITET WYBORCZY POLSKA JEST JEDNA",
    "KWW MNIEJSZOŚĆ NIEMIECKA": "KOMITET WYBORCZY WYBORCÓW MNIEJSZOŚĆ NIEMIECKA",
    "KWW RDIP": "KOMITET WYBORCZY WYBORCÓW RUCHU DOBROBYTU I POKOJU",
}


def _parse_int(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


def _parse_pl_float(value: str) -> float | None:
    raw = (value or "").strip().strip('"')
    if not raw:
        return None
    normalized = raw.replace(" ", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


SENATE_VALID_VOTES_TOTAL_COL = "Liczba głosów ważnych oddanych łącznie na wszystkich kandydatów"

# Sejm list aggregates (official rollups). Obwód-level detail lives in `results` from list-by-precinct import.
SEJM_AGGREGATE_FILE_STEMS: list[tuple[str, str, bool]] = [
    ("po_wojewodztwach_sejm", "voivodeship", False),
    ("po_wojewodztwach_proc_sejm", "voivodeship", True),
    ("po_powiatach_sejm", "powiat", False),
    ("po_powiatach_proc_sejm", "powiat", True),
    ("po_gminach_sejm", "gmina", False),
    ("po_gminach_proc_sejm", "gmina", True),
    ("po_okregach_sejm", "district", False),
    ("po_okregach_proc_sejm", "district", True),
]


def _sejm_aggregate_csv_path(year: int, stem: str) -> Path:
    base = Path(f"data/pkw_all/sejmsenat{year}/csv")
    if year == 2019:
        return base / f"wyniki_gl_na_listy_{stem}.csv"
    return base / f"wyniki_gl_na_listy_{stem}_utf8.csv"


def _iter_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.reader(file, delimiter=";")
        header = [c.replace("\ufeff", "").strip('"') for c in next(reader, [])]
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))}
            rows.append(row)
        return header, rows


def _aggregate_geo_dims(level: str, row: dict[str, str]) -> dict[str, str]:
    if level == "voivodeship":
        return {
            "sejm_district": "",
            "teryt": "",
            "gmina": "",
            "powiat": "",
            "wojewodztwo": (row.get("Województwo") or "").strip(),
        }
    if level == "powiat":
        return {
            "sejm_district": "",
            "teryt": (row.get("Kod TERYT") or "").strip(),
            "gmina": "",
            "powiat": (row.get("Powiat") or "").strip(),
            "wojewodztwo": (row.get("Województwo") or "").strip(),
        }
    if level == "gmina":
        return {
            "sejm_district": _extract_district(row),
            "teryt": (row.get("TERYT Gminy") or "").strip(),
            "gmina": (row.get("Gmina") or "").strip(),
            "powiat": (row.get("Powiat") or "").strip(),
            "wojewodztwo": (row.get("Województwo") or "").strip(),
        }
    if level == "district":
        return {
            "sejm_district": _extract_district(row),
            "teryt": "",
            "gmina": "",
            "powiat": "",
            "wojewodztwo": "",
        }
    return {"sejm_district": "", "teryt": "", "gmina": "", "powiat": "", "wojewodztwo": ""}


def _import_sejm_aggregates(year: int) -> None:
    election_id: int | None = None
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM elections WHERE year = %s AND type = %s LIMIT 1",
                (year, "sejm"),
            )
            row_e = cur.fetchone()
            if not row_e:
                return
            election_id = row_e[0]
            cur.execute(
                "SELECT COUNT(*) FROM sejm_aggregate_results WHERE election_id = %s",
                (election_id,),
            )
            if cur.fetchone()[0] > 0:
                return

    assert election_id is not None

    for stem, geography_level, is_proc in SEJM_AGGREGATE_FILE_STEMS:
        path = _sejm_aggregate_csv_path(year, stem)
        if not path.is_file():
            continue
        header, rows = _iter_csv_rows(path)
        committees = [c for c in header if _is_committee_column(c)]
        if not committees:
            continue
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    dims = _aggregate_geo_dims(geography_level, row)
                    for committee in committees:
                        raw_val = (row.get(committee) or "").strip()
                        if not raw_val:
                            continue
                        if is_proc:
                            metric = _parse_pl_float(raw_val)
                        else:
                            metric = float(_parse_int(raw_val))
                        if metric is None:
                            continue
                        cur.execute(
                            """
                            INSERT INTO sejm_aggregate_results (
                                election_id, geography_level, sejm_district, teryt, gmina, powiat,
                                wojewodztwo, committee_name, metric_value, is_percentage
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (
                                election_id, geography_level, sejm_district, teryt, gmina, powiat,
                                wojewodztwo, committee_name, is_percentage
                            ) DO UPDATE SET metric_value = EXCLUDED.metric_value
                            """,
                            (
                                election_id,
                                geography_level,
                                dims["sejm_district"],
                                dims["teryt"],
                                dims["gmina"],
                                dims["powiat"],
                                dims["wojewodztwo"],
                                committee,
                                metric,
                                is_proc,
                            ),
                        )


def _senate_candidate_columns(header: list[str]) -> list[tuple[int, str]]:
    try:
        start = header.index(SENATE_VALID_VOTES_TOTAL_COL) + 1
    except ValueError:
        return []
    out: list[tuple[int, str]] = []
    for idx in range(start, len(header)):
        name = header[idx].strip().strip('"')
        if name:
            out.append((idx, name))
    return out


def _import_senate_obwody(year: int) -> None:
    base = Path(f"data/pkw_all/sejmsenat{year}/csv")
    if not base.is_dir():
        return

    paths = sorted(p for p in base.glob("wyniki_gl_na_kand_po_obwodach_senat_okr_*.csv") if "_utf8" not in p.name)
    if not paths:
        return

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM elections WHERE year = %s AND type = %s LIMIT 1",
                (year, "senate"),
            )
            row_e = cur.fetchone()
            if row_e:
                election_id = row_e[0]
            else:
                cur.execute(
                    "INSERT INTO elections (year, type) VALUES (%s, %s) RETURNING id",
                    (year, "senate"),
                )
                election_id = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM senate_results WHERE election_id = %s",
                (election_id,),
            )
            if cur.fetchone()[0] > 0:
                return

            for csv_path in paths:
                district_match = re.search(r"_okr_(\d+)", csv_path.name)
                senate_district = district_match.group(1) if district_match else "unknown"
                with csv_path.open("r", encoding="utf-8", newline="") as file:
                    reader = csv.reader(file, delimiter=";")
                    header = [c.replace("\ufeff", "").strip('"') for c in next(reader, [])]
                    cand_cols = _senate_candidate_columns(header)
                    if not cand_cols:
                        continue
                    key_symbol = header.index("Symbol kontrolny") if "Symbol kontrolny" in header else 0
                    idx_teryt = header.index("Kod TERYT") if "Kod TERYT" in header else None
                    idx_numer = header.index("Numer") if "Numer" in header else None
                    idx_gmina = header.index("Gmina") if "Gmina" in header else None
                    idx_powiat = header.index("Powiat") if "Powiat" in header else None
                    idx_woj = header.index("Województwo") if "Województwo" in header else None

                    for raw in reader:
                        symbol = (raw[key_symbol] if key_symbol < len(raw) else "").strip().strip('"')
                        if not symbol:
                            continue
                        teryt = (raw[idx_teryt] if idx_teryt is not None and idx_teryt < len(raw) else "") or ""
                        numer = (raw[idx_numer] if idx_numer is not None and idx_numer < len(raw) else "") or ""
                        gmina = (raw[idx_gmina] if idx_gmina is not None and idx_gmina < len(raw) else "") or ""
                        powiat = (raw[idx_powiat] if idx_powiat is not None and idx_powiat < len(raw) else "") or ""
                        woj = (raw[idx_woj] if idx_woj is not None and idx_woj < len(raw) else "") or ""
                        for idx, cand_name in cand_cols:
                            if idx >= len(raw):
                                continue
                            votes = _parse_int(raw[idx])
                            if votes <= 0:
                                continue
                            cur.execute(
                                """
                                INSERT INTO senate_results (
                                    election_id, senate_district, symbol_kontrolny, teryt, numer_obwodu,
                                    gmina, powiat, wojewodztwo, candidate_name, votes
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (election_id, symbol_kontrolny, candidate_name) DO UPDATE
                                SET votes = EXCLUDED.votes
                                """,
                                (
                                    election_id,
                                    senate_district,
                                    symbol,
                                    teryt.strip(),
                                    numer.strip(),
                                    gmina.strip(),
                                    powiat.strip(),
                                    woj.strip(),
                                    cand_name,
                                    votes,
                                ),
                            )


def _extract_district(row: dict[str, str]) -> str:
    candidates = [
        "Nr okręgu",
        "Numer okręgu",
        "Okręg",
        "Okreg",
        "Numer",
    ]
    for key in candidates:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return "unknown"


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


def _list_committee_from_2023_candidate_suffix(committee_raw: str) -> str:
    committee_raw = committee_raw.strip().strip('"')
    mapped = SEJM_2023_COLUMN_COMMITTEE_TO_LIST_NAME.get(committee_raw)
    if mapped:
        return mapped
    upper = committee_raw.upper()
    if upper.startswith("KW "):
        return "KOMITET WYBORCZY " + committee_raw[3:].strip()
    return committee_raw


def _extract_2023_candidate_votes_by_district() -> dict[tuple[str, str], list[tuple[str, int, int]]]:
    base = Path("data/pkw_all/sejmsenat2023/csv/wyniki_gl_na_kandydatow_po_gminach_sejm_csv")
    if not base.is_dir():
        return {}

    result: dict[tuple[str, str], dict[tuple[str, int], int]] = {}
    for csv_path in sorted(base.glob("okreg_*_utf8.csv")):
        district_match = re.search(r"okreg_(\d+)_utf8\.csv$", csv_path.name, re.IGNORECASE)
        if not district_match:
            continue
        district = district_match.group(1)
        with csv_path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.reader(file, delimiter=";")
            header = [c.replace("\ufeff", "").strip('"') for c in next(reader, [])]

            position_by_committee: dict[str, int] = {}
            candidate_cols: list[tuple[int, str, str, int]] = []
            for idx, col in enumerate(header):
                if idx < 6:
                    continue
                raw = col.strip()
                if " - " not in raw:
                    continue
                name_part, com_raw = raw.rsplit(" - ", 1)
                committee = _list_committee_from_2023_candidate_suffix(com_raw)
                pos = position_by_committee.get(committee, 0) + 1
                position_by_committee[committee] = pos
                candidate_cols.append((idx, name_part.strip(), committee, pos))

            for row in reader:
                for idx, candidate_name, committee, position in candidate_cols:
                    if idx >= len(row):
                        continue
                    votes = _parse_int(row[idx])
                    if votes <= 0:
                        continue
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


def _seed_sejm_candidate_ballots(
    year: int,
    candidate_votes: dict[tuple[str, str], list[tuple[str, int, int]]],
) -> None:
    """All list candidates who received votes in gmina CSVs (not only mandate winners)."""
    if not candidate_votes:
        return

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sejm_candidate_ballots WHERE year = %s", (year,))
            for (district, committee), ranked in candidate_votes.items():
                for candidate_name, position, votes in ranked:
                    if votes <= 0:
                        continue
                    cur.execute(
                        """
                        INSERT INTO sejm_candidate_ballots
                            (year, district, committee_name, candidate_name, list_position, total_votes)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (year, district, committee_name, candidate_name)
                        DO UPDATE SET
                            total_votes = EXCLUDED.total_votes,
                            list_position = EXCLUDED.list_position
                        """,
                        (year, district, committee, candidate_name, position, votes),
                    )


def _seed_elected_candidates(
    year: int,
    candidate_votes: dict[tuple[str, str], list[tuple[str, int, int]]],
) -> None:
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
                WHERE e.year = %s AND e.type = 'sejm'
                GROUP BY r.district, c.name
                """,
                (year,),
            )
            district_committee_votes_rows = cur.fetchall()

            cur.execute(
                """
                SELECT c.name, SUM(r.votes) AS votes
                FROM results r
                JOIN elections e ON e.id = r.election_id
                JOIN candidates c ON c.id = r.candidate_id
                WHERE e.year = %s AND e.type = 'sejm'
                GROUP BY c.name
                """,
                (year,),
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

            cur.execute("DELETE FROM elected_candidates WHERE year = %s", (year,))

            for district, seats in SEJM_DISTRICT_SEATS.items():
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
                            (year, district, committee, candidate_name, votes, position),
                        )


def clear_election_seed_data() -> None:
    """Remove rows produced by the PKW seed so imports can run again from CSV files.

    Also clears KBW mirror fact tables so a forced reseed can reload them from disk.
    """
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM results")
            cur.execute("DELETE FROM sejm_aggregate_results")
            cur.execute("DELETE FROM senate_results")
            cur.execute("DELETE FROM sejm_candidate_ballots")
            cur.execute("DELETE FROM elected_candidates")
            cur.execute("DELETE FROM elections")
            cur.execute("DELETE FROM kbw_facts")
            cur.execute("DELETE FROM kbw_election_runs")


def _run_seed_pipeline(*, import_kbw_facts: bool = False) -> None:
    profile_all_csv_sources()
    kbw_catalog.profile_mirror_if_present()
    for year, csv_path in _dataset_candidates():
        if csv_path.exists():
            _import_dataset(csv_path, year)
    _import_sejm_aggregates(2019)
    _import_sejm_aggregates(2023)
    _import_senate_obwody(2019)
    votes_2019 = _extract_2019_candidate_votes_by_district()
    if votes_2019:
        _seed_sejm_candidate_ballots(2019, votes_2019)
        _seed_elected_candidates(2019, votes_2019)
    votes_2023 = _extract_2023_candidate_votes_by_district()
    if votes_2023:
        _seed_sejm_candidate_ballots(2023, votes_2023)
        _seed_elected_candidates(2023, votes_2023)

    if import_kbw_facts and kbw_catalog.DEFAULT_KBW_ROOT.is_dir():
        kbw_import.import_all_kbw_csv_facts(root=kbw_catalog.DEFAULT_KBW_ROOT)


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
                district = _extract_district(row)
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


def seed_if_empty(*, force: bool = False) -> None:
    with _seed_lock:
        if force:
            seed_complete.clear()
        try:
            if force:
                clear_election_seed_data()
            _run_seed_pipeline(import_kbw_facts=force)
        finally:
            seed_complete.set()


def reseed_from_disk() -> None:
    """Same as seed_if_empty(force=True); for explicit API/thread calls."""
    seed_if_empty(force=True)
