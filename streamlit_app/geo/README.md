# Boundaries for result maps

Simplified administrative boundaries used by the Streamlit choropleth (`_render_map` in `app.py`,
matching rules in `map_units.py`).

| File | Units | `properties.teryt` | Matched against result column | Licence |
| ---- | ----- | ------------------ | ----------------------------- | ------- |
| `gminy.json` | 2474 gminas, 22 former extents (Warsaw `146501`, Kamienica and Supraśl before 2025, Dukla before 2010, 18 extents of 17 gminas before a gmina was split off in 1994-1998) | 6-digit gmina TERYT (without the type digit) | `teryt_gmina`, `teryt_gmina_pre1999` (through `gminy_pre1999.json`) | public domain |
| `gminy_osm.json` | 18 Warsaw districts, Szczawa, Grabówka, Kamienica and Supraśl since 2025, Warszawa-Centrum `143104`, 6 Warsaw dzielnice of 1990-1994 | as above; the dzielnice by their pre-1999 code (`01001` ...) | as above | ODbL 1.0 |
| `powiaty.json` | 380 powiats, 18 former extents | 4-digit powiat TERYT | `teryt_powiat` | public domain |
| `powiaty_osm.json` | 4 former extents built from Warsaw districts (`1431` and `1465` in 1998, `1465` without Wesoła 1999–2001, `1412` with Wesoła) | as above | `teryt_powiat` | ODbL 1.0 |
| `wojewodztwa.json` | 16 voivodeships, 4 former extents (małopolskie with Sławków and without Szerzyny 1998-2001, without both 2002; śląskie without Sławków 1998-2001; podkarpackie with Szerzyny 1998-2002) | 2-digit TERYT; matched by `name` | `wojewodztwo` (name) | public domain |
| `wojewodztwa_1975.json` | 49 voivodeships of 1975-1998, unions of `gminy.json` polygons | odd 2-digit code of the old register (`01` warszawskie ... `97` zielonogórskie); matched by `name` | `wojewodztwo` (name) when the result is from before 1998 or names an old voivodeship | public domain |
| `teryt_changes.json` | old code → current code | | applied to `teryt_gmina` / `teryt_powiat` before matching | |
| `gminy_pre1999.json` | pre-1999 gmina code → 1999 TERYT and current gmina(s); gmina → 1975-1998 voivodeship | | applied to `teryt_gmina_pre1999` before matching | |

District borders (`map_units.district_borders`) rely on neighbouring features sharing their border vertices exactly: an edge of two gminas in different districts is drawn, an edge of one gmina is the national border.

`map_units.boundaries` merges `<level>.json` with `<level>_osm.json`, so the map uses both (2479
gminas, Warsaw as its 18 districts).

Voivodeship results use `wojewodztwa_1975.json` when every name is one of the 49 old voivodeships and
either a `year` is before 1998 or a name exists only in the old division (`map_units.voivodeship_level`;
łódzkie, lubelskie and opolskie exist in both, and the 1998 local elections already used the 16).

Sources and licences:

- `gminy.json`, `powiaty.json`, `wojewodztwa.json`, `wojewodztwa_1975.json`: public domain.
  [jusuff/PolandGeoJson](https://github.com/jusuff/PolandGeoJson) (`data/poland.*.json`, released
  under the Unlicense), derived from the GUGiK national register of boundaries (PRG), state of 2021.
  `properties.terc` was cut to the lengths above and renamed `teryt`, coordinates were rounded to 3
  decimals (about 100 m) and repeated points dropped. Former extents in these files are the original
  polygons or unions of them; `wojewodztwa_1975.json` and the voivodeship extents are built only from
  these polygons (unions of gminas, or a current voivodeship plus or minus Sławków or Szerzyny), so
  they stay public domain.
