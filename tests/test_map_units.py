"""Unit tests for matching result rows to map boundaries (streamlit_app/map_units.py)."""

from __future__ import annotations

import json
import unittest

from streamlit_app.map_units import (
    GEO_DIR, boundaries, is_additive, is_osm, teryt_changes, unit_values, visible_features,
)

DISTRICTS = [f"1465{n:02d}" for n in range(2, 20)]


def drawn_codes(codes: set[str], level: str = "gminy", years: set[int] | None = None) -> list[tuple[str, bool]]:
    """(teryt, is former extent) of the features drawn for the result codes and years."""
    return [(f["properties"]["teryt"], "parts" in f["properties"])
            for f in visible_features(boundaries(level)["features"], codes, years)]


class TestTerytChanges(unittest.TestCase):
    def test_walbrzych_2011_colours_current_city(self) -> None:
        rows = [
            {"year": 2011, "teryt_gmina": "022109", "gmina": "Wałbrzych", "votes": 39537},
            {"year": 2011, "teryt_gmina": "022101", "gmina": "Boguszów-Gorce", "votes": 4684},
        ]
        values, merged, skipped = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual(values, {"026501": 39537, "022101": 4684})
        self.assertEqual((merged, skipped), ({}, []))
        self.assertIn(("026501", False), drawn_codes(set(values)))

    def test_merged_units_sum_votes_and_skip_pct(self) -> None:
        rows = [
            {"year": 2010, "round": 1, "teryt_gmina": "086201", "votes": 100, "pct": 40.0},
            {"year": 2010, "round": 1, "teryt_gmina": "080910", "votes": 30, "pct": 55.0},
            {"year": 2010, "round": 1, "teryt_gmina": "080901", "votes": 7, "pct": 20.0},
        ]
        values, merged, skipped = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual(values, {"086201": 130, "080901": 7})
        self.assertEqual((merged, skipped), ({"086201": 2}, []))
        values, merged, skipped = unit_values(rows, "teryt_gmina", "pct", "gminy")
        self.assertEqual(values, {"080901": 20.0})
        self.assertEqual((merged, skipped), ({}, ["086201"]))

    def test_merged_units_from_different_elections_are_skipped(self) -> None:
        rows = [
            {"year": 2011, "teryt_gmina": "022109", "votes": 39537},
            {"year": 2015, "teryt_gmina": "026501", "votes": 40453},
        ]
        values, merged, skipped = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual((values, merged, skipped), ({}, {}, ["026501"]))

    def test_ship_and_abroad_codes_stay_unmapped(self) -> None:
        rows = [{"teryt_gmina": code, "votes": 5} for code in ("149801", "149901", "229901", "329901")]
        values, _, _ = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual(set(values), {"149801", "149901", "229901", "329901"})
        features = {teryt for teryt, _ in drawn_codes(set(values))}
        self.assertFalse(set(values) & features)

    def test_additive_columns(self) -> None:
        for column in ("votes", "yes_votes", "unit_valid_votes", "seats", "eligible_voters", "valid_ballots"):
            self.assertTrue(is_additive(column), column)
        for column in ("pct", "turnout_pct", "yes_pct", "change_pp", "votes_pct", "age"):
            self.assertFalse(is_additive(column), column)

    def test_mapping_targets_exist_and_old_codes_do_not(self) -> None:
        for level, changes in teryt_changes().items():
            current = {f["properties"]["teryt"] for f in boundaries(level)["features"]}
            for old, new in changes.items():
                self.assertIn(new, current, old)
                self.assertNotIn(old, current, old)


