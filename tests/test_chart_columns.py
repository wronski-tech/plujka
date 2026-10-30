"""Unit tests for picking chart columns of a result (streamlit_app/chart_columns.py)."""

from __future__ import annotations

import unittest

from streamlit_app.chart_columns import line_series, numeric_columns, scatter_columns


class TestChartColumns(unittest.TestCase):
    def test_codes_are_not_measures(self) -> None:
        row = {
            "year": 2023, "round": 1, "teryt_powiat": "1465", "district_no": 19, "person_id": 7,
            "list_position": 3, "list_group": 2, "rank": 1, "votes": 1200, "pct": 12.5,
        }
        self.assertEqual(numeric_columns([row]), ["votes", "pct"])

    def test_turnout_and_unemployment_per_powiat(self) -> None:
        rows = [
            {"teryt_powiat": "0201", "powiat": "bolesławiecki", "turnout_pct": 68.1, "unemployment_rate": 4.2},
            {"teryt_powiat": "0202", "powiat": "dzierżoniowski", "turnout_pct": 66.0, "unemployment_rate": 7.9},
            {"teryt_powiat": "0203", "powiat": "głogowski", "turnout_pct": 70.3, "unemployment_rate": 3.1},
        ]
        self.assertEqual(scatter_columns(rows), ("powiat", "turnout_pct", "unemployment_rate"))

    def test_finest_unit_name_labels_points(self) -> None:
        rows = [
            {"year": 2023, "wojewodztwo": "mazowieckie", "teryt_gmina": f"14650{n}", "gmina": f"G{n}",
             "committee": "PiS", "pct": 30.0 + n, "avg_salary": 7000 + n}
            for n in range(3)
        ]
        self.assertEqual(scatter_columns(rows), ("gmina", "pct", "avg_salary"))

    def test_teryt_with_other_name_column(self) -> None:
        rows = [{"teryt": f"0{n}", "name": f"W{n}", "density": 100 + n, "urban_pct": 50.0 + n} for n in range(3)]
        self.assertEqual(scatter_columns(rows), ("name", "density", "urban_pct"))

    def test_no_scatter_without_exactly_two_measures(self) -> None:
        one = [{"powiat": f"P{n}", "teryt_powiat": f"02{n:02d}", "pct": 1.0 + n} for n in range(3)]
        three = [{**row, "votes": 10, "seats": 1} for row in one]
        self.assertIsNone(scatter_columns(one))
        self.assertIsNone(scatter_columns(three))

    def test_no_scatter_for_non_territorial_rows(self) -> None:
        rows = [{"committee": f"K{n}", "votes": 100 * n, "pct": 10.0 * n} for n in range(3)]
        self.assertIsNone(scatter_columns(rows))

    def test_no_scatter_when_a_unit_repeats(self) -> None:
        # One row per party per powiat: two points per unit would mislead.
        rows = [
            {"teryt_powiat": "0201", "powiat": "bolesławiecki", "committee": c, "votes": v, "pct": p}
            for c, v, p in (("A", 10, 40.0), ("B", 15, 60.0))
        ] + [{"teryt_powiat": "0202", "powiat": "dzierżoniowski", "committee": "A", "votes": 5, "pct": 50.0}]
        self.assertIsNone(scatter_columns(rows))

    def test_no_scatter_for_two_units(self) -> None:
        rows = [{"powiat": f"P{n}", "pct": 1.0, "avg_salary": 2.0} for n in range(2)]
        self.assertIsNone(scatter_columns(rows))


if __name__ == "__main__":
    unittest.main()

    def test_line_series_one_row_per_year_and_party(self) -> None:
        rows = [{"gmina": "Gdów", "year": y, "party": p, "pct": 10.0} for y in (2019, 2023) for p in ("KO", "PiS")]
        self.assertEqual(line_series(rows), ("party",))
        self.assertEqual(line_series([r for r in rows if r["party"] == "KO"]), (None,))
        # Two rounds in one year, or one year only, are not a time series.
        self.assertIsNone(line_series([{**r, "round": n} for r in rows[:1] for n in (1, 2)]))
        self.assertIsNone(line_series([r for r in rows if r["year"] == 2023]))
        # Two varying labels (party and unit) would draw tangled lines.
        self.assertIsNone(line_series([{**r, "gmina": r["party"]} for r in rows]))
