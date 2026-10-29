# Boundaries for result maps

Simplified administrative boundaries used by the Streamlit choropleth (`_render_map` in `app.py`).

| File | Units | `properties.teryt` | Matched against result column |
| ---- | ----- | ------------------ | ----------------------------- |
| `gminy.json` | 2477 gminas | 6-digit gmina TERYT (without the type digit) | `teryt_gmina` |
| `powiaty.json` | 380 powiats | 4-digit powiat TERYT | `teryt_powiat` |
| `wojewodztwa.json` | 16 voivodeships | 2-digit TERYT; matched by `name` | `wojewodztwo` (name) |

Source: [jusuff/PolandGeoJson](https://github.com/jusuff/PolandGeoJson) (`data/poland.*.json`,
public domain under the Unlicense), derived from the GUGiK national register of boundaries (PRG).
Preparation: `properties.terc` was cut to the lengths above and renamed `teryt`, coordinates were
rounded to 3 decimals (about 100 m) and repeated points dropped.

Known differences from the mart: Warsaw is one unit (`146501`), while the mart has its 18 districts
(`146502`–`146519`), so Warsaw districts are not coloured; gminas created or merged after 2021 are
missing.