- `gminy_osm.json`, `powiaty_osm.json`: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, licensed under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/);
  keep this attribution when redistributing these files. Every feature has `properties.source = "osm"`.
  Boundary relations with `teryt:terc` (fetched 2026-10-29) for the Warsaw districts and for Szczawa
  (`120713`, from Kamienica, Dz.U. 2024 poz. 1453) and Grabówka (`200216`, from Supraśl, Dz.U. 2024
  poz. 1454), clipped to the PRG polygon of Warsaw, Kamienica or Supraśl so the outer borders match
  the neighbours, simplified along shared borders and rounded to 3 decimals. The Warsaw dzielnice of
  1990-1994 are unions of district polygons, so they are ODbL too. Kamienica and Supraśl
  since 2025 are the PRG polygons minus the new gminas, and the Warszawa-Centrum and Warsaw-area
  powiat extents are unions of district polygons, so they are ODbL too. The map shows the caption
  "© OpenStreetMap contributors (ODbL)" whenever it draws one of these features; when a result uses
  none of them, the public-domain extents (Warsaw as one unit, Kamienica and Supraśl before 2025) are
  drawn in grey instead.

Former extents: a feature with `properties.parts` is the area of a unit at an earlier time: Warsaw
`146501` as one unit, gmina Warszawa-Centrum `143104` (sejmik 1998), Kamienica and Supraśl before
2025, and powiats before gminas moved in or out (the 2002 powiats, Sławków 2002, Tarczyn and Szerzyny
2003, Warsaw and Wesoła 2002, Wałbrzych 2003-2012, Rejowiec 2006), voivodeships before Sławków (2002)
and Szerzyny (2003) moved, and gminas before a gmina was split off (see below). It replaces the current units in
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

Gminas before 1999 (`gminy_pre1999.json`): rows of 1990-1997 carry the 5-digit code of the old register
in `teryt_gmina_pre1999` (2-digit voivodeship of 1975-1998, 3-digit gmina number). No official key from
these codes to TERYT exists (the 1998 acts, Dz.U. 1998 nr 96 poz. 603 and nr 103 poz. 652, and the
1999 identifiers, Dz.U. 1998 nr 157 poz. 1031, list only the new units; GUS TERYT files start on
1999-01-01). Each code was matched to the GUS BDL gmina of the same year (`mart.gus_units`, 1995-1998,
1999 TERYT codes) by name and gmina type, unique in Poland or within the 1999 powiats of the old
voivodeship's other gminas, and checked to touch the other gminas of its voivodeship; renames since 1990
come from GUS, Komunikat o zmianach w podziale terytorialnym (1977-1998), each with its act. All 2527
codes in the mart are listed: 2526 map to current gminas (2519 by name, the 7 Warsaw dzielnice of
1990-1994 to their districts per the 1994 Warsaw act), Ostrowice is left out (split in 2019). `method`,
`sources` and per-code `match` in the file document this. A code with one current gmina maps to it;
a code with several maps to its own extent in `gminy_osm.json`.

Gminas split off between 1994 and 2010 (Pszów, Radlin, Izabelin, Radzionków, Jaśliska and 17 others,
`later_gminas`) were part of another gmina before; the parent gets a former extent with the child for
those years (e.g. Bytom with Radzionków 1990-1997), so a 1990s map colours the parent's area of the time.
Extents that end before 1998 are used only when the result has a `year`. In 1991 the KBW file has no row
for 24 rural gminas (their precincts are under the town of the same name) and for 22 gminas created in
1991-1993 (`year_notes`); those stay grey.

The 49 voivodeships are unions of the current gminas assigned to them (`voivodeships`): every gmina of
`gminy.json` (Warsaw, Kamienica and Supraśl as one unit each) belongs to exactly one, through its 1990s
code or the gmina it was split from; none is left unassigned. Changes of the 1999 gminas since then:
renumbered (2002 powiats, Sławków, Tarczyn, Szerzyny, Wałbrzych, Rejowiec, Warsaw gminas and Wesoła,
see `teryt_changes.json`), merged (Zielona Góra 2015, counts summed), split (Ostrowice 2019, left out at
gmina level; Drawsko Pomorskie and Złocieniec lie in the same old voivodeship), created (Jaśliska 2010
from Dukla; Szczawa and Grabówka 2025, drawn through the Kamienica and Supraśl extents, now valid from
1990), renamed (e.g. Słupsk gm. → Redzikowo, matched by the 1998-2002 PKW names). Boundary corrections
between gminas after 1998 are not modelled; the old voivodeships follow today's gmina borders.