class TestFormerExtents(unittest.TestCase):
    def test_city_level_warsaw_colours_whole_city(self) -> None:
        drawn = drawn_codes({"146501", "146601"})
        self.assertIn(("146501", True), drawn)
        self.assertFalse(set(DISTRICTS) & {teryt for teryt, _ in drawn})

    def test_district_results_colour_districts(self) -> None:
        drawn = drawn_codes(set(DISTRICTS) | {"146601"})
        self.assertTrue(set(DISTRICTS) <= {teryt for teryt, former in drawn if not former})
        self.assertNotIn("146501", {teryt for teryt, _ in drawn})

    def test_gmina_before_split_uses_former_extent(self) -> None:
        # Szczawa (120713) was split off Kamienica (120705) on 2025-01-01.
        before = drawn_codes({"120705", "120704"})
        self.assertIn(("120705", True), before)
        self.assertNotIn("120713", {teryt for teryt, _ in before})
        after = drawn_codes({"120705", "120713"})
        self.assertIn(("120705", False), after)
        self.assertIn(("120713", False), after)
        self.assertNotIn(("120705", True), after)

    def test_powiat_walbrzyski_before_2013_covers_the_city(self) -> None:
        before = drawn_codes({"0221", "0208"}, "powiaty")
        self.assertIn(("0221", True), before)
        self.assertNotIn("0265", {teryt for teryt, _ in before})
        self.assertIn(("0265", False), drawn_codes({"0221", "0265"}, "powiaty"))

    def test_parts_exist_as_current_units(self) -> None:
        for level in ("gminy", "powiaty"):
            features = boundaries(level)["features"]
            current = {f["properties"]["teryt"] for f in features if "parts" not in f["properties"]}
            for feature in features:
                props = feature["properties"]
                for code in set(props.get("parts", [])) - {props["teryt"]}:
                    self.assertIn(code, current, props)
                # An old-only code must reach its outline unchanged, not be mapped elsewhere.
                if "parts" in props and props["teryt"] not in current:
                    self.assertNotIn(props["teryt"], teryt_changes()[level], props)

    def test_period_extent_needs_matching_year(self) -> None:
        # Kamienica alone in a 2025 result is today's gmina, not the pre-2025 extent.
        self.assertIn(("120705", True), drawn_codes({"120705", "120704"}, years={2020}))
        drawn = drawn_codes({"120705", "120704"}, years={2025})
        self.assertIn(("120705", False), drawn)
        self.assertNotIn(("120705", True), drawn)


class TestOpenStreetMapFeatures(unittest.TestCase):
    def test_osm_features_live_only_in_osm_files(self) -> None:
        for level in ("gminy", "powiaty"):
            main = json.loads((GEO_DIR / f"{level}.json").read_text(encoding="utf-8"))["features"]
            self.assertFalse([f["properties"] for f in main if is_osm(f)], level)
            osm = json.loads((GEO_DIR / f"{level}_osm.json").read_text(encoding="utf-8"))["features"]
            self.assertTrue(osm and all(is_osm(f) for f in osm), level)
        osm_gminy = {f["properties"]["teryt"] for f in boundaries("gminy")["features"] if is_osm(f)}
        self.assertTrue(set(DISTRICTS) | {"120713", "200216", "143104"} <= osm_gminy)

    def test_district_result_colours_osm_districts(self) -> None:
        shown = visible_features(boundaries("gminy")["features"], set(DISTRICTS) | {"146601"})
        osm = {f["properties"]["teryt"] for f in shown if is_osm(f) and "parts" not in f["properties"]}
        self.assertTrue(set(DISTRICTS) <= osm)

    def test_unused_osm_units_fall_back_to_public_domain_extents(self) -> None:
        # A result outside Warsaw and the 2025 splits draws no OpenStreetMap feature (no attribution).
        shown = visible_features(boundaries("gminy")["features"], {"022101", "022102"}, {2011})
        self.assertFalse([f["properties"]["teryt"] for f in shown if is_osm(f)])
        drawn = {(f["properties"]["teryt"], "parts" in f["properties"]) for f in shown}
        self.assertTrue({("146501", True), ("120705", True), ("200209", True)} <= drawn)


