# Boundaries for result maps

Simplified administrative boundaries used by the Streamlit choropleth (`_render_map` in `app.py`,
matching rules in `map_units.py`).

| File | Units | `properties.teryt` | Matched against result column | Licence |
| ---- | ----- | ------------------ | ----------------------------- | ------- |
| `gminy.json` | 2474 gminas, 3 former extents (Warsaw `146501`, Kamienica and Supraśl before 2025) | 6-digit gmina TERYT (without the type digit) | `teryt_gmina` | public domain |
| `gminy_osm.json` | 18 Warsaw districts, Szczawa, Grabówka, Kamienica and Supraśl since 2025, Warszawa-Centrum `143104` | as above | `teryt_gmina` | ODbL 1.0 |
| `powiaty.json` | 380 powiats, 18 former extents | 4-digit powiat TERYT | `teryt_powiat` | public domain |
| `powiaty_osm.json` | 4 former extents built from Warsaw districts (`1431` and `1465` in 1998, `1465` without Wesoła 1999–2001, `1412` with Wesoła) | as above | `teryt_powiat` | ODbL 1.0 |
| `wojewodztwa.json` | 16 voivodeships | 2-digit TERYT; matched by `name` | `wojewodztwo` (name) | public domain |
| `teryt_changes.json` | old code → current code | | applied to `teryt_gmina` / `teryt_powiat` before matching | |

`map_units.boundaries` merges `<level>.json` with `<level>_osm.json`, so the map uses both (2479
gminas, Warsaw as its 18 districts).

Sources and licences:

- `gminy.json`, `powiaty.json`, `wojewodztwa.json`: public domain.
  [jusuff/PolandGeoJson](https://github.com/jusuff/PolandGeoJson) (`data/poland.*.json`, released
  under the Unlicense), derived from the GUGiK national register of boundaries (PRG), state of 2021.
  `properties.terc` was cut to the lengths above and renamed `teryt`, coordinates were rounded to 3
  decimals (about 100 m) and repeated points dropped. Former extents in these files are the original
  polygons or unions of them.
- `gminy_osm.json`, `powiaty_osm.json`: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, licensed under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/);
  keep this attribution when redistributing these files. Every feature has `properties.source = "osm"`.
  Boundary relations with `teryt:terc` (fetched 2026-10-29) for the Warsaw districts and for Szczawa
  (`120713`, from Kamienica, Dz.U. 2024 poz. 1453) and Grabówka (`200216`, from Supraśl, Dz.U. 2024
  poz. 1454), clipped to the PRG polygon of Warsaw, Kamienica or Supraśl so the outer borders match
  the neighbours, simplified along shared borders and rounded to 3 decimals. Kamienica and Supraśl
  since 2025 are the PRG polygons minus the new gminas, and the Warszawa-Centrum and Warsaw-area
  powiat extents are unions of district polygons, so they are ODbL too. The map shows the caption
  "© OpenStreetMap contributors (ODbL)" whenever it draws one of these features; when a result uses
  none of them, the public-domain extents (Warsaw as one unit, Kamienica and Supraśl before 2025) are
  drawn in grey instead.

Former extents: a feature with `properties.parts` is the area of a unit at an earlier time: Warsaw
`146501` as one unit, gmina Warszawa-Centrum `143104` (sejmik 1998), Kamienica and Supraśl before
2025, and powiats before gminas moved in or out (the 2002 powiats, Sławków 2002, Tarczyn and Szerzyny
2003, Warsaw and Wesoła 2002, Wałbrzych 2003-2012, Rejowiec 2006). It replaces the current units in
`parts` when the result has its own code and none of the other part codes. With `properties.years`
([first, last]) it is drawn only when every `year` in the result falls in that period; a result
without a `year` column uses it only when its own code no longer exists or a part code created later
is absent. Extents of 1999–2001 also cover 1998, whose files already use the 1999 codes; the
1998 Warsaw gminas (`1431xx`, councils) map to their districts. So EP 2004 still colours Warsaw as one unit, and a 2001 powiat map shows powiat nowosolski
with Wschowa. Powiat extents are unions of the current gmina polygons in the powiat's composition of
the time, taken from the mart data; `teryt_changes.json` (`former_extents`) lists them with sources.

Old codes (`teryt_changes.json`): each entry names the act that changed the code. When several result
rows map to one current unit (gmina Zielona Góra merged into the city in 2015), counts such as votes
are summed and the caption says so; percentages and turnout are not summed, so the unit stays grey
and the caption names it. Codes listed under `unmapped` (ships, Ostrowice split in 2019 between two
gminas) are not drawn; abroad precincts have no TERYT in the mart.

To find codes the boundaries do not cover after loading new years, compare the distinct
`teryt_gmina` of `mart.precincts`, `mart.pe_precinct_protocols`, `mart.prezydent_precinct_protocols`,
`mart.sejmik_gmina_protocols`, `mart.wbp_gmina_protocols` and `mart.v_gmina_continuity` (and their
first four digits for powiats) with `gminy.json` and `powiaty.json`, and add each new code to
`teryt_changes.json` with its source.
