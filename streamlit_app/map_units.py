"""Match result rows to boundary features across TERYT changes (see geo/README.md).

Kept free of Streamlit so the matching rules can be unit tested.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

# Simplified boundaries keyed by current TERYT, and old codes mapped to current ones.
GEO_DIR = Path(__file__).resolve().parent / "geo"
# A value column is summed over old units that map to one current unit only when it is a count.
_ADDITIVE = ("votes", "seats", "mandat", "voters", "ballots", "precincts", "residents", "candidates", "count")
_NOT_ADDITIVE = ("pct", "_pp", "turnout", "share", "avg", "rate", "ratio", "rank", "median", "age")


def fold(text: str) -> str:
    """Lowercase without Polish diacritics (for matching voivodeship names)."""
    return unicodedata.normalize("NFKD", text.lower().replace("ł", "l")).encode("ascii", "ignore").decode()


@lru_cache(maxsize=4)
def boundaries(level: str) -> dict:
    """GeoJSON FeatureCollection for ``gminy``, ``powiaty``, ``wojewodztwa`` or ``wojewodztwa_1975``.

    Public-domain features come from ``<level>.json``; features derived from OpenStreetMap (ODbL,
    ``properties.source == "osm"``) from ``<level>_osm.json`` when it exists. Current units come
    first and former extents last, so extents are drawn on top.
    """
    features = json.loads((GEO_DIR / f"{level}.json").read_text(encoding="utf-8"))["features"]
    osm = GEO_DIR / f"{level}_osm.json"
    if osm.exists():
        features = features + json.loads(osm.read_text(encoding="utf-8"))["features"]
    return {"type": "FeatureCollection", "features": sorted(features, key=lambda f: "parts" in f["properties"])}


def is_osm(feature: dict) -> bool:
    """True for a feature derived from OpenStreetMap (needs the ODbL attribution when drawn)."""
    return feature["properties"].get("source") == "osm"


@lru_cache(maxsize=1)
def teryt_changes() -> dict:
    """Old gmina/powiat code -> current code, per boundary level."""
    data = json.loads((GEO_DIR / "teryt_changes.json").read_text(encoding="utf-8"))
    return {level: {old: entry["current"] for old, entry in data[level].items()} for level in ("gminy", "powiaty")}


@lru_cache(maxsize=1)
def pre1999_codes() -> dict:
    """Pre-1999 gmina code (``teryt_gmina_pre1999``) -> key of the gmina feature that covers it.

    A code covered by one current gmina maps to that gmina; a code covered by several (Warsaw dzielnice of
    1990-1994) maps to itself, the key of its former extent. Codes without a cover (Ostrowice) are left out.
    """
    data = json.loads((GEO_DIR / "gminy_pre1999.json").read_text(encoding="utf-8"))
    return {
        old: entry["current"][0] if len(entry["current"]) == 1 else old
        for old, entry in data["codes"].items() if entry["current"]
    }


def voivodeship_level(rows: list[dict]) -> str:
    """``wojewodztwa_1975`` when the rows name the 49 voivodeships of 1975-1998, else ``wojewodztwa``.

    Three names (łódzkie, lubelskie, opolskie) exist in both divisions, so the old map needs a year
    before 1998 (the 1998 local elections already used the 16 voivodeships) or a name that exists only
    in the old division.
    """
    def names(level: str) -> set[str]:
        return {fold(f["properties"]["name"]) for f in boundaries(level)["features"]}

    found = {fold(r["wojewodztwo"]) for r in rows if r.get("wojewodztwo")}
    years = [r["year"] for r in rows if isinstance(r.get("year"), int)]
    if found and found <= names("wojewodztwa_1975") and (found - names("wojewodztwa") or (years and max(years) < 1998)):
        return "wojewodztwa_1975"
    return "wojewodztwa"


def is_additive(column: str) -> bool:
    """True for counts (votes, seats, voters), which may be summed; False for percentages and rates."""
    name = column.lower()
    return any(p in name for p in _ADDITIVE) and not any(p in name for p in _NOT_ADDITIVE)


def feature_key(props: dict, level: str) -> str:
    """Key of a boundary feature in ``unit_values``: TERYT, or folded name for voivodeships."""
    return fold(props["name"]) if level.startswith("wojewodztwa") else props["teryt"]


def unit_values(rows: list[dict], column: str, value: str, level: str) -> tuple[dict, dict, list[str]]:
    """Value per current boundary code.

    Old codes are mapped to the current unit that covers them (``teryt_gmina_pre1999`` through
    ``gminy_pre1999.json``, other codes through ``teryt_changes.json``). When several rows land on one unit,
    their values are summed if the column is additive and the rows come from the same election
    (year, round); otherwise the unit is left out. Returns (values, merged, skipped): ``merged`` maps
    a summed unit to the number of rows in it, ``skipped`` lists the units left out.
    """
    key = fold if level.startswith("wojewodztwa") else str
    changes = pre1999_codes() if column == "teryt_gmina_pre1999" else teryt_changes().get(level, {})
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get(value) is not None:
            code = key(row[column])
            groups[changes.get(code, code)].append(row)
    values, merged, skipped = {}, {}, []
    for code, members in groups.items():
        if len(members) == 1:
            values[code] = members[0][value]
        elif is_additive(value) and len({(r.get("year"), r.get("round")) for r in members}) == 1:
            values[code] = sum(r[value] for r in members)
            merged[code] = len(members)
        else:
            skipped.append(code)
    return values, merged, skipped


def _former_applies(props: dict, codes: set[str], years: set[int], current: set[str]) -> bool:
    """Whether a former extent describes the result (see ``visible_features``)."""
    others = set(props["parts"]) - {props["teryt"]}
    if props["teryt"] not in codes or others & codes:
        return False
    if "years" not in props:
        return True
    first, last = props["years"]
    if years:
        return all(first <= year <= last for year in years)
    # Without a year column only codes tell the periods apart; extents of the 1990s (a gmina before
    # another was split off it) need the year, as later results often lack the split-off gmina too.
    return last >= 1998 and (bool(others) or props["teryt"] not in current)


def visible_features(
    features: list[dict], codes: set[str], years: set[int] | None = None, level: str = "gminy",
) -> list[dict]:
    """Features to draw for the result codes and the result years (``year`` column, if any).

    A feature with ``parts`` is a former extent (Warsaw before its districts are shown, a gmina
    before a new gmina was split off, a powiat before gminas moved in or out). It replaces the
    current units listed in ``parts`` when the result has its own code and none of the other part
    codes. An extent with ``years`` ([first, last]) is valid only then: it is drawn when every result
    year falls in that period, or, without a year column, when its own code no longer exists or
    another part code (a unit created later) is absent from the result.

    OpenStreetMap units that the result does not use (all grey) are drawn as the public-domain former
    extent that covers them instead (Warsaw as one unit, Kamienica and Supraśl before 2025), so a
    map needs the OpenStreetMap attribution only when it colours such a unit.

    Voivodeship codes are folded names (``feature_key``); they are turned into TERYT here, so the
    voivodeship extents (małopolskie with Sławków until 2001, ...) follow the same rules.
    """
    if level.startswith("wojewodztwa"):
        teryt = {feature_key(f["properties"], level): f["properties"]["teryt"] for f in features}
        codes = {teryt.get(code, code) for code in codes}
    current = {f["properties"]["teryt"] for f in features if "parts" not in f["properties"]}
    former = [
        f for f in features
        if "parts" in f["properties"] and _former_applies(f["properties"], codes, years or set(), current)
    ]
    for extent in features:
        props = extent["properties"]
        parts = set(props.get("parts", []))
        if not parts or is_osm(extent) or props["teryt"] in codes or parts & codes:
            continue
        covered = [f for f in features if "parts" not in f["properties"] and f["properties"]["teryt"] in parts]
        if covered and all(is_osm(f) for f in covered):
            former.append(extent)
    drawn = {id(f) for f in former}
    hidden = {code for f in former for code in f["properties"]["parts"]}
    return [
        f for f in features
        if id(f) in drawn or ("parts" not in f["properties"] and f["properties"]["teryt"] not in hidden)
    ]


def _rings(geometry: dict) -> list[list[list[float]]]:
    """Outer and inner rings of a Polygon or MultiPolygon."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [ring for polygon in polygons for ring in polygon]


