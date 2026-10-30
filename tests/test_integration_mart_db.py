"""PostgreSQL checks of the mart (``PLUJKA_RUN_DB_TESTS=1``; needs CREATEROLE on DATABASE_URL).

The read-only role must deny writes, other schemas and long queries; person linking must split
namesakes by birth year, build local persons and apply manual overrides. The linking test fills the candidate tables
inside a transaction that is rolled back, so a built mart is left untouched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_RUN = os.environ.get("PLUJKA_RUN_DB_TESTS", "").strip().lower() in ("1", "true", "yes")


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestMartReaderRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        from api.services.config import DATABASE_URL
        from api.services.mart import schema

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS public.mart_role_probe (x int)")
            schema.ensure_schema(conn)
            schema.ensure_reader_role(conn, statement_timeout_ms=500)

    def test_reader_is_limited_to_read_only_mart(self) -> None:
        import psycopg

        from api.services.mart.safe_sql import run_readonly

        self.assertGreaterEqual(run_readonly("SELECT count(*) AS n FROM mart.persons", timeout_ms=500)[0]["n"], 0)
        denied = {
            "SELECT * FROM public.mart_role_probe": psycopg.errors.InsufficientPrivilege,
            "UPDATE mart.persons SET gender = gender": psycopg.errors.ReadOnlySqlTransaction,
            "SELECT pg_sleep(2)": psycopg.errors.QueryCanceled,
        }
        for sql, error in denied.items():
            with self.subTest(sql=sql), self.assertRaises(error):
                run_readonly(sql, timeout_ms=500)


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestPersonEndpointsDb(unittest.TestCase):
    """``GET /persons`` and ``GET /persons/{id}`` run their fixed queries as the reader on a built mart."""

    def test_timeline_matches_person_history(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import app
        from api.services.mart.safe_sql import run_readonly

        found = run_readonly(
            "SELECT p.person_id, p.last_name, p.candidacies FROM mart.entity_lexicon l"
            " JOIN mart.persons p ON p.person_id = l.ref::int WHERE l.kind = 'person'"
            " ORDER BY p.candidacies DESC, p.person_id LIMIT 1"
        )
        if not found:
            self.skipTest("mart has no persons; build it first")
        person = found[0]
        client = TestClient(app)
        body = client.get(f"/persons/{person['person_id']}").json()
        self.assertEqual(body["person"]["candidacies"], person["candidacies"])
        self.assertEqual(len(body["candidacies"]), person["candidacies"])
        dates = [c["election_date"] for c in body["candidacies"]]
        self.assertNotIn(None, dates)
        self.assertEqual(dates, sorted(dates))
        hits = client.get("/persons", params={"name": person["last_name"].lower(), "limit": 50}).json()["persons"]
        self.assertLessEqual(len(hits), 50)
        self.assertIn(person["person_id"], [h["person_id"] for h in hits])
        missing = run_readonly("SELECT coalesce(max(person_id), 0) + 1 AS n FROM mart.persons")[0]["n"]
        self.assertEqual(client.get(f"/persons/{missing}").status_code, 404)


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestBuildChecksDb(unittest.TestCase):
    """Every loaded election keeps the consistency checks it passed (``GET /checks``)."""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        from api.services.config import DATABASE_URL, MART_STATEMENT_TIMEOUT_MS
        from api.services.mart import schema

        # CI runs these tests before building the mart: the reader role may not exist yet.
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            schema.ensure_schema(conn)
            schema.ensure_reader_role(conn, statement_timeout_ms=MART_STATEMENT_TIMEOUT_MS)

    def test_every_loaded_election_has_passed_checks(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import app
        from api.services.mart.safe_sql import run_readonly

        elections = run_readonly("SELECT DISTINCT election, year FROM mart.elections ORDER BY 1, 2")
        if not elections:
            self.skipTest("mart has no elections; build it first")
        client = TestClient(app)
        for e in elections:
            with self.subTest(**e):
                checks = client.get(f"/checks/{e['election']}/{e['year']}").json()
                self.assertGreater(len(checks), 0)
                self.assertEqual({c["failing_rows"] for c in checks}, {0})


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestPersonLinking(unittest.TestCase):
    CANDIDATE_TABLES = ("sejm_candidates", "senat_candidates", "prezydent_candidates", "pe_candidates",
                        "sejmik_candidates", "wbp_candidates", "rada_candidates")

    def _link(self, conn, overrides: str) -> None:
        from api.services.mart import derived

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "person_overrides.csv"
            path.write_text("election,year,full_name,place,person,note\n" + overrides, encoding="utf-8")
            derived.link_persons(conn, path)

    def _fixture(self, conn) -> None:
        from api.services.mart import schema

        schema.ensure_schema(conn)
        conn.execute("TRUNCATE " + ", ".join(f"mart.{t}" for t in self.CANDIDATE_TABLES))
        sejm = [  # year, id, district, full name, last, first, gender
            (2019, 1, 1, "Jan KOWALSKI", "KOWALSKI", "Jan", "Mężczyzna"),
            (2023, 1, 5, "Jan KOWALSKI", "KOWALSKI", "Jan", "Mężczyzna"),
            (2023, 2, 3, "Anna NOWAK", "NOWAK", "Anna", "Kobieta"),
            (2015, 3, 7, "Marek LIS", "LIS", "Marek", "Mężczyzna"),
            (2019, 4, 2, "Ewa MALINOWSKA", "MALINOWSKA", "Ewa", "Kobieta"),
        ]
        conn.cursor().executemany(
            "INSERT INTO mart.sejm_candidates (year, candidate_id, district_no, committee_id, list_no, list_position,"
            " full_name, last_name, first_names, gender, votes, elected) VALUES (%s, %s, %s, 1, 1, 1, %s, %s, %s, %s, 10, false)",
            sejm,
        )
        conn.cursor().executemany(  # namesakes in one election, told apart by residence
            "INSERT INTO mart.sejm_candidates (year, candidate_id, district_no, committee_id, list_no, list_position,"
            " full_name, last_name, first_names, gender, residence, residence_teryt, votes, elected)"
            " VALUES (2023, %s, %s, 1, 1, 2, 'Tomasz WÓJCIK', 'WÓJCIK', 'Tomasz', 'Mężczyzna', %s, %s, 10, false)",
            [(5, 1, "Bolesławiec", "020101"), (6, 13, "Kraków", "126101")],
        )
        conn.cursor().executemany(  # 2015: residence as text only, mapped to a powiat through the 2023 pairs
            "INSERT INTO mart.sejm_candidates (year, candidate_id, district_no, committee_id, list_no, list_position,"
            " full_name, last_name, first_names, gender, residence, votes, elected)"
            " VALUES (2015, %s, %s, 1, 1, 2, 'Adam KRAWCZYK', 'KRAWCZYK', 'Adam', 'Mężczyzna', %s, 10, false)",
            [(8, 1, "Bolesławiec"), (9, 13, "Kraków")],
        )
        wbp = [(2018, 1, "020101", "Anna NOWAK", "NOWAK", "Anna", "Kobieta", 40),
               (2018, 2, "020102", "Marek LIS", "LIS", "Marek", "Mężczyzna", 50),
               (2024, 3, "020106", "Piotr ZIELIŃSKI", "ZIELIŃSKI", "Piotr", "Mężczyzna", 46)]
        conn.cursor().executemany(
            "INSERT INTO mart.wbp_candidates (year, round, candidate_id, teryt_gmina, full_name, last_name, first_names,"
            " gender, age, elected) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, false)",
            wbp,
        )
        rada = [(2024, 1, "020103", "Anna NOWAK", "NOWAK", "Anna", "Kobieta", 48),
                (2024, 2, "020104", "Marek LIS", "LIS", "Marek", "Mężczyzna", 55),
                (2018, 3, "020105", "Ewa WIŚNIEWSKA", "WIŚNIEWSKA", "Ewa", "Kobieta", 44),
                (2018, 4, "020107", "Piotr ZIELIŃSKI", "ZIELIŃSKI", "Piotr", "Mężczyzna", 40),
                (2018, 5, "0301", "Piotr ZIELIŃSKI", "ZIELIŃSKI", "Piotr", "Mężczyzna", 40),
                (2024, 6, "126105", "Tomasz WÓJCIK", "WÓJCIK", "Tomasz", "Mężczyzna", 40),
                (2024, 7, "300101", "Tomasz WÓJCIK", "WÓJCIK", "Tomasz", "Mężczyzna", 52),
                (2024, 8, "126107", "Adam KRAWCZYK", "KRAWCZYK", "Adam", "Mężczyzna", 45)]
        conn.cursor().executemany(
            "INSERT INTO mart.rada_candidates (year, candidate_id, council_level, teryt, district_no, committee_id,"
            " full_name, last_name, first_names, gender, age, elected) VALUES (%s, %s, 'gmina', %s, 1, 1, %s, %s, %s, %s, %s, false)",
            rada,
        )

    def _person(self, conn, table: str, year: int, name: str):
        return conn.execute(f"SELECT person_id FROM mart.{table} WHERE year = %s AND full_name = %s", (year, name)).fetchone()[0]

    def test_birth_years_split_namesakes_and_overrides_win(self) -> None:
        import psycopg

        from api.services.config import DATABASE_URL

        with psycopg.connect(DATABASE_URL) as conn:
            try:
                self._fixture(conn)
                self._link(
                    conn,
                    "sejm,2023,Jan KOWALSKI,5,jan-2023,different person than in 2019\n"
                    "sejm,2019,Ewa MALINOWSKA,,ewa,changed surname\n"
                    "rada,2018,Ewa WIŚNIEWSKA,020105,ewa,changed surname\n",
                )
                jan_2019 = self._person(conn, "sejm_candidates", 2019, "Jan KOWALSKI")
                jan_2023 = self._person(conn, "sejm_candidates", 2023, "Jan KOWALSKI")
                self.assertNotEqual(jan_2019, jan_2023)
                rule = conn.execute("SELECT match_rule FROM mart.persons WHERE person_id = %s", (jan_2023,)).fetchone()[0]
                self.assertEqual(rule, "manual")
                # Marek: born 1968 (wbp 2018, age 50) and 1969 (council 2024, age 55): one person.
                marek = self._person(conn, "sejm_candidates", 2015, "Marek LIS")
                self.assertEqual(self._person(conn, "wbp_candidates", 2018, "Marek LIS"), marek)
                self.assertEqual(self._person(conn, "rada_candidates", 2024, "Marek LIS"), marek)
                # Anna: born 1978 and 1976 -> two people with one name; neither is the national Anna, and
                # each becomes a separate local person.
                anna = self._person(conn, "sejm_candidates", 2023, "Anna NOWAK")
                anna_wbp = self._person(conn, "wbp_candidates", 2018, "Anna NOWAK")
                anna_rada = self._person(conn, "rada_candidates", 2024, "Anna NOWAK")
                self.assertEqual(len({anna, anna_wbp, anna_rada}), 3)
                # Piotr has no national candidacy: council 2018 and mayor 2024 in powiat 0201 (born 1978) are one
                # local person; the namesake in powiat 0301 is another.
                piotr = conn.execute(
                    "SELECT k.person_id, p.match_rule FROM mart.rada_candidates k JOIN mart.persons p USING (person_id)"
                    " WHERE k.year = 2018 AND k.teryt = '020107'"
                ).fetchone()
                self.assertEqual(piotr[1], "local:name+gender+birth year+powiat")
                self.assertEqual(self._person(conn, "wbp_candidates", 2024, "Piotr ZIELIŃSKI"), piotr[0])
                other = conn.execute("SELECT person_id FROM mart.rada_candidates WHERE year = 2018 AND teryt = '0301'").fetchone()[0]
                self.assertNotEqual(other, piotr[0])
                # Two national Tomasz WÓJCIK: the councillor in powiat 1261 joins the one living in Kraków
                # (residence_teryt 126101); the one in powiat 3001, where neither lives, stays local.
                tomasz = dict(conn.execute(
                    "SELECT residence_teryt, person_id FROM mart.sejm_candidates WHERE full_name = 'Tomasz WÓJCIK'"
                ).fetchall())
                self.assertNotEqual(tomasz["020101"], tomasz["126101"])
                rada_tomasz = dict(conn.execute(
                    "SELECT teryt, person_id FROM mart.rada_candidates WHERE full_name = 'Tomasz WÓJCIK'"
                ).fetchall())
                self.assertEqual(rada_tomasz["126105"], tomasz["126101"])
                self.assertNotIn(rada_tomasz["300101"], tomasz.values())
                # Adam KRAWCZYK (2015, residence text only): "Kraków" maps to powiat 1261 via the 2023 pairs.
                adam = dict(conn.execute(
                    "SELECT residence, person_id FROM mart.sejm_candidates WHERE full_name = 'Adam KRAWCZYK'"
                ).fetchall())
                self.assertNotEqual(adam["Kraków"], adam["Bolesławiec"])
                self.assertEqual(self._person(conn, "rada_candidates", 2024, "Adam KRAWCZYK"), adam["Kraków"])
                # Override links a local candidacy under another surname to the national person.
                ewa = self._person(conn, "sejm_candidates", 2019, "Ewa MALINOWSKA")
                self.assertEqual(self._person(conn, "rada_candidates", 2018, "Ewa WIŚNIEWSKA"), ewa)
            finally:
                conn.rollback()

    def test_national_namesakes_and_abbreviated_names(self) -> None:
        import psycopg

        from api.services.config import DATABASE_URL

        with psycopg.connect(DATABASE_URL) as conn:
            try:
                self._fixture(conn)
                sejm = [  # year, id, district, full name, first names, residence, residence_teryt, occupation, age
                    # 1991 gives only the first given name; birth years 1957 link Gdańsk to Sopot.
                    (1991, 100, 25, "Donald TUSK", "Donald", "Gdańsk", None, "dziennikarz", 34),
                    (1993, 100, 25, "Donald Franciszek TUSK", "Donald Franciszek", "Sopot", None, "historyk", 36),
                    # Same place, no ages: linked. No evidence: not linked. Birth years 20 apart: not linked.
                    (1991, 101, 10, "Marek WRONA", "Marek", "Kłodzko", None, "rolnik", None),
                    (2005, 101, 10, "Marek Jan WRONA", "Marek Jan", "Kłodzko", None, "rolnik", None),
                    # From 2011 one given name is the full name: a namesake beside Marek Jan in 2011.
                    (2011, 101, 10, "Marek Jan WRONA", "Marek Jan", "Kłodzko", None, "rolnik", None),
                    (2011, 102, 26, "Marek WRONA", "Marek", "Gdynia", None, "kierowca", None),
                    (1991, 102, 11, "Jan NOWICKI", "Jan", "Lublin", None, "lekarz", None),
                    (2005, 102, 11, "Jan Piotr NOWICKI", "Jan Piotr", "Radom", None, "lekarz", None),
                    (1991, 103, 12, "Ewa LIS", "Ewa", "Opole", None, "nauczyciel", 30),
                    (1993, 103, 12, "Ewa Maria LIS", "Ewa Maria", "Opole", None, "nauczyciel", 52),
                    # One given name, residences in voivodeships 02 and 12, unlike occupations: two persons.
                    (2011, 104, 1, "Andrzej KOT", "Andrzej", "Bolesławiec", None, "rolnik", None),
                    (2015, 104, 1, "Andrzej KOT", "Andrzej", "Bolesławiec", None, "rolnik", None),
                    (2019, 104, 13, "Andrzej KOT", "Andrzej", "Kraków", "126101", "informatyk", None),
                    # The same, but alike occupations: one person (moved).
                    (2015, 105, 1, "Anna KOT", "Anna", "Bolesławiec", None, "lekarz", None),
                    (2019, 105, 13, "Anna KOT", "Anna", "Kraków", "126101", "lekarz weterynarii", None),
                    # Birth years 1931 and 1963 in different places: two persons; 1997 joins by place.
                    (1991, 106, 13, "Jerzy MAZUR", "Jerzy", "Kraków", None, "emeryt", 60),
                    (1993, 106, 13, "Jerzy MAZUR", "Jerzy", "Wieliczka", None, "student", 30),
                    (1997, 106, 13, "Jerzy MAZUR", "Jerzy", "Wieliczka", None, "prawnik", None),
                    # Birth years 7 apart in the same place: a register error, one person.
                    (1991, 107, 13, "Jerzy GAJ", "Jerzy", "Kraków", None, "prawnik", 50),
                    (1993, 107, 13, "Jerzy GAJ", "Jerzy", "Kraków", None, "prawnik", 45),
                    # Namesakes of one election with the same residence: a person each.
                    (1991, 108, 1, "Jan KAMIŃSKI", "Jan", "Warszawa", None, "inżynier", None),
                    (1991, 109, 2, "Jan KAMIŃSKI", "Jan", "Warszawa", None, "inżynier", None),
                    # Local candidacies below: born 1984 (too young for the Sejm in 1991), born 1966 (1957 here).
                    (1991, 110, 5, "Lech ROGALA", "Lech", "Opole", None, "rolnik", None),
                    (1997, 111, 5, "Zofia BUK", "Zofia", "Opole", None, "rolnik", 40),
                ]
                conn.cursor().executemany(
                    "INSERT INTO mart.sejm_candidates (year, candidate_id, district_no, committee_id, list_no, list_position,"
                    " full_name, last_name, first_names, gender, residence, residence_teryt, occupation, age, votes, elected)"
                    " VALUES (%s, %s, %s, 1, 1, 3, %s, upper(split_part(%s, ' ', -1)), %s,"
                    " CASE WHEN %s ~ '^(Ewa|Anna|Zofia) ' THEN 'Kobieta' ELSE 'Mężczyzna' END, %s, %s, %s, %s, 10, false)",
                    [(y, i, d, n, n, f, n, r, t, o, a) for y, i, d, n, f, r, t, o, a in sejm],
                )
                conn.execute(
                    "INSERT INTO mart.senat_candidates (year, candidate_id, district_no, committee_name, full_name, last_name,"
                    " first_names, gender, residence, votes, elected)"
                    " VALUES (2001, 100, 30, 'X', 'Donald Franciszek TUSK', 'TUSK', 'Donald Franciszek', 'Mężczyzna', 'Sopot', 10, true)"
                )
                conn.cursor().executemany(
                    "INSERT INTO mart.rada_candidates (year, candidate_id, council_level, teryt, district_no, committee_id,"
                    " full_name, last_name, first_names, gender, age, elected) VALUES (%s, %s, 'gmina', '160101', 1, 1, %s, %s, %s, %s, %s, false)",
                    [(2024, 100, "Lech ROGALA", "ROGALA", "Lech", "Mężczyzna", 40),
                     (2018, 101, "Zofia BUK", "BUK", "Zofia", "Kobieta", 52)],
                )
                self._link(conn, "")

                def ids(year: int, name: str) -> list:
                    return [r[0] for r in conn.execute(
                        "SELECT person_id FROM mart.sejm_candidates WHERE year = %s AND full_name = %s ORDER BY candidate_id",
                        (year, name)).fetchall()]

                def rule(person_id: int) -> str:
                    return conn.execute("SELECT match_rule FROM mart.persons WHERE person_id = %s", (person_id,)).fetchone()[0]

                tusk = self._person(conn, "sejm_candidates", 1991, "Donald TUSK")
                self.assertEqual(self._person(conn, "sejm_candidates", 1993, "Donald Franciszek TUSK"), tusk)
                self.assertEqual(self._person(conn, "senat_candidates", 2001, "Donald Franciszek TUSK"), tusk)
                self.assertEqual(rule(tusk), "surname+first given name+gender")
                self.assertEqual(conn.execute("SELECT full_name FROM mart.persons WHERE person_id = %s", (tusk,)).fetchone()[0],
                                 "Donald Franciszek TUSK")
                self.assertEqual(ids(1991, "Marek WRONA"), ids(2005, "Marek Jan WRONA"))
                self.assertEqual(ids(2005, "Marek Jan WRONA"), ids(2011, "Marek Jan WRONA"))
                self.assertNotEqual(ids(2011, "Marek WRONA"), ids(2011, "Marek Jan WRONA"))
                self.assertNotEqual(ids(1991, "Jan NOWICKI"), ids(2005, "Jan Piotr NOWICKI"))
                self.assertNotEqual(ids(1991, "Ewa LIS"), ids(1993, "Ewa Maria LIS"))
                kot = ids(2011, "Andrzej KOT") + ids(2015, "Andrzej KOT") + ids(2019, "Andrzej KOT")
                self.assertEqual(kot[0], kot[1])
                self.assertNotEqual(kot[1], kot[2])
                self.assertEqual(rule(kot[2]), "name+gender+voivodeship")
                self.assertEqual(ids(2015, "Anna KOT"), ids(2019, "Anna KOT"))
                mazur = ids(1991, "Jerzy MAZUR") + ids(1993, "Jerzy MAZUR") + ids(1997, "Jerzy MAZUR")
                self.assertNotEqual(mazur[0], mazur[1])
                self.assertEqual(mazur[1], mazur[2])
                self.assertEqual(rule(mazur[0]), "name+gender+birth year")
                self.assertEqual(ids(1991, "Jerzy GAJ"), ids(1993, "Jerzy GAJ"))
                self.assertEqual(len(set(ids(1991, "Jan KAMIŃSKI"))), 2)
                # Local namesakes whose birth year contradicts the national candidacy become local persons.
                for year, name in ((2024, "Lech ROGALA"), (2018, "Zofia BUK")):
                    national = self._person(conn, "sejm_candidates", 1991 if year == 2024 else 1997, name)
                    local = self._person(conn, "rada_candidates", year, name)
                    self.assertNotEqual(local, national)
                    self.assertTrue(rule(local).startswith("local:"))
            finally:
                conn.rollback()

    def test_override_matching_nothing_fails(self) -> None:
        import psycopg

        from api.services.config import DATABASE_URL

        with psycopg.connect(DATABASE_URL) as conn:
            try:
                self._fixture(conn)
                with self.assertRaisesRegex(ValueError, r"lines \[3\]"):
                    self._link(conn, "sejm,2019,Jan KOWALSKI,1,jan,ok\nsejm,2019,Jan KOWALSKY,1,jan,typo\n")
            finally:
                conn.rollback()


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestSenatByelectionsDb(unittest.TestCase):
    def test_mistyped_votes_fail_the_build(self) -> None:
        import tempfile
        from pathlib import Path

        import psycopg

        from api.services.config import DATABASE_URL
        from api.services.mart import derived

        lines = derived.SENAT_BYELECTIONS.read_text(encoding="utf-8").splitlines(keepends=True)
        # 2016 district 59: Anna Maria ANDERS won with 30 661 of 64 881 valid votes.
        row = next(i for i, line in enumerate(lines) if ",ANDERS,Anna Maria," in line)
        with tempfile.TemporaryDirectory() as tmp, psycopg.connect(DATABASE_URL) as conn:
            path = Path(tmp) / "byelections.csv"
            path.write_text("".join(lines[:row] + [lines[row].replace(",30661,", ",30616,")] + lines[row + 1:]), encoding="utf-8")
            try:
                with self.assertRaisesRegex(ValueError, r"2016, 3, 6\), 59"):
                    derived.load_senat_byelections(conn, path)
            finally:
                conn.rollback()


if __name__ == "__main__":
    unittest.main()
