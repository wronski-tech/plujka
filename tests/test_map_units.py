"""Unit tests for matching result rows to map boundaries (streamlit_app/map_units.py)."""

from __future__ import annotations

import json
import unittest

from streamlit_app.map_units import (
    GEO_DIR, boundaries, district_borders, feature_key, features_svg, is_additive, is_osm, pre1999_codes, teryt_changes, unit_values,
    visible_features, voivodeship_level,
)

DISTRICTS = [f"1465{n:02d}" for n in range(2, 20)]


def drawn_codes(codes: set[str], level: str = "gminy", years: set[int] | None = None) -> list[tuple[str, bool]]:
    """(teryt, is former extent) of the features drawn for the result codes and years."""
    return [(f["properties"]["teryt"], "parts" in f["properties"])
            for f in visible_features(boundaries(level)["features"], codes, years, level)]


def contains(geometry: dict, lon: float, lat: float) -> bool:
    """Point in (Multi)Polygon by ray casting (outer rings minus holes)."""
    def in_ring(ring: list) -> bool:
        inside = False
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return any(in_ring(p[0]) and not any(in_ring(h) for h in p[1:]) for p in polygons)


PRE1999 = json.loads((GEO_DIR / "gminy_pre1999.json").read_text(encoding="utf-8"))


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

    def test_warszawa_centrum_1998_uses_its_outline(self) -> None:
        centrum = {"146505", "146506", "146507", "146508", "146510", "146518", "146519"}
        others = {"146502", "146503", "146504", "146509", "146511", "146512", "146513", "146514", "146516", "146517"}
        drawn = drawn_codes({"143104", "141203"} | others, years={1998})
        self.assertIn(("143104", True), drawn)
        plain = {teryt for teryt, former in drawn if not former}
        self.assertTrue(others <= plain)
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

    def test_warsaw_powiats_1998_to_2001(self) -> None:
        # Sejmik 1998 keeps Warszawa-Centrum (1431) apart from the other Warsaw gminas (1465xx).
        drawn = drawn_codes({"1431", "1465", "1412"}, "powiaty", {1998})
        former = {teryt for teryt, is_former in drawn if is_former}
        self.assertTrue({"1431", "1465", "1412"} <= former)
        self.assertFalse({"1465", "1412"} & {teryt for teryt, is_former in drawn if not is_former})
        # From 2000 Warsaw is one powiat 1465 (president 2000 is split into districts too); Wesoła is in 1412.
        for year in (2000, 2001):
            drawn = drawn_codes({"1465", "1412"}, "powiaty", {year})
            self.assertTrue({("1465", True), ("1412", True)} <= set(drawn), year)
            self.assertNotIn("1431", {teryt for teryt, _ in drawn})

    def test_1998_warsaw_gmina_councils_map_to_districts(self) -> None:
        rows = [{"year": 1998, "teryt_gmina": "143101", "votes": 5}, {"year": 1998, "teryt_gmina": "143118", "votes": 7}]
        values, _, _ = unit_values(rows, "teryt_gmina", "votes", "gminy")
        self.assertEqual(values, {"146502": 5, "146517": 7})


