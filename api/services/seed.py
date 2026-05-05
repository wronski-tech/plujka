from __future__ import annotations

import csv
import json
import re
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
    non_committee_tokens = ("kod", "teryt", "gmina", "powiat", "wojew", "okręg", "okr", "nr ", "liczba", "symbol")
    return not any(token in lowered for token in non_committee_tokens)


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
    return [
        (2019, Path("data/pkw_all/sejmsenat2019/csv/wyniki_gl_na_listy_po_obwodach_sejm.csv")),
        (2023, Path("data/pkw_all/sejmsenat2023/csv/wyniki_gl_na_listy_po_obwodach_sejm.csv")),
        (2023, Path(SEED_SAMPLE_CSV)),
    ]


def _import_dataset(csv_path: Path, year: int) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        rows = list(reader)
        if not rows:
            return

    committees = [col for col in reader.fieldnames or [] if col not in FIXED_COLUMNS and col.strip()]

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


def seed_if_empty() -> None:
    profile_all_csv_sources()
    for year, csv_path in _dataset_candidates():
        if csv_path.exists():
            _import_dataset(csv_path, year)