class TestYears2000To2003(unittest.TestCase):
    def test_2001_gmina_codes_of_2002_powiats_map_to_current(self) -> None:
        rows = [
            {"year": 2001, "teryt_gmina": "080411", "votes": 10},  # Wschowa
            {"year": 2001, "teryt_gmina": "180104", "votes": 20},  # Lesko
            {"year": 2001, "teryt_gmina": "026301", "votes": 30},  # Wałbrzych
            {"year": 2001, "teryt_gmina": "141203", "votes": 40},  # Wesoła
        ]
        values, merged, skipped = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual(values, {"081203": 10, "182103": 20, "026501": 30, "146515": 40})
        self.assertEqual((merged, skipped), ({}, []))

    def test_2001_ships_stay_unmapped(self) -> None:
        rows = [{"teryt_gmina": code, "votes": 1} for code in ("229997", "329997")]
        values, _, _ = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual(set(values), {"229997", "329997"})
        self.assertFalse(set(values) & {teryt for teryt, _ in drawn_codes(set(values))})

    def test_warszawa_centrum_2000_uses_its_outline(self) -> None:
        centrum = {"146505", "146506", "146507", "146508", "146510", "146518", "146519"}
        others = {"146502", "146503", "146504", "146509", "146511", "146512", "146513", "146514", "146516", "146517"}
        drawn = drawn_codes({"143104", "146515"} | others, years={2000})
        self.assertIn(("143104", True), drawn)
        plain = {teryt for teryt, former in drawn if not former}
        self.assertTrue(others | {"146515"} <= plain)
        self.assertFalse(centrum & plain)
        self.assertNotIn("146501", {teryt for teryt, _ in drawn})

    def test_walbrzych_city_powiat_2001_is_renumbered(self) -> None:
        rows = [{"year": 2001, "teryt_powiat": "0263", "pct": 40.0}, {"year": 2001, "teryt_powiat": "0221", "pct": 30.0}]
        values, _, _ = unit_values(rows, "teryt_powiat", "pct", "powiaty")
        self.assertEqual(values, {"0265": 40.0, "0221": 30.0})
        # With the city as its own powiat, powiat wałbrzyski keeps today's extent.
        self.assertIn(("0221", False), drawn_codes(set(values), "powiaty", {2001}))

    def test_2001_powiat_split_in_2002_uses_former_extent(self) -> None:
        # Nowosolski covered wschowski (0812) until 2001; a later powiat code is absent from the result.
        for years in ({2001}, set()):
            drawn = drawn_codes({"0804", "0802"}, "powiaty", years)
            self.assertIn(("0804", True), drawn)
            self.assertNotIn("0812", {teryt for teryt, _ in drawn})
        drawn = drawn_codes({"0804", "0802"}, "powiaty", {2023})
        self.assertIn(("0804", False), drawn)
        self.assertIn(("0812", False), drawn)

    def test_gmina_moved_between_existing_powiats_needs_year(self) -> None:
        # Tarczyn was in grójecki (1406) until 2002 and is in piaseczyński (1418) since 2003.
        drawn = drawn_codes({"1406", "1418"}, "powiaty", {2001})
        self.assertIn(("1406", True), drawn)
        self.assertIn(("1418", True), drawn)
        self.assertNotIn(("1406", False), drawn)
        for years in (set(), {2005}):
            drawn = drawn_codes({"1406", "1418"}, "powiaty", years)
            self.assertEqual({former for teryt, former in drawn if teryt in ("1406", "1418")}, {False})

    def test_warsaw_powiats_2000(self) -> None:
        drawn = drawn_codes({"1431", "1465", "1412"}, "powiaty", {2000})
        former = {teryt for teryt, is_former in drawn if is_former}
        self.assertTrue({"1431", "1465", "1412"} <= former)
        self.assertFalse({"1465", "1412"} & {teryt for teryt, is_former in drawn if not is_former})
        # In 2001 Warszawa-Centrum is already split into districts coded 1465xx.
        drawn = drawn_codes({"1465", "1412"}, "powiaty", {2001})
        self.assertIn(("1465", True), drawn)
        self.assertNotIn("1431", {teryt for teryt, _ in drawn})


if __name__ == "__main__":
    unittest.main()