class TestVoivodeships1975(unittest.TestCase):
    def test_level_needs_old_year_or_old_name(self) -> None:
        self.assertEqual(voivodeship_level([{"year": 1995, "wojewodztwo": "łódzkie"}, {"year": 1995, "wojewodztwo": "opolskie"}]),
                         "wojewodztwa_1975")
        self.assertEqual(voivodeship_level([{"wojewodztwo": "katowickie"}, {"wojewodztwo": "łódzkie"}]), "wojewodztwa_1975")
        # The 1998 sejmik and results without a year naming only shared names use the 16 voivodeships.
        self.assertEqual(voivodeship_level([{"year": 1998, "wojewodztwo": "łódzkie"}, {"year": 1998, "wojewodztwo": "opolskie"}]),
                         "wojewodztwa")
        self.assertEqual(voivodeship_level([{"wojewodztwo": "łódzkie"}, {"wojewodztwo": "lubelskie"}]), "wojewodztwa")
        self.assertEqual(voivodeship_level([{"year": 1995, "wojewodztwo": "mazowieckie"}]), "wojewodztwa")

    def test_every_gmina_belongs_to_one_old_voivodeship(self) -> None:
        voivodeships = PRE1999["voivodeships"]
        self.assertEqual(len(voivodeships), 49)
        assigned = [g for v in voivodeships.values() for g in v["gminy"]]
        self.assertEqual(len(assigned), len(set(assigned)))
        public = {f["properties"]["teryt"] for f in boundaries("gminy")["features"] if not is_osm(f)
                  and ("parts" not in f["properties"] or f["properties"]["teryt"] in ("146501", "120705", "200209"))}
        self.assertEqual(set(assigned), public)
        self.assertEqual(PRE1999["unassigned"], [])
        names = {f["properties"]["name"] for f in boundaries("wojewodztwa_1975")["features"]}
        self.assertEqual(names, {v["name"] for v in voivodeships.values()})

    def test_president_1995_colours_49_voivodeships(self) -> None:
        rows = [{"year": 1995, "round": 2, "wojewodztwo": v["name"], "pct": 50.0 + n}
                for n, v in enumerate(PRE1999["voivodeships"].values())]
        self.assertEqual(voivodeship_level(rows), "wojewodztwa_1975")
        values, merged, skipped = unit_values(rows, "wojewodztwo", "pct", "wojewodztwa_1975")
        shown = visible_features(boundaries("wojewodztwa_1975")["features"], set(values), {1995}, "wojewodztwa_1975")
        self.assertEqual(len({feature_key(f["properties"], "wojewodztwa_1975") for f in shown} & set(values)), 49)
        self.assertEqual((merged, skipped), ({}, []))
        # Brzeszcze (today małopolskie) was in katowickie, Szerzyny (today małopolskie) in tarnowskie.
        katowickie = next(f for f in shown if f["properties"]["name"] == "katowickie")
        self.assertTrue(contains(katowickie["geometry"], 19.15, 49.99))


class TestGminasBefore1999(unittest.TestCase):
    def test_codes_map_to_drawn_features(self) -> None:
        keys = {f["properties"]["teryt"] for f in boundaries("gminy")["features"]}
        for old, key in pre1999_codes().items():
            self.assertIn(key, keys, old)
        for old, entry in PRE1999["codes"].items():
            self.assertEqual(old[:2], next(k for k, v in PRE1999["voivodeships"].items() if v["name"] == entry["wojewodztwo"]))
            self.assertTrue(entry["current"] or entry.get("note"), old)
            self.assertTrue(entry["match"], old)

    def test_renamed_and_ambiguous_names(self) -> None:
        codes = pre1999_codes()
        self.assertEqual(codes["77641"], "221208")  # Słupsk gm., renamed Redzikowo in 2023
        self.assertEqual(codes["01643"], "140805")  # Skrzeszew, renamed Wieliszew in 1996
        self.assertEqual(codes["21177"], "080101")  # Kostrzyn (gorzowskie) = Kostrzyn nad Odrą
        self.assertEqual(codes["63185"], "302108")  # Kostrzyn (poznańskie)
        self.assertEqual(codes["01020"], "143104")  # Warszawa-Centrum 1994-2002
        self.assertNotIn("33589", codes)  # Ostrowice, split in 2019

    def test_referendum_1997_gminas_colour_current_outlines(self) -> None:
        rows = [{"year": 1997, "teryt_gmina_pre1999": code, "yes_pct": 50.0 + n}
                for n, code in enumerate(("27145", "27449", "27355"))]  # Bytom, Wodzisław Śląski, Pszów
        values, merged, skipped = unit_values(rows, "teryt_gmina_pre1999", "yes_pct", "gminy")
        self.assertEqual(set(values), {pre1999_codes()[r["teryt_gmina_pre1999"]] for r in rows})
        drawn = drawn_codes(set(values), years={1997})
        # Bytom covered Radzionków until 1997; Wodzisław Śląski no longer covered Radlin.
        self.assertIn(("246201", True), drawn)
        self.assertIn(("241504", False), drawn)
        self.assertNotIn("241303", {teryt for teryt, former in drawn if not former})

    def test_parent_extent_follows_the_year(self) -> None:
        self.assertIn(("241504", True), drawn_codes({"241504", "241503"}, years={1993}))
        parts = {tuple(f["properties"]["parts"]) for f in visible_features(boundaries("gminy")["features"], {"241504"}, {1993})
                 if f["properties"]["teryt"] == "241504" and "parts" in f["properties"]}
        self.assertEqual(parts, {("241504", "241501", "241502", "241508")})
        parts = {tuple(f["properties"]["parts"]) for f in visible_features(boundaries("gminy")["features"], {"241504"}, {1995})
                 if f["properties"]["teryt"] == "241504" and "parts" in f["properties"]}
        self.assertEqual(parts, {("241504", "241502")})
        # Without a year a 1990s extent is never used (a later result may lack Radzionków for other reasons).
        self.assertIn(("246201", False), drawn_codes({"246201", "246101"}))

    def test_warsaw_dzielnica_1990_uses_its_extent(self) -> None:
        rows = [{"year": 1990, "teryt_gmina_pre1999": code, "votes": 10} for code in ("01001", "01009", "01010", "01103")]
        values, _, _ = unit_values(rows, "teryt_gmina_pre1999", "votes", "gminy")
        self.assertEqual(set(values), {"01001", "146510", "146512", "143201"})
        drawn = drawn_codes(set(values), years={1990})
        self.assertIn(("01001", True), drawn)
        plain = {teryt for teryt, former in drawn if not former}
        self.assertFalse({"146505", "146513", "146516"} & plain)
        self.assertTrue({"146510", "146512"} <= plain)


