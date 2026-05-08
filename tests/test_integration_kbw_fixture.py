"""End-to-end KBW pipeline on a tiny synthetic row (requires PostgreSQL + extensions).

Runs when ``PLUJKA_RUN_DB_TESTS=1`` (see CI integration job).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from api.services import db, kbw_analytics, kbw_import

_RUN = os.environ.get("PLUJKA_RUN_DB_TESTS", "").strip().lower() in ("1", "true", "yes")

_FIXTURE_YEAR = 2099
_FIXTURE_REL_PATH = "test_fixture/2099/sejm/wyniki_gl_na_kandydatow_po_gminach_sejm_csv/x.csv"


def _delete_fixture_catalog_row() -> None:
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kbw_dane_files WHERE rel_path = %s", (_FIXTURE_REL_PATH,))


def _seed_minimal_sejm_candidate_fact() -> None:
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kbw_dane_files (
                  rel_path, file_name, file_ext, file_kind
                ) VALUES (%s, 'x.csv', 'csv', 'tabular')
                RETURNING id
                """,
                (_FIXTURE_REL_PATH,),
            )
            file_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO kbw_election_runs (family, year, round, slice, variant)
                VALUES ('sejm', %s, 0, '', '')
                RETURNING id
                """,
                (_FIXTURE_YEAR,),
            )
            run_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO kbw_facts (
                  election_run_id, geography, subject, metric, value,
                  is_percentage, source_file_id
                ) VALUES (
                  %s,
                  '{"Gmina": "Test gmina", "Numer okręgu": "1"}'::jsonb,
                  '{"kind": "series", "column": "Jan Kowalski — lista test"}'::jsonb,
                  'jan_kowalski',
                  142.0,
                  false,
                  %s
                )
                """,
                (run_id, file_id),
            )


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestKbwMinimalFixture(unittest.TestCase):
    def setUp(self) -> None:
        db.init_database()
        kbw_import.clear_kbw_imported_facts()
        _delete_fixture_catalog_row()
        _seed_minimal_sejm_candidate_fact()

    def tearDown(self) -> None:
        kbw_import.clear_kbw_imported_facts()
        _delete_fixture_catalog_row()

    def test_candidate_fact_backfills_view_and_geo_sql(self) -> None:
        n_geo = db.backfill_kbw_candidate_geo_votes_from_facts(year=_FIXTURE_YEAR)
        self.assertEqual(n_geo, 1)
        n_person = db.backfill_kbw_person_election_facts(year=_FIXTURE_YEAR)
        self.assertEqual(n_person, 1)
        n_sync = db.sync_kbw_candidates_from_person_facts(year=_FIXTURE_YEAR)
        self.assertEqual(n_sync, 1)

        agg = db.run_sql(
            "SELECT votes FROM kbw_v_sejm_district_list_agg WHERE year = %(y)s LIMIT 1",
            {"y": _FIXTURE_YEAR},
        )
        self.assertEqual(len(agg), 1)
        self.assertEqual(int(agg[0]["votes"]), 142)

        sql, params = kbw_analytics.sql_candidate_geo_votes_detail_from_facts(
            year=_FIXTURE_YEAR,
            candidate_pattern="%Kowalski%",
            gmina_pattern="%Test gmina%",
            limit=10,
        )
        rows = db.run_sql(sql, params)
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["votes"]), 142)
        geo = rows[0]["geography"]
        if isinstance(geo, dict):
            self.assertIn("Test gmina", str(geo.get("Gmina", "")))
        else:
            self.assertIn("Test gmina", str(geo))

        fact_cnt = db.run_sql("SELECT COUNT(*)::int AS c FROM kbw_facts", {})[0]["c"]
        self.assertGreaterEqual(fact_cnt, 1)

    def test_route_question_geo_intent_kb_facts_fallback(self) -> None:
        """Router branch + SQL without ``kbw_candidate_geo_votes`` backfill (scan ``kbw_facts``).

        Intent/entity are patched so the test does not depend on token order in
        ``person_name_fragment_from_question`` vs. the word „kandydat”.
        """
        from api.services import router

        q = "Ile głosów w gminie Test gmina 2099?"
        with mock.patch("api.services.llm.OPENAI_API_KEY", ""):
            with mock.patch(
                "api.services.router.extract_intent_and_entity",
                return_value=("kbw_candidate_geo_votes_detail", "Kowalski"),
            ):
                out = router.route_question(q)
        self.assertEqual(out.get("intent"), "kbw_candidate_geo_votes_detail")
        self.assertEqual(out.get("candidate_geo_source"), "kbw_facts")
        self.assertEqual(out.get("year"), _FIXTURE_YEAR)
        res = out.get("result") or []
        self.assertEqual(len(res), 1)
        self.assertEqual(int(res[0]["votes"]), 142)


if __name__ == "__main__":
    unittest.main()
