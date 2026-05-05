from __future__ import annotations

import csv
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


def _parse_int(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


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
    for year, csv_path in _dataset_candidates():
        if csv_path.exists():
            _import_dataset(csv_path, year)