def features_svg(features: list[dict], title: str, legend: list[tuple[list[int], str]], width: int = 800) -> str:
    """Static SVG of a choropleth: features carry ``properties.fill`` [r, g, b, alpha] and ``name``/``value``.

    Equirectangular projection scaled by the cosine of the mid latitude (fine at Poland's size); the title on
    top and the legend swatches below the map.
    """
    from xml.sax.saxutils import escape

    points = [p for f in features for ring in _rings(f["geometry"]) for p in ring]
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    west, north = min(lons), max(lats)
    k = math.cos(math.radians((min(lats) + north) / 2))
    span_x, span_y = (max(lons) - west) * k, north - min(lats)
    scale = (width - 20) / span_x
    height = span_y * scale + 20
    top, bottom = 40, 30 + 22 * ((len(legend) + 4) // 5)

    def xy(p: list[float]) -> str:
        return f"{10 + (p[0] - west) * k * scale:.1f},{top + 10 + (north - p[1]) * scale:.1f}"

    paths = []
    for f in features:
        r, g, b, a = f["properties"]["fill"]
        d = " ".join("M" + " L".join(xy(p) for p in ring) + "Z" for ring in _rings(f["geometry"]))
        label = escape(f"{f['properties'].get('name', '')}: {f['properties'].get('value', '')}")
        paths.append(f'<path d="{d}" fill="rgb({r},{g},{b})" fill-opacity="{a / 255:.2f}" fill-rule="evenodd" '
                     f'stroke="#64748b" stroke-opacity="0.5" stroke-width="0.4"><title>{label}</title></path>')
    swatches = "".join(
        f'<rect x="{10 + 150 * (i % 5)}" y="{top + height + 12 + 22 * (i // 5)}" width="18" height="12" '
        f'fill="rgb({c[0]},{c[1]},{c[2]})" stroke="#94a3b8"/><text x="{34 + 150 * (i % 5)}" '
        f'y="{top + height + 22 + 22 * (i // 5)}" font-size="12">{escape(text)}</text>'
        for i, (c, text) in enumerate(legend)
    )
    total = top + height + bottom
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{total:.0f}" viewBox="0 0 {width} {total:.0f}" '
            f'font-family="sans-serif"><rect width="100%" height="100%" fill="white"/>'
            f'<text x="10" y="26" font-size="16" font-weight="bold">{escape(title)}</text>{"".join(paths)}{swatches}</svg>')