class TestVoivodeshipExtents(unittest.TestCase):
    SLAWKOW, SZERZYNY = (19.39, 50.30), (21.25, 49.80)

    def outline(self, name: str, year: int | None) -> dict:
        shown = visible_features(boundaries("wojewodztwa")["features"], {"malopolskie", "slaskie", "podkarpackie"},
                                 {year} if year else None, "wojewodztwa")
        return next(f for f in shown if f["properties"]["name"] == name)

    def test_slawkow_in_malopolskie_until_2001(self) -> None:
        malopolskie, slaskie = self.outline("MAŁOPOLSKIE", 2001), self.outline("ŚLĄSKIE", 2001)
        self.assertTrue(contains(malopolskie["geometry"], *self.SLAWKOW))
        self.assertFalse(contains(slaskie["geometry"], *self.SLAWKOW))
        self.assertTrue(contains(self.outline("ŚLĄSKIE", 2002)["geometry"], *self.SLAWKOW))
        self.assertFalse(contains(self.outline("MAŁOPOLSKIE", 2002)["geometry"], *self.SLAWKOW))

    def test_szerzyny_in_podkarpackie_until_2002(self) -> None:
        for year in (2001, 2002):
            self.assertTrue(contains(self.outline("PODKARPACKIE", year)["geometry"], *self.SZERZYNY))
            self.assertFalse(contains(self.outline("MAŁOPOLSKIE", year)["geometry"], *self.SZERZYNY))
        for year in (2003, None):
            self.assertTrue(contains(self.outline("MAŁOPOLSKIE", year)["geometry"], *self.SZERZYNY))
            self.assertNotIn("parts", self.outline("MAŁOPOLSKIE", year)["properties"])



class TestFeaturesSvg(unittest.TestCase):
    def test_square_degree_at_60n_is_half_as_wide_as_tall(self) -> None:
        # cos(60°) = 0.5: one degree of longitude spans half the width of one degree of latitude.
        square = {"type": "Polygon", "coordinates": [[[20, 59.5], [21, 59.5], [21, 60.5], [20, 60.5], [20, 59.5]]]}
        feature = {"geometry": square, "properties": {"fill": [1, 2, 3, 255], "name": "A & B", "value": 7}}
        svg = features_svg([feature], "Tytuł <x>", [([0, 0, 0], "0–1")], width=220)
        xs, ys = zip(*(map(float, p.split(",")) for p in svg.split('d="M')[1].split("Z")[0].split(" L")))
        self.assertAlmostEqual((max(ys) - min(ys)) / (max(xs) - min(xs)), 2.0, places=2)
        self.assertIn("<title>A &amp; B: 7</title>", svg)
        self.assertIn("Tytuł &lt;x&gt;", svg)



class TestDistrictBorders(unittest.TestCase):
    def test_only_edges_between_districts_and_the_outer_border_are_drawn(self) -> None:
        def square(teryt: str, x: int) -> dict:
            ring = [[x, 0], [x + 1, 0], [x + 1, 1], [x, 1], [x, 0]]
            return {"geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {"teryt": teryt}}

        units = [square("A", 0), square("B", 1), square("C", 2), square("D", 3)]
        lines = district_borders(units, {"A": {1}, "B": {1}, "C": {2}}, "gminy")["geometry"]["coordinates"]
        segments = {tuple(map(tuple, line)) for line in lines}
        self.assertIn(((2, 0), (2, 1)), segments)  # B | C: different districts
        self.assertNotIn(((1, 0), (1, 1)), segments)  # A | B: same district
        self.assertIn(((0, 0), (0, 1)), segments)  # outer edge of A
        self.assertNotIn(((3, 0), (3, 1)), segments)  # C | D: D has no district, so nothing is drawn
        self.assertNotIn(((4, 0), (4, 1)), segments)


if __name__ == "__main__":
    unittest.main()
