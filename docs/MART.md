# Relational election mart (`mart` schema)

The `mart` schema holds official election results as ordinary relational tables: one row per
precinct (or gmina), candidate, list or district, with named columns instead of raw CSV headers in
JSONB. Every election is checked against the official PKW totals before it is loaded.

It exists so that questions do not need a hand-written SQL template each. An LLM writes SQL over a
small, documented schema, and guards keep that SQL read-only and inside `mart` (see
[Answering questions](#answering-questions)).

## What is loaded

| Election (`mart.elections.election`) | Years | Detail |
| ------------------------------------ | ----- | ------ |
| `sejm` | 1991, 1993, 1997, 2001, 2005, 2007, 2011, 2015, 2019, 2023 | precinct: lists, candidates, protocols (1991–2005: no per-precinct candidate votes; 1991–1997: 69 national-list seats as district 0, pre-1999 geography, candidate age `age`; 1997 national-list positions checked against the registered national lists) |
| `senat` | 1991, 1993, 1997, 2001, 2005, 2007, 2011, 2015, 2019, 2023 | precinct: candidates, protocols (1991: candidates only; 1991–1997: 49 voivodeship districts; 2005: candidates with totals only, results published per powiat) |
| `prezydent` (both rounds; 2000 one round) | 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025 | precinct: candidates, protocols |
| `pe` (European Parliament) | 2004, 2009, 2014, 2019, 2024 | precinct: lists, candidates, protocols (2004, 2009: gmina rows, no candidate votes below district) |
| `referendum` | 1996 (5 questions, two ballots), 1997, 2003, 2015, 2023 | precinct: answers per question |
| `sejmik` (voivodeship assembly) | 1998, 2002, 2006, 2010, 2014, 2018, 2024 | gmina: list votes, protocols; candidates with totals |
| `wbp` (wójt / burmistrz / prezydent miasta, both rounds) | 2002, 2006, 2010, 2014, 2018, 2024 | gmina: candidates, protocols |
| `rada` (councils: rada gminy / miasta, rada powiatu, rady dzielnic Warszawy; 1994: gmina councils only, pre-1999 codes; 1998: Rada m.st. Warszawy and the Warszawa-Centrum districts) | 1994, 1998, 2002, 2006, 2010, 2014, 2018, 2024 | district: candidates with totals, lists (2006: list blocs), seats |

Councils are loaded at district level only (no per-precinct council results); list totals are the
sum of their candidates' votes (see [Known gaps](#known-gaps-in-the-source-data)).
`mart.elections` lists what a database actually contains, with dates; `GET /health?details=true`
shows it too.

The build output matches the official results. Examples (from the built mart):

| Check | Result |
| ----- | ------ |
| Sejm: PiS votes / seats | 2011: 4,295,016 / 157 · 2015: 5,711,687 / 235 · 2019: 8,051,935 / 235 · 2023: 7,640,854 / 194 |
| Sejm 1991 / 1993 / 1997 | 1991: UD 1,382,051 / 62, SLD 60, WAK 49, PSL 48, KPN 46, POC 44, KLD 37, PL 28, Solidarność 27, PPPP 16, MN 7; turnout 43.20% · 1993: SLD 2,815,169 / 171, PSL 132, UD 74, UP 41, KPN 22, BBWR 16, MN 4; 52.08% (mart 52.13%, see gaps) · 1997: AWS 4,427,373 / 201, SLD 164, UW 60, PSL 27, ROP 6, MN 2; 47.93% |
| Senate seats 1991–1997 | 1991: UD 21, Solidarność 11, WAK 9, POC 9, PSL 7, KLD 6, PL 5, KPN 4, SLD 4 · 1993: SLD 37, PSL 36, Solidarność 9, UD 4 · 1997: AWS 51, SLD 28, UW 8, ROP 5, PSL 3 |
| Sejm 2001 | SLD-UP 5,342,519 (41.04%, 216), PO 65, Samoobrona 53, PiS 44, PSL 42, LPR 38, MN 2; AWSP 5.60% and UW 3.10% below the threshold; turnout 46.29% |
| Sejm 2005 / 2007 | 2005: PiS 3,185,714 (26.99%, 155), PO 133, Samoobrona 56, SLD 55, LPR 34, PSL 25, MN 2 · 2007: PO 6,701,010 (41.51%, 209), PiS 166, LiD 53, PSL 31, MN 1 |
| Sejm 2011 | PO 5,629,773 (39.18%, 207), RP 40, PSL 28, SLD 27, MN 1 |
| Sejm turnout | 2005: 40.57% · 2007: 53.88% · 2011: 48.92% · 2015: 50.92% · 2019: 61.74% · 2023: 74.38% |
| Senate seats | 2001: SLD-UP 75, Blok Senat 2001 15, PSL 4, LPR 2, Samoobrona 2, voters' committees 2 · 2005: PiS 49, PO 34, LPR 7, Samoobrona 3, PSL 2, voters' committees 5 · 2007: PO 60, PiS 39, Cimoszewicz 1 · 2011: PO 63, PiS 31, PSL 2, independents 4 · 2015: PiS 61, PO 34 · 2019: PiS 48, KO 43 |
| President | 2000: Kwaśniewski 53.90%, elected in round 1, turnout 61.12% |
| President 1990 / 1995 | 1990: round 1 Wałęsa 6,569,889, Tymiński 3,797,605, Mazowiecki 2,973,264; round 2 Wałęsa 10,622,696, Tymiński 3,683,098 · 1995: round 1 Kwaśniewski 6,275,670, Wałęsa 5,917,328; round 2 Kwaśniewski 9,704,439, Wałęsa 9,058,176 |
| President, round 2 | 2005: Kaczyński 54.04% · 2010: Komorowski 53.01% · 2015: Duda 51.55% · 2020: Duda 51.03% (turnout 68.18%) · 2025: Nawrocki 50.89% (turnout 71.63%) |
| European Parliament | 2004: PO 24.10% / 15, LPR 15.92% / 10, 54 seats, turnout 20.87% · 2009: PO 44.43% / 25, PiS 27.40% / 15, 50 seats, turnout 24.53% · 2014: PO 32.13% / 19, PiS 31.78% / 19, 51 seats · 2019: PiS 45.38% / 27 seats, KE 38.47% / 22 · 2024: KO 37.06% / 21, PiS 36.16% / 20 |
| Referendum | 1996: question 1 (uwłaszczenie) turnout 32.40%, yes 8,580,129 · 1997 (constitution): yes 6,396,641, no 5,570,493 · 2003 (EU accession): "yes" 77.45%, turnout 58.85% · 2015: JOW "yes" 78.75%, turnout 7.80% · 2023: "no" 94.61–96.79%, turnout 40.91% |
| Sejmiki | 2018: PiS 34.13% / 254, KO 26.97% / 194, PSL 70 · 2024: PiS 34.27% / 239, KO 30.59% / 210, TD 80 (552 seats each) |
| Sejmiki 1998 | AWS 342, SLD 329, PS (PSL-UP-KPEiR) 89, UW 76, MN 13 (855 seats in 127 districts), turnout 45.35% |
| Sejmiki 2002 / 2006 | 2002: SLD-UP 189, Samoobrona 101, LPR 92, POPiS 79, PSL 58, PiS 10, MN 7 (561 seats), turnout 44.23% · 2006: PO 186, PiS 170, PSL 83, LiD 66, Samoobrona 37, LPR 11, MN 7 (561 seats), turnout 45.91% |
| Sejmiki 2010 / 2014 | 2010: PO 222, PiS 141, PSL 93, SLD 85 (561 seats), turnout 47.26% · 2014: PO 179, PiS 171, PSL 157, SLD 28, MN 7 (555 seats), turnout 47.21% |
| Mayors | 2002: 2,478 gminas, 1,202 run-offs · 2006: 2,477 gminas, 843 run-offs (827 + 16 on 10 December) · 2010: 2,479 gminas, 738 run-offs (728 on 5 December + 10 on 19 December) · 2014: 2,477 gminas, 890 run-offs · 2018: 2,477 gminas, 649 run-offs · 2024: 2,477 gminas, 748 run-offs |
| Councils, seats | 2002: ≤20k 32,205, >20k 5,629, cities incl. Warsaw 1,745, powiat 6,294, Warsaw districts 399 · 2006: ≤20k 32,265, >20k 5,566, cities 1,707, powiat 6,284, Warsaw districts 407 · 2010: ≤20k 32,280, >20k 5,568, cities 1,701, powiat 6,290, Warsaw districts 409 · 2014: gmina 37,842 + cities 1,694, powiat 6,276, Warsaw districts 423 · 2018: ≤20k 32,175, >20k 7,351, powiat 6,244, Warsaw districts 425 · 2024: ≤20k 32,370, >20k 7,046, powiat 6,170, Warsaw districts 420 |

### GUS context statistics (`gus` family)

Not an election. `scripts/fetch_gus_bdl.py` downloads official GUS statistics from the Local Data
Bank (BDL) API into `data/gus/bdl_values.parquet` (committed, 1.7 MB) and `data/gus/bdl_variables.csv`;
`build_gus.py` loads them per year 1995–2024 into `mart.gus_units` (unit name, voivodeship, powiat,
`gmina_type` from the 7th TERYT digit: miejska / wiejska / miejsko-wiejska / dzielnica Warszawy) and
`mart.gus_indicators` (long format: population, urban/rural population, area in ha, density,
registered unemployed and their share of the working-age population, the unemployment rate and the
average gross salary at powiat level and above, shares of pre-working / working / post-working age,
gmina budget income per resident, and census education: residents aged 13+ and with higher education
from NSP 2021 by gmina and NSP 2011 by powiat, stored in the census year only; NSP 2002 is not in BDL by
unit); `v_gus_units` is the wide form (`higher_education_pct`). Units join elections
by TERYT or by `unit_key` (as in `v_party_results`); statistics refer to 31 December, so compare an
election with the closest GUS year (October 2023 → 2023, June 2025 → 2024). The family writes no
`mart.elections` rows and is built after the election families (its TERYT check reads their tables).
Checks: unique keys; every TERYT appears in the election tables or `build_gus.NOT_IN_ELECTIONS`; gminas
add up to their powiat, powiats to their voivodeship, voivodeships to Poland and Warsaw's districts to
the city; urban + rural = population; age shares sum to 100; census higher education ≤ residents 13+. GUS words in a question add the GUS relations to the prompt but
do not narrow it (`dynamic_sql.ADD_ONLY_PREFIXES`), and such questions never fall back to the Sejm
templates.

## Data model

Every table has `year` in its key; two-round elections also have `round`. Elections that publish
per-precinct files carry the precinct geography (TERYT, gmina, powiat, województwo) in their
protocol table. Sejm and Senate share `precincts`; from 2011 it also keeps each precinct's published
area (`boundary_description`: streets with house-number ranges or villages), which `GET /precincts/find`
searches for the „mój obwód” field of the „Wyniki obwodowe” panel.

| Family | Tables |
| ------ | ------ |
| Sejm + Senate | `precincts`, `sejm_districts`, `sejm_committees`, `sejm_district_lists`, `sejm_candidates`, `sejm_precinct_protocols`, `sejm_precinct_list_votes`, `sejm_precinct_candidate_votes`, `senat_districts`, `senat_candidates`, `senat_precinct_protocols`, `senat_precinct_candidate_votes` |
| President | `prezydent_candidates` (official national totals), `prezydent_precinct_protocols`, `prezydent_precinct_candidate_votes` |
| European Parliament | `pe_districts`, `pe_committees`, `pe_district_lists`, `pe_candidates`, `pe_precinct_protocols`, `pe_precinct_list_votes`, `pe_precinct_candidate_votes` |
| Referendum | `referendum_questions` (texts from the referendum orders), `referendum_precinct_protocols`, `referendum_precinct_answers` |
| Sejmik | `sejmik_committees`, `sejmik_district_lists`, `sejmik_candidates`, `sejmik_gmina_protocols`, `sejmik_gmina_list_votes` |
| Mayors | `wbp_gmina_protocols` (office, per round), `wbp_candidates` (votes, `votes_against` for single candidates, `age`) |
| Councils | `rada_committees`, `rada_districts` (`council_level` gmina/powiat/dzielnica, `voting_system`, seats, valid votes), `rada_district_lists`, `rada_candidates` (`age`) |
| Shared | `elections`, `persons`, `entity_lexicon` |
| GUS statistics (not elections) | `gus_units`, `gus_indicators` |

Committees carry `aliases` (`{PiS,Zjednoczona Prawica}`, `{KO,PO,Platforma Obywatelska}`), assigned
by `COMMITTEE_ALIASES` in `build_common.py` (first matching needle wins; needles are specific, e.g.
`PARTIA RAZEM`, not `RAZEM`, so local committees such as "ZAWSZE RAZEM" get no alias). Rules for one
year (`YEAR_COMMITTEE_ALIASES`) come first; they separate committees with the same name, e.g. the 2010
sejmik committee "KOALICYJNY KOMITET WYBORCZY LEWICA" (`KKW Lewica`, not SLD) from the 2024 Lewica
committee (`Lewica`). Compare parties across years by alias, not by name.

### Views

Domain logic lives in views so that nobody (person or LLM) re-derives it. All percentages are 0–100.
Rollup views have a `level` column (`country`, `wojewodztwo`, `powiat`, `gmina`); abroad and ships
count only in `country`.

| View | Content |
| ---- | ------- |
| `v_sejm_national_results` | Sejm national votes, percent, `threshold_pct`, `passed_threshold`, seats |
| `v_sejm_list_results`, `v_sejm_turnout` | Sejm list support and turnout per level (also `sejm_district`) |
| `v_prezydent_results` | candidate votes and percent per round and level |
| `v_pe_national_results` | EP national result with 5% threshold and seats |
| `v_referendum_results` | turnout, yes/no and percentages per question and level |
| `v_sejmik_results` | sejmik committee support per level; seats at country and voivodeship level |
| `v_rada_results` | council committee votes and seats per council, voivodeship and country (`council_level`; `votes_exact = false` where struck-off candidates make list totals unknown) |
| `v_turnout` | official turnout of **every** election per level (`election`, `year`, `round`; `turnout_basis` says whether PKW counted ballots issued or valid ballots) |
| `v_precinct_results` | plain (not materialized) view: votes per list or candidate in each precinct (Sejm, president, EP) with protocol counts, `short_name` and official `turnout_pct`; filter by election, year and `teryt_gmina` (`GET /precincts`, panel „Wyniki obwodowe”) |
| `v_candidate_gmina_votes` | plain view: a Sejm (2007+), Senate (2001+) or EP (2014+) candidate's votes per gmina summed from precinct files, with the gmina's valid votes and `pct`; filter by election, year and `person_id`/`candidate_id` (`GET /persons/{id}/gminas`, person card „Głosy w gminach”) |
| `v_party_results` | party families across Sejm, EP and sejmik per level (`unit_key` joins elections) |
| `senat_byelections` | table rebuilt with the views from `data/pkw_html/senat_byelections.csv` (PKW result announcements read by `scripts/scrape_senat_byelections.py`): every Senate by-election 2003–2025, one row per candidate with the district's totals, `turnout_pct`, `pct` and `elected`; the build fails when candidate votes do not sum to the valid votes or the winner lacks the most votes; built before persons are linked, so candidates get `person_id` and appear in `v_person_history` as `senat_uzup` |
| `local_referenda` | table rebuilt with the views from `data/pkw_html/local_referenda.csv` (PKW's result protocols on the wybory.gov.pl term sites, read by `scripts/fetch_local_referenda.py`): local referendums to recall a mayor, a gmina council or a powiat council, terms 2014–2018, 2018–2024 and 2024–2029, one row per question, with `turnout_pct`, `yes_pct`, `is_valid` (3/5 of the voters of the organ's election took part) and `recalled`; the build fails when yes + no ≠ valid votes or the commission's validity or outcome statement disagrees with the votes. Referendums on other local matters and recall referendums before 2015 are not published by PKW |
| `v_party_change` | change against the previous election of the same kind, with a `comparable` flag |
| `v_gmina_continuity` | which gmina TERYT codes appear in which elections |
| `v_person_history` | every candidacy of a person (materialized, indexed by `person_id` and by `full_name` trigrams) |
| `v_gus_units` | GUS statistics per unit and year in wide form: population, urban share, area, density, unemployment, salary, age structure, gmina income per resident, census higher education (2011, 2021) |
| `v_seat_methods` | Sejm and sejmik seats per committee under D'Hondt, Sainte-Laguë and modified Sainte-Laguë, with the official thresholds |
| `party_families` (table) | lead alias → party family, with a Polish note on the lineage |
| `gmina_codes_pre1999` (table) | 1975–1998 gmina code → 1999 TERYT, today's voivodeship and powiat, current gmina codes (from `streamlit_app/geo/gminy_pre1999.json`) |

Cross-election rules:

- **Party families** (`PARTY_FAMILIES` in `derived.py`, queryable as `mart.party_families` with a note
  per alias): PO, KO and Nowoczesna are `KO`; PiS, Solidarna Polska and Polska Razem are `PiS`; SLD,
  LiD, ZL, NL, Wiosna and Lewica are `Lewica`; Razem and Lewica Razem are `Razem`; PSL and TD are
  `TD/PSL`; Konfederacja, KORWiN (Korwin-Mikke's committees 2005–2015), KNP and RN are
  `Konfederacja`; UW and PD are `UW/PD`; KE (EP 2019 coalition) stays `KE`; other first aliases are
  their own family; committees without aliases are `other`. `lead_aliases` shows which committees
  formed the family in that election (e.g. `PiS+Polska Razem+Solidarna Polska` in EP 2014).
- **`comparable`** in `v_party_change` is true only when the family ran under the same lead alias both
  times. PO 2015 → KO 2019 and PSL 2019 → TD 2023 are false: say so when reporting such a change.
- **TERYT continuity.** Units are matched by TERYT. Warsaw is split into its 18 districts (1465xx)
  in every election except mayors, where the city is one gmina (146501). Gminas created or dissolved
  between 2015 and 2025 (Ostrowice dissolved in 2019; Szczawa and Grabówka created in 2025) have
  `in_all_elections = false` in `v_gmina_continuity` and are absent from `v_party_change` where they
  did not exist.

Every table, view and column has a PostgreSQL `COMMENT` in English. These comments are the schema
documentation: `dynamic_sql.schema_description()` sends them to the LLM. Write a comment for every
new column in `schema.py` (tables) or `derived.py` (views).

### Persons

PKW files carry no personal identifier. National candidacies (Sejm, Senate, President, European
Parliament, Senate by-elections) with the same normalized full name and gender are one person. Each
by-election is its own election event; its announcement gives no gender or residence, so the gender
comes from other candidacies of the name. When a name is shared by
several candidates within one election event (Sejm+Senate of a year, a presidential round, an EP
election), residence must match too; namesakes of one event with the same residence text are a person
each. A missing gender (2015 presidential files) is taken from other candidacies with the same name
when they agree. Three more rules, measured against the ages in the 1991–1997 Sejm and Senate
registers (`age`; the true birth year is year − age or one less):

- **One given name, residences in different voivodeships** (`name+gender+voivodeship`): split when
  no candidacies of the two sides have alike occupations (trigram similarity ≥ 0.3, or a student),
  the same place text or birth years at most 1 apart. Warsaw and unmapped residences fit any side;
  such candidacies join the part nearest in time. Names with a presidential candidacy (no residence
  before 2020) and names with two given names (there a new voivodeship nearly always means a move)
  are not split. 82–92% of split pairs with ages differ by more than 3 years.
- **Birth years more than 3 apart** (`name+gender+birth year`): split, unless the candidacies share a
  powiat or place (the 1990s registers are off by up to 3 years for the same person; 2% of same-place
  pairs differ by more).
- **Abbreviated given names** (`surname+first given name+gender`): files before 2011 often give only
  the first given name ("Donald TUSK" in 1991, "Donald Franciszek TUSK" later; 65–75% of unique
  matches live in the same powiat before 2011, 5–10% from 2011). Candidacies before 2011 of a person
  with one given name join the one person with more given names, the same surname, gender and first
  given name, when they never share an election event, live in no conflicting voivodeships (as above),
  have no birth years more than 3 apart, and one pair lives in the same powiat or place or has birth
  years at most 1 apart (98% right on 1990s ages). A candidacy from 2011 with one given name is a
  namesake: it must share an election event with the fuller person, otherwise nothing joins.
  `persons.match_rule` records the rule.

Local candidates (sejmik, mayors, councils) join a national person only when exactly one national
person has that name and gender and the name occurs once in that local election (once among all
council candidates of the year). When several national persons share the name, a local candidacy
joins the one whose residence lies in the same powiat (sejmik: voivodeship), if exactly one does and
the name occurs once in that powiat (voivodeship) in that local election. Residence is
`residence_teryt` where published (national registers from 2019); before 2019 the residence text is
mapped to a powiat through place names published with a TERYT since 2019: a place maps to a powiat
when at least 90% of its residence text + `residence_teryt` pairs have that powiat (98% right on
2023–2025 places learned from 2019–2020, also maps Warsaw and other cities); the village part of
"X, gm. Y" is compared as text only (mapping it put 41% of villages into same-name towns). Local registers also publish the age on
election day (`age`), national registers only in 1991–1997. All local candidacies linked to one person must
agree on the birth year (year − age, at most 1 apart); otherwise the name belongs to several people
and none of that person's local candidacies is linked. Local candidacies linked to a national
person must also have a birth year that allows the person's national candidacies (at least 21 for
Sejm and EP, and for the Senate until 1997; 30 for the Senate from 2001; 35 for President) and that
is within 3 years of national ages where published; otherwise none of that person's local
candidacies is linked. `v_person_history.age` shows the age.

**Local persons.** Local candidacies still unlinked are grouped by name, gender and powiat (TERYT of
the gmina, powiat or Warsaw district, first four digits) into runs of birth years at most 1 apart.
A run that contains candidacies of exactly one national person joins that person; a run without
one becomes a local person (`match_rule = 'local:name+gender+birth year+powiat'`); a run with
several national persons stays unlinked. Sejmik candidacies (voivodeship-wide) join the single
matching run in their voivodeship, or form runs of their own (`'local:...+voivodeship'`). So a wójt
who was a councillor in 2014 and mayor in 2018 and 2024 is one person in `v_person_history` without
any national candidacy. Two namesakes in one powiat born within a year of each other are merged.
Some local files before 2010 give only the first given name ("Artur KOZIOŁ", mayor of Wieliczka in
2006, is "Artur Stanisław KOZIOŁ" from 2010): such a candidacy takes the full name of the single name
with the same surname, first given name, gender and powiat and a birth year at most 1 apart from
another election, before the runs are formed.
Only elected mayors and sejmik councillors among local persons are in `entity_lexicon` (hundreds of
thousands of council candidates would make every surname ambiguous); others are found by SQL on
`full_name`.

**Colloquial names** live in `api/services/mart/entity_aliases.csv` (`kind,alias,target,note`) and
are added to `entity_lexicon` after every rebuild, so the lexicon stays the one place that turns
names into keys. A committee alias ("Platforma", "Konfa", "ludowcy", "Koalicja Obywatelska") stands
for every committee with the target alias; a person alias ("Olek KWAŚNIEWSKI", or a surname part
used alone such as "KORWIN") points at the person of the target candidacy (`<full name>|<chamber>
<year>`) and shares its `person_id`, so it never becomes a second clarification option. An alias
that matches nothing fails the build; every row needs a `note`.

**Manual corrections** live in `api/services/mart/person_overrides.csv`:

```csv
election,year,full_name,place,person,note
sejm,2023,Jan KOWALSKI,5,jan-kowalski-2023,not the 2019 Jan KOWALSKI (different residence)
sejm,2019,Ewa MALINOWSKA,,ewa-malinowska,changed surname
rada,2018,Ewa WIŚNIEWSKA,020105,ewa-malinowska,changed surname
```

All candidacies with the same `person` label are one person (`match_rule = 'manual'`); a label used
by one candidacy splits it from its namesakes. `place` narrows the match: district number (sejm,
senat, pe), voivodeship (sejmik), TERYT (wbp: gmina, rada: council unit); empty means every
candidacy of that name in that election and year. A local candidacy joins the person of the national
candidacies in its group (no national candidacy: unlinked). A row that matches no candidacy fails
the build. Overrides win over every automatic rule.

Limits: namesakes with one given name who never ran in different voivodeships, or whose occupations
happen to be alike, can still be merged; movers whose occupation changed are split (overrides:
Hofman, Kośla, Hatka); people who changed surname are linked only by an override.

Current build: 60,709 national persons (56,448 by name and gender, 2,348 with abbreviated given
names joined, 1,016 by residence, 728 split by voivodeship, 163 by birth year, 6 manual) and
1,145,815 local persons.

## Building it

The `loader` service (`docker compose --profile tools run --rm loader`) does everything. By hand:

```bash
# 1. Stage sources to Parquet (strict: a file that would lose rows fails).
python3 scripts/kbw_stage_duckdb.py --root data/kbw_mirror/dane --out data/kbw_stage_parquet
for d in data/pkw_all/sejmsenat*/zip; do
  python3 scripts/kbw_stage_duckdb.py --root "$d" --out data/pkw_stage_parquet
done

# 2. Build the mart (about 9 minutes for everything serially, under 8 with --jobs 3 on 4 CPUs; about 4 of them are persons, views and the seat check).
DATABASE_URL=postgresql://plujka:plujka@localhost:5432/plujka python3 scripts/build_mart.py
python3 scripts/build_mart.py --years 2023                  # every family of one year
python3 scripts/build_mart.py --elections prezydent,pe      # some families, all years
MART_DUCKDB_MEMORY_LIMIT=3GB python3 scripts/build_mart.py --jobs 3   # families in parallel processes (CI)
python3 scripts/build_mart.py --elections pe --skip-derived  # load only; persons and views on the next full run

# 3. Check the curated SQL examples against the new build.
MART_DATABASE_URL=postgresql://plujka_mart_reader:...@localhost:5432/plujka python3 scripts/add_sql_example.py --check
```

`build_mart.py` groups elections in families built from the same files: `parliament` (Sejm+Senate),
`prezydent`, `pe`, `referendum`, `samorzad` (sejmik, mayors and councils). Each family-year is built in its own
in-memory DuckDB, checked, and loaded into PostgreSQL in one transaction (its tables and its
`mart.elections` rows). Family-years whose files are not staged are skipped; a failed check skips
that family-year and makes the script exit non-zero. Afterwards it rebuilds persons, views and the
lexicon, runs `ANALYZE`, and refreshes the read-only role.

Code layout: `build_common.py` (macros, header matching, loading), `build_precincts.py` (precinct
geography and protocol columns shared by per-precinct files), one `build_<family>.py` per family
with `TABLES`, `FAMILY`, `available(sources)` and `build_<family>(con, sources) -> list[Election]`.

### Sources

- **KBW mirror** (`data/kbw_mirror/dane/<year>/…`, from danewyborcze.kbw.gov.pl): registers, district
  tables and result files of every election above. Files of 2010–2014 have title lines above the
  header, "Lista nr N" rows above candidate columns, Excel `="…"` wrappers and, in 2010, unquoted
  fields containing `""`; the staging script handles each explicitly (row counts are still checked)
  and fails on anything else. Excel workbooks with several sheets are staged as
  one Parquet file per sheet (`<stem>__sheetNN_<hash>.parquet`; EP 2019 has one sheet per district).
- **PKW "dane w arkuszach" bundles** (`data/pkw_all/sejmsenat<year>/zip`): read first, per dataset.
  KBW's 2023 Senate per-precinct candidate file has empty vote columns; the PKW copy has the votes.

The staged Parquet is committed (`data/kbw_stage_parquet` about 285 MB, `data/pkw_stage_parquet` about 40 MB, `data/gus` 2 MB; CI builds the mart from it); the raw
mirror is not.

### Consistency checks

A family-year is not loaded if any check fails. The checks it passed are stored in
`mart.build_checks` (label and failing rows per family-year; `GET /checks/{election}/{year}`, the
„Zgodność z PKW” button on the election card). Among them:

- seats: 460 Sejm, 100 Senate, 552 sejmik councillors, EP seats per district; elected counts match;
- every precinct in a result file exists in the precinct register or protocol file;
- every result column maps to exactly one registered candidate or committee;
- precinct (or gmina) votes add up to the official candidate, list, district and territorial totals;
- president: precinct sums match the official per-gmina file (1990, 1995, 2000: also PKW's
  announced national figures); referendum: answers match territorial totals and yes + no = valid votes;
- president 2000: every Warszawa-Centrum precinct is matched to the 2001 precinct with the same number
  and address (`build_prezydent.CentrumMatchError` otherwise), so no row keeps code 143104;
- mayors: candidate votes add up to valid votes; with a single candidate, votes for + against do;
  no gmina has two elected mayors; round 2 only where round 1 took place;
- seats (`derived.check_seat_allocation`, after every load): official seats of every Sejm district
  (lists in seat allocation), sejmik district and contested proportional council district (councils
  with unknown list totals skipped) are recomputed with `mart.allocate_seats`: D'Hondt
  (`SEAT_METHODS`: modified Sainte-Laguë for Sejm 2001) among lists with 5% of the voivodeship's /
  council's valid votes (1998 and 2002: no threshold in gmina councils outside cities with powiat
  rights and in Warsaw district councils; 1998 also none in Rada m.st. Warszawy) and 2006 list blocs (`list_group`) with 15% (sejmik) / 10% (councils);
  inside a bloc Sainte-Laguë among its lists that passed 5%. Sejm 1993/1997: D'Hondt in districts
  among committees with 5% (coalitions 8%, minorities exempt) nationally, and for the 69 national-list
  seats (district 0, its own contender set) among committees with 7%; Sejm 1991: Hare-Niemeyer in
  districts without a threshold, list blocs (`sejm_district_lists.list_group`) first as one contender
  and then split by Hare-Niemeyer, and modified Sainte-Laguë for the national list (5% or seats in
  five linked districts, or a minority list). European Parliament: D'Hondt over the
  national votes of committees with 5% of the country's valid votes, then each committee's seats
  split between its district lists by largest remainders (`hare_niemeyer`). A difference is allowed only for an
  exact tie in list votes for the last seat, which the Electoral Code decides by data not loaded
  (precincts won); these districts are logged (seven so far).
- local elections 1998: the per-precinct files (sejmik and powiat per-district workbooks, gminas up to
  20,000 inhabitants) match the precinct file and the candidate register precinct by precinct: precinct
  protocols, list totals, every candidate's votes, mark and surname; candidates add up to their list in
  every precinct except the named struck-off lists; district sums equal the published valid votes;
  PKW's totals for gminas up to 20,000 inhabitants (votes cast, invalid, without an "x") and powiat
  councils (votes cast); the mazowieckie district summary (seats, lists, candidates, protocol, votes);
- 1991–1997 registers: every candidate has an age of at least 21 (the Sejm laws, and the Senate law
  of 10 May 1991 through art. 1; 30 only from the 1997 Constitution, in force on 17 October 1997).
  National-list seats went to the first candidates in list order without a district seat. 1997:
  every row of the registered national lists (OGPOL-01..10) matches a register candidate on list,
  position, district, surname and first given name, apart from
  `build_sejm.NATIONAL_LIST_EXCEPTIONS_1997` (1,042 candidates and 69 seats, as in M.P. 1997 nr 64
  poz. 620).

### Known gaps in the source data

These are properties of the official files, not build errors. Each check names its exception.

- **Councils 1994 and 1990.** KBW publishes no files of the 1990 local elections. 1994 (gmina councils
  only, 19 June 1994) keeps the codes of the 1975–1998 division: `rada_districts.teryt` is the 5-digit
  code (the first 5 of KBW's 7 digits, as `teryt_gmina_pre1999`; 33225 Silnowo only here, added to
  `gminy_pre1999.json`), `wojewodztwo` the old voivodeship, `teryt_powiat` NULL; person linking takes
  the powiat of the 1999 gmina from `mart.gmina_codes_pre1999`. Seven single-member districts are
  missing from the candidate file, each one district and seat short of its gmina's official totals
  (`build_rady.EMPTY_DISTRICTS[1994]`). Single-member candidates name only an optional supporting
  organisation (`Poparcie`, the committee; `(NIE PODANO)` without one; names are upper case as in other
  years); lists of one district with the same designation get ` (LISTA N)`. `valid_votes` is the votes for candidates: valid ballots without a
  choice are not included (they reconcile with KBW's valid ballots in every district). Seats in cities
  above 40,000 follow modified Sainte-Laguë without a threshold (art. 97 of the 1990 law) only with
  quotients cut to whole numbers (`allocate_seats` method `sainte_lague_modified_whole`; exact quotients
  differ in 17 of 843 districts); Gniezno district 3 is a tie (473 votes each), and in Nysa district 4
  the published seats contradict the published votes (`derived.SEAT_MISMATCHES`).
- **GUS census education 2011.** NSP 2011 education comes from the census sample survey and GUS
  rounds each unit's estimate separately: residents with higher education in the powiats of 13
  voivodeships differ from the voivodeship total by up to 3 persons (`build_gus.SAMPLE_ROUNDING`).
- **Sejm and Senate 1991–1997.** Sejm 1991: PKW announced 22,341 precinct commissions, KBW's files
  have 22,340 (all votes reconcile). Sejm and Senate 1993: KBW's files give 21,807 fewer eligible
  voters than PKW (district 21 Kraków −9,000, district 32 Piła −12,807), votes are equal, so turnout
  from the mart is 52.13% (official 52.08%; answers carry a caveat). Sejm 1991: national lists
  counted only linked district lists, and the linkage is not published; seats on unlinked lists are
  in `build_sejm.UNLINKED_SEAT_DISTRICTS_1991`. List blocs split their seats by unpublished
  declarations: the seat check compares the 1991 national blocs and the KPN blocs in districts 21,
  33, 34, 36 and 37 by bloc total only. Senate 1991: candidates with totals only, no precinct
  results. Senate 1993: skierniewickie (38) has candidate votes per precinct but no protocol
  columns, so its precinct protocols are NULL (its ballots exist per gmina only); zielonogórskie (49)
  is published twice and the register-only copy is checked against the results file. The 1991–2009
  files often give only the first given name; such candidacies join the person with the full name
  only when residence or birth year confirms it (see Persons). `v_seat_methods` covers the Sejm from 2001
  (single-tier allocation). PKW computed turnout of all six elections from votes cast.
  1997 national lists: OGPOL lists 1,045 candidates, the register 1,042. Żochowski (SLD 15) and
  Morawski (KPEiR 24) were struck off. Pokrzepa (SLD 115) is missing from the register, while
  `informacje_dodatkowe.txt` names Urbańczyk (SLD 66), who is in the register and won a district
  seat. OGPOL-01 prints position 35 twice (the register has Mikosz at 36). Three surnames are
  spelled differently. Both sources give PSL position 28 to Siwoń and Gwiżdż (no 29). Ages are as
  published (`Wiek`): some are the age on election day, others year minus birth year, so the birth
  year is year − age or one less. Senators under 30 were legal until 1997 (1993: Okrzesik and
  Borkowski, both 29). National registers from 2001 publish no age. Sejm and Senate 1989 are not
  loaded: KBW's archive starts in 1990.
- **GUS statistics.** No unemployment rate below powiat level; no unemployment figures for Warsaw's
  districts (only the city; BDL's zeros are dropped, `build_gus.NOT_PUBLISHED_FOR_DISTRICTS`); no
  district area or density in 2006–2007; Jastrzębie-Zdrój salary withheld in 2022–2023. Before 2002
  Warsaw is powiat 1431 with gminas 1431xx, while the 2000/2001 elections use the districts 1465xx.
  The town and rural parts of urban-rural gminas are not loaded.
- **Local elections 1998.** No mayors (elected by councils then). The KBW sejmik files exceed PKW's
  announcement (Dz.U. 1998 nr 131 poz. 861) by 1,608 eligible voters, 1,402 valid ballots, 1,382
  valid votes and 20 invalid ballots (14 of them without an "x"). Every KBW file gives the larger
  figures: the precinct file, the gmina file, the per-district workbooks (`1998woj`, precinct by
  precinct) and the mazowieckie summary (`1998mazowieckie`, district by district); the council files
  reproduce PKW's council figures exactly, so the cause is not in the published files. The check
  requires exactly this difference (`build_samorzad.SEJMIK_PKW_GAP_1998`); seats are unaffected.
  Ballots issued come from the workbooks (in 64 precincts they are fewer than the votes cast); turnout
  is valid ballots / eligible, PKW's basis. Seats per committee are not in PKW's announcement; the
  sejmik seats match the voivodeship results (AWS 342, SLD 329, …). In gminas up to 20,000
  inhabitants KBW marks 1,651 districts uncontested (PKW: 1,647) and has 4 more candidates and 27
  fewer lists (93,864 by its list ids; `rada_district_lists` has 93,851 rows because same-named
  committees are merged) than PKW. The per-precinct file has exactly KBW's 24,507 contested districts
  and their 121,075 candidates with the same votes, so the difference cannot be located
  (`build_rady.UNCONTESTED_GAP_1998`). Śniadowo district 12 had no list (1 seat added), nobody voted
  in Margonin district 14 (seat empty; 0 votes in the precinct file), Sochaczew district 5 lacks 77
  votes of a list missing from the file (a town over 20,000 inhabitants: no per-precinct file). The
  candidate file numbers Komitet Wyborczy "Samorządność" in Powiat Kartuski district 4 as list 4; the
  workbook and its other districts number it 7, which is loaded (`build_rady.LIST_NUMBER_ERRORS`). Four
  of the seven surnames "1" are PRAWDA in the per-precinct files, which is loaded
  (`build_rady.SURNAME_FIXES`); the other three stay "1" (`SURNAME_ERRORS`). A committee's
  lists in proportional councils share one number (art. 109, 141 of the 1998 act), so spelling
  variants with a shared number are one committee (`build_rady._uniform_committees_1998`, named
  exceptions `SEPARATE_LIST_NUMBERS` / `UNIFORM_LIST_NUMBERS`); variants above the highest shared
  number may remain separate. Warsaw: Rada m.st. Warszawy (KBW code 149900) is loaded as 146501, the
  seven Warszawa-Centrum district councils as their later districts (`build_rady.WARSAW_COUNCILS_1998`),
  the other Warsaw gmina councils keep their 1431xx codes; in the sejmik data Warszawa-Centrum is
  gmina 143104 (powiat 1431) and the other Warsaw gminas carry their later district codes.
- **Local elections 2002/2006.** No district register: seats are the elected candidates, checked
  against PKW's national counts. 2002 eligible voters in the KBW files are 29,572,213 for sejmiki
  (PKW: 29,572,225 in the announcement, 29,572,221 on its web pages) and for mayors round 1 (PKW web:
  29,572,209). 2002 council list totals outside powiat councils are recovered from the candidates'
  percentages (Warsaw district 7, list 18 set to PKW's 19,030, `build_rady.RESOLVED_LIST_TOTALS`).
  2006 Markuszów district 4: 1 seat unfilled, no valid vote cast. Warsaw 2002 already has the
  post-reform councils (Rada m.st. Warszawy and 18 district councils).
- **List blocs 2006** (`list_group` in `sejmik_district_lists` / `rada_district_lists`) are not in the
  KBW files; PKW published them only on its results pages. They are scraped by
  `scripts/scrape_list_groups_2006.py` into `data/pkw_html/list_groups_2006.csv` (source URL per row).
- **2000/2001 TERYT codes.** Files use the codes of that time: 37 gminas moved to powiats created in
  2002 (e.g. Wschowa 080411 → 081203, Lesko 180104 → 182103), Wałbrzych is 026301, ships
  229997/329997. Warsaw (powiat 1431 until 2002) is mapped to its districts 1465xx
  (`build_precincts.WARSAW_GMINAS_2001`; 2001 splits Warszawa-Centrum by a letter), except president
  2000, where the 451 Warszawa-Centrum precincts are assigned to districts through the 2001 precinct
  with the same number and address (one reviewed by hand, `build_prezydent.CENTRUM_2000_REVIEWED`).
  Wesoła was a gmina of powiat miński (141203).
- **Before 1999** (Sejm and Senate 1991/1993/1997, president 1990/1995, referendums 1996/1997): 49 voivodeships and no powiats; no
  official key maps their gmina codes to TERYT. Rows keep `teryt_gmina` / `powiat` NULL, the 5-digit
  code in `teryt_gmina_pre1999` and the historical voivodeship in `wojewodztwo`
  (`build_precincts.VOIVODESHIPS_1975`); 1990 uses KBW's gminas in the 1993 division. Result and
  turnout views place these precincts in today's gminas, powiats and voivodeships through
  `mart.gmina_codes_pre1999` (`derived.GEO_PRE1999`; Warsaw's gminas as their 1465xx districts, as in
  2000–2001) and also give level `wojewodztwo_1975` with the old voivodeship name in `wojewodztwo`
  (old names such as łódzkie or lubelskie covered other territory than today's). Gmina rows before
  1995 are approximate (see below) and get a caveat in answers. The 1990/1995 presidential and 1996 referendum
  files publish no ballots issued; turnout is valid ballots / eligible, PKW's basis. Referendum 1996
  had two ballot papers (question 1, questions 2–5) with their own ballots and turnout, in
  `referendum_precinct_answers.valid_ballots`; `v_turnout` has no 1996 row. Referendum 1997 omits the
  3 precincts annulled by the Supreme Court, as does PKW's corrected result; Żuromin precinct 8 has NO
  published as 13101, corrected to 11 (`build_referendum.CORRECTIONS_PRE1999`), which reproduces PKW's
  national total. 1990: two empty ship precincts are dropped and two hospital precincts did not vote
  in round 2 (named in `build_prezydent`).
- **Pre-1999 gmina codes.** `teryt_gmina_pre1999` is the code of the old GUS register: two digits for
  the 1975–1998 voivodeship (odd numbers, 01 warszawskie … 97 zielonogórskie) and three for the gmina
  (100–499 towns and urban-rural gminas, 500–999 rural gminas, 000–099 big cities and Warsaw
  dzielnice). No official key to TERYT exists. `streamlit_app/geo/gminy_pre1999.json` matches every
  code in the mart to its 1999 TERYT gmina by name and type, checked against GUS BDL units for
  1995–1998 and the GUS change notices; the UI maps use it, and `derived.load_gmina_codes_pre1999`
  loads it as `mart.gmina_codes_pre1999` (the build fails when a code in the result tables is missing
  from it, or the file gives it another old voivodeship or does not list that year). The 1991 Sejm file has no row for 24
  rural gminas (their precincts are under the town of the same name) and for 22 gminas created in
  1991–1993. Before a gmina was split off (1994–1998, and Jaśliska in 2010), the parent's code covers
  both. The Warsaw dzielnice of 1990–1994 cover several current districts. Ostrowice (split in 2019)
  cannot be mapped to a current gmina.
- **Sejm 2001** publishes no per-precinct candidate votes (no 2001 rows in
  `sejm_precinct_candidate_votes`); candidate totals are checked against list totals. Seats were
  allocated by modified Sainte-Laguë (`derived.SEAT_METHODS`).
- **Senate 2001.** 40 multi-member districts; `valid_votes` / `invalid_votes` count ballots. In the
  district 6 file, Łęczna precinct 6 is shifted two columns right (split address); the builder
  realigns it (`build_senat.SHIFTED_ROWS_2001`), after which the district reproduces the official
  totals.
- **President 2000** had one round; `advanced_to_round2` is false for every candidate. In 2000/2001
  precinct type Z means abroad (`build_precincts.PRECINCT_TYPES_2001`).
- **Referendum 2003.** Voting on 7–8 June (`election_date` 2003-06-08). The Supreme Court annulled
  precincts Ełk 14 and Wrocław 15; they are dropped (`build_referendum.ANNULLED_PRECINCTS`), so totals
  are PKW's corrected result of 21 July 2003 (25,163 precincts, yes 13,514,872), not the 9 June
  announcement (25,165, yes 13,516,612).
- **Turnout basis.** PKW computed the official turnout of Sejm/Senate 2001, 2005 and 2007,
  president 2000, 2005 and 2010, and sejmiki and mayors 2002 and 2006 from ballots issued, later from valid ballots taken from the ballot box.
  `v_turnout.turnout_pct` and `v_sejm_turnout.turnout_pct` follow PKW per election
  (`derived.TURNOUT_FROM_BALLOTS_ISSUED`, column `turnout_basis`) and match the official figures.
- **Sejm 2005 and 2007 candidate votes.** 2005 has no per-precinct candidate votes
  (`sejm_precinct_candidate_votes` has no 2005 rows); candidate totals are checked against the list
  totals. 2007 candidate votes come from separate district × list files; invalid votes are votes cast
  minus valid votes, because the column is not published.
- **Shared list numbers.** Sejm 2005 list 19 belonged to four committees in different districts, EP
  2004 list 20 to two; lists are matched per (district, list number).
- **Struck-off candidates 2004–2009** (gaps in ballot positions), named in the checks: Sejm 2005: 16
  lists; Sejm 2007: 5 lists; EP 2004: district 8 list 3; EP 2009: district 4 list 3, district 6 list 4,
  district 10 list 8.
- **Senate 2005/2007.** 40 multi-member districts electing 2–4 senators; voters picked several
  candidates, so `pct_of_district` is a share of valid ballots and does not add up to 100. Senate
  2005 results are published per powiat only: no 2005 rows in the Senate precinct tables and no
  2005 Senate turnout row; totals are checked against the official results (the files' 'Suma' row is
  ignored, 24 of 40 powiat files carry zeros there).
- **European Parliament 2004/2009.** Published per gmina only: `pe_precinct_*` hold one row per gmina
  with `precinct_no` 0, `pe_precinct_candidate_votes` is empty. EP 2004 publishes Warsaw as one unit
  (146501); abroad is 149901, ships 229901 and 329901. One 2004 register row has district 'P'; the
  builder takes district 7 from the commission seat and logs it. The 2009 PSL committee carries the
  alias "Koalicja Polska" because its name matches the 2019 rule.
- **Warsaw 2005.** Sejm and president round 1 identify Warsaw districts by letter
  (`build_precincts.WARSAW_DISTRICTS_2005`); president round 2 precincts are matched to round 1 by
  number.
- **`residence_teryt`** is published from 2019 only (Sejm/Senate 2019, 2023; EP 2019, 2024; president
  2020, 2025); earlier registers give residence as text and the column is NULL.

- **Unpublished precincts.** A few military precincts abroad have no published results: Sejm/Senate
  2023: 2, 2019: 6 (in 2019 they appear, empty, in all 100 Senate files); president 2020: 4,
  2025: 3. Precinct sums for the president therefore fall a few hundred votes short of the official
  national totals; `prezydent_candidates` holds the official totals, `v_prezydent_results` the sums.
- **Struck-off candidates.** Votes marked next to a candidate struck off after the ballots were printed
  count for the list only.
  - Sejm: some have result columns but no register entry (2019: two; 2015: about fifteen, with empty
    per-precinct columns, so 2015 list totals come from `2015-gl-lis-okr`).
  - European Parliament: struck-off candidates keep empty columns; they are logged and allowed.
  - Sejmik 2018: the register omits them; the three affected lists are named in
    `build_samorzad.STRUCK_OFF` (mazowieckie 7 Wolność w Samorządzie, świętokrzyskie 3 Partia Razem,
    śląskie 1 Partia Zieloni). Sejmik 2024: kept in the register with NULL votes (6 candidates).
    `sejmik_district_lists.votes` is the list total from the results, which can exceed the sum of its
    candidates.
- **Referendum 2023 by gmina.** The gmina file lists abroad results by country and omits ships and
  some abroad precincts, so the check compares domestic precincts only; the precinct file matches the
  official national result.
- **Sejmik 2024 by gmina.** The gmina file omits cities with powiat rights; they are taken from the
  powiat file, where such a city is its own powiat.
- **Combined military precincts, 2011.** The Sejm district 19 and Senate district 44 candidate
  files publish the military precincts abroad nos. 1–8, 29, 116 and 264 as one row ("w ujęciu
  łącznym"). Their candidate votes count towards the official candidate totals (checked) but belong
  to no precinct. These 11 precincts have Sejm protocols and list votes but no Senate protocol, so
  Senate 2011 eligible voters sum to 30,759,051.
- **Struck-off candidates, 2011/2014.** Sejm 2011: 10 candidate columns contain only "Skreślony" and
  have no register entry. EP 2014: two empty columns in district 9. Both are logged and allowed.
- **Namesakes with patronymics, 2011.** The registers tell namesakes apart by patronymic ("Andrzej
  Stefan syn Józefa SZTORC" / "syn Stefana", Sejm, Tarnów; "Mieczysław s. Stanisława BAGIŃSKI" /
  "s. Tomasza", Senate district 59); result headers carry only the bare name, so they are matched by
  list and ballot order. The names keep the patronymic, so these persons are not linked to other years.
- **EP 2014 has no list register.** Seats are the elected candidates; a committee took part in seat
  allocation if it won any seat. PKW's headline turnout 23.83% includes postal packets; the district
  tables and the mart give 23.82% (`ballots_issued` 7,295,481 without packets, PKW 7,301,650).
- **President 2010 valid ballots.** The 2010 files have no count of valid ballots, so `valid_ballots`
  holds votes cast ("Głosy oddane"); invalid votes are cast minus valid. Turnout uses ballots issued,
  as PKW did (54.94% / 55.31%).
- **TERYT changes before 2015.** Wałbrzych is 022109 in 2010/2011 and 026501 from 2013; gmina Zielona
  Góra (080910) exists until 2014 and was merged into the city in 2015. Ship precincts keep their
  codes (2011: 149801, 2010: 229901).
- **Councils, struck-off candidates.** 2024 registers keep them with NULL votes; list totals are
  exact (checked against the per-precinct protocols), except 2 lists in district 121905/2. The 2014
  and 2018 registers omit them, so in the districts named in `build_rady.STRUCK_OFF_DISTRICTS` the
  list cannot be identified and `rada_district_lists.votes` is NULL (2014: 29 districts, 211 lists;
  2018: 28 districts, 179 lists); `v_rada_results` uses candidate votes there with
  `votes_exact = false`.
- **Council list totals** are the sum of the candidates' votes (no district-level list results are
  published). This was checked once against the per-precinct council protocols: 2024 exact
  everywhere, 2018 differences only in the named districts; 2014 powiat councils cannot be checked.
- **Councils 2014: no district register.** A district's seats are its elected candidates; national
  councillor and district counts are checked against PKW (`build_rady.OFFICIAL_2014`).
  `council_name` is NULL in 2014. Four candidates have the surname "1" in the PKW file
  (`SURNAME_ERRORS`).
- **Councils 2018.** Two districts had no candidates (Turawa 6, Brzeżno 5): one seat each, nobody
  elected. District valid votes are recovered from the candidates' `% w okr.`.
- **Council committees** get aliases only when a sejmik committee with the same name ran that year;
  substring rules would mislabel thousands of local committees ("KWW RAZEM DLA …"). Nationwide
  parties running under another name at council level have no alias.
- **Sejmik 2014.** No district register: the 555 seats come from PKW. 13 lists are named in
  `build_samorzad.STRUCK_OFF[2014]`: seven with a struck-off candidate, six with small differences
  between the published files (mazowieckie district 4, śląskie district 2). PKW's widely quoted
  17.93% invalid votes and PiS 26.85% are preliminary night figures; the final data give 17.47% and
  PiS 26.89%.
- **Mayors 2014.** No vote in Zielona Góra (city and gmina merged on 1 January 2015); no mayor elected
  in Sosnówka (round 1) and Moskorzew (round 2).
- **Local elections 2010, no district registers.** Sejmik seats (561) and council seats are the
  elected candidates; councillors, districts and councils per council type are checked against PKW
  (`build_samorzad.SEJMIK_SEATS`, `build_rady.OFFICIAL_2010`). `council_name` is NULL in 2010. A vote
  marked only next to a struck-off candidate was invalid in 2010, so list totals equal the
  candidates' votes everywhere.
- **Councils 2010, multi-member majoritarian districts.** Gminas up to 20,000 inhabitants elected
  their councils in districts of 1–5 seats, and every voter had as many votes as seats.
  `rada_districts.valid_votes` there is the votes cast for all candidates (more than the number of
  voters). Valid ballots are recovered from the candidates' percentages and checked against the
  official gmina statistics.
- **Sejmik 2010, Łódź** is split between sejmik districts 1 and 2 (`build_samorzad.SEJMIK_SPLIT_GMINAS`);
  `sejmik_gmina_protocols` adds the two rows up.
- **Mayors 2010, run-offs on 19 December.** In ten gminas (`build_samorzad.LATE_RUNOFFS`) a run-off
  candidate withdrew and the run-off moved from 5 to 19 December (Ostróda: for/against vote). The
  withdrawn candidate has no round-2 row. Round-2 figures include these ten gminas (738 run-offs,
  turnout 35.30%; PKW's 728 and 35.31% cover 5 December only). `valid_ballots` is "głosy oddane" as
  published in the 2010 protocols.
- **Mayors 2024, withdrawn candidates.** A candidate who withdrew or was struck off after the ballots
  were printed keeps a row with NULL votes: two in Kraków (round 1) and one in each of the run-offs of
  Kuźnia Raciborska and Obrzycko, where the remaining candidate then faced a for/against vote.

### Adding an election year

1. Download and stage the files (step 1 above). Add the year to the family's `years` in
   `FAMILIES` (`scripts/build_mart.py`) and its date to the family builder.
2. Check the header layout. Column aliases live in `build_precincts.GEO_ALIASES` /
   `PROTOCOL_ALIASES` and in the builders; add aliases or a header parser for a new layout.
3. Add short names for new committees to `COMMITTEE_ALIASES` (`build_common.py`) and, if needed, the
   family to `PARTY_FAMILIES` (`derived.py`).
4. Run `build_mart.py --years <year> --elections <family>` until all checks pass, compare the
   national results with the official PKW page, then run `add_sql_example.py --check`.
5. List the gmina/powiat codes of the new year that are missing from `streamlit_app/geo/gminy.json` /
   `powiaty.json` and add each to `streamlit_app/geo/teryt_changes.json` with the act that changed it.

## Answering questions

```
question (+ previous turn) ─▶ router ─▶ dynamic_sql ─▶ rows? ─▶ answer text ─▶ /ask response
                                             └─ no rows, plain Sejm question ─▶ legacy SQL templates
```

**Routing** (`router.route_question`). Every question goes to dynamic SQL over the mart first. The
legacy Sejm templates run only when dynamic SQL returned no rows (no OpenAI key, mart unreachable,
failed or empty query) and the request is none of: an answer to a clarification (`entity_refs`), a
follow-up (`previous`), or a question naming an election only the mart covers (president,
referendum, sejmik, councils, wójt/burmistrz, European Parliament, Senate), because the templates
would answer it from Sejm data.

**Dynamic SQL** (`dynamic_sql.try_dynamic_query`):

1. **Entity resolution** (`mart/entities.py`). Question words are folded (lower case, no diacritics),
   common Polish endings are stripped (an adjective inside a one-word name and the "-rze" locative are
   put back in the nominative: "Białegostoku", "Jeleniej Górze"), and the result is matched against
   `entity_lexicon`: "w Krakowie" becomes `m. Kraków (teryt_gmina = '126101')`; places with more
   name words are kept first ("Jelenia Góra" before "Góra"). A person needs a question word that
   names the surname (`entities.surname_mentioned`): not the word that matched a given name, and,
   without a given name in the question, a capitalized word closer to the surname than to any
   committee alias ("w gminach" is not GMIŃSKI, "turę" not TUR, "PiS" not PIŚ, "Lewicy" not
   LEWICKI; "Kukiz" is both). Committee aliases of five or more letters may be inflected
   ("Konfederacji", "Platformy"); shorter ones (PO, KO, PiS) must be written exactly. Questions
   without Polish letters match the same way (everything is folded), and a long name word may
   carry one typo, a wrong or two swapped letters ("Platfromy", "Gdańksu"); a person found only
   through such a near match yields to a place or person the same word names more closely
   (KRASOWSKI is not "Krakowa", KAMYSZEK not "Kosiniak-Kamysz"). Before the per-kind cut, persons who
   ran in a year the question names come first; afterwards the election it names („prezydenckich
   2005”) narrows namesakes of that year. A lower-case surname alone ("tusk") is left to SQL on `full_name`.
   Persons whose given name appears in the question are kept first; then those elected more often.
   `narrow_matches` drops matches the question rules out: other given names; surnames that are
   matched places, except that a given name or a capitalized word inside the sentence beats a
   voivodeship or powiat, whose names are lower-case adjectives ("Mazowieckiego" is the person,
   "w województwie mazowieckim" the place); places named like the given name of a matched person;
   places matched only by a party alias ("PiS" is not Pisz); 1975–1998 voivodeships when a
   current place matched and the question names neither an earlier year nor "województwo"; places
   whose question words a place of the same kind matches more closely or with more words, or a
   person named with given name and surname ("Szczecin" is not Szczecinek, "Bielsko-Biała" not
   Biała, "Przemysław Czarnek" neither Przemyśl nor Czarne); persons without a candidacy in the
   years the question names when another matched person has one; and rural gminas named like a
   town gmina when the question says "miasto". Round words („turę”, „turze”) and „mieli” are stop
   words. `scripts/run_eval.py --check` lists evaluation questions whose entity resolution differs
   from the expected one.
2. **Clarification.** If several persons or same-name gminas remain, the API returns
   `intent = "clarification"` with options instead of guessing. The UI shows them as buttons; a
   choice is sent back as `entity_refs: [{"kind": "person", "ref": "<person_id>"}]`, which pins that
   entity. With an OpenAI key, one model call writes a query and a short Polish description for each
   of at most four options (`dynamic_sql.add_interpretations`); options carry `description` and the
   validated `sql`, and each query is put in the SQL cache under the request the choice sends, so a
   picked option runs without another model call (an option whose query fails the guard stays,
   without `sql`).
3. **Follow-ups.** With `previous` (`{question, sql, entity_refs}` of the preceding turn), entity
   kinds the new question does not name are inherited from the previous question, and the previous
   question and SQL are added to the prompt with the instruction to keep their filters unless the
   new question changes them. The UI sends the accumulated questions of the conversation as
   `previous.question`, so a chain of follow-ups keeps the original place or person. A follow-up
   that compares (`dynamic_sql.COMPARISON_RE`: „w porównaniu”, „względem”, „zmieniło”, „niż”, …)
   asks for both sides in one result instead of a replaced filter: one row per unit with a column
   per side and their difference. The UI turns the follow-up toggle on for short comparisons too.
4. **Prompt.** Schema comments (cached per mart load) of the relevant relations only, loaded
   elections, matched entities, up to `SQL_EXAMPLES_K` similar verified examples and the SQL rules.
   `dynamic_sql.FAMILY_KEYWORDS` maps words naming an election kind ("prezydenckie", "sejmik",
   "radnych", "europejskiego", ...) to relation prefixes; the prompt then describes those relations
   plus `SHARED_RELATIONS` (elections, persons, cross-election views). A question that names no
   kind gets every relation, base tables in compact form (table comment and column names, no column
   comments; `schema_description(compact=True)`), views in full: about 31k instead of 46k characters. `params._dynamic_sql_meta` reports `schema_relations` and
   `prompt_chars`. When adding a relation group, add its prefix to `FAMILY_KEYWORDS`.
5. **Validation** (`mart/safe_sql.py`). sqlglot parses the query. Only one SELECT (CTEs and UNION
   allowed) over mart relations is accepted. DML, DDL, `SET`, `SELECT INTO`, `pg_*` functions and
   functions such as `set_config`, `dblink` or `nextval` are rejected, also when quoted
   (`"pg_read_file"(...)`). `LIMIT` is capped at `MART_MAX_ROWS`. Expensive but harmless queries are
   bounded by the statement timeout and the row cap. `tests/test_mart.py` has a list of attacks and a
   seeded fuzzer that wraps safe queries in injection fragments and checks every accepted output
   independently (one SELECT, mart relations only, no system functions, capped LIMIT); a follow-up's
   previous SQL is only shown to the model, never executed. A question asking two or three separate
   things may get one SELECT per part (`{"parts": [{"title", "sql"}]}`, kept as one text of
   `-- part: <title>` blocks, `dynamic_sql.split_parts`); every part is validated and one rejected
   part rejects the whole answer.
6. **Execution** as `plujka_mart_reader` (`MART_DATABASE_URL`): read-only transactions, `mart` only,
   statement timeout `MART_STATEMENT_TIMEOUT_MS`. On a database error the model gets one repair
   attempt with the error message. Rows of a multi-part answer carry their part title in `part`.
7. **Cache.** A validated query that ran is cached per (folded question, entity choices, previous turn, mart load)
   for `SQL_CACHE_TTL_SECONDS`; a repeated question skips the LLM. A new build invalidates it.

Layers 5 and 6 are independent: a query that passes the parser still cannot write or read other
schemas.

**SQL examples** (`mart/examples.py`). Curated pairs live in `api/services/mart/sql_examples.jsonl`
(add one with `scripts/add_sql_example.py --question ... --sql ...`, which validates and runs it).
A thumbs-up on a `dynamic_sql` answer only queues the pair in the feedback log (`sql` on the
thumbs-up line); `scripts/add_sql_example.py --review` runs each queued pair, shows the rows and
appends it to the curated file when accepted (decisions in `example_reviews.jsonl` next to the log).
`scripts/triage_feedback.py` groups the thumbs-down answers by likely problem with the LLM
(`feedback_groups.md`) and asks the usual SQL prompt for a corrected query, with the rejected one and
the problem as the error to fix; proposals that pass the guard and return rows go to
`example_proposals.jsonl`, which `--review` offers next.
Curated pairs are indexed in the OpenSearch index `SQL_EXAMPLES_INDEX` (anything else is removed at
startup) and retrieved by kNN on the question embedding plus text match; without OpenSearch the
curated file is searched by word overlap.

**Answer text** (`api/services/answer.py`). The LLM may describe the rows in Polish, but every number
in its text must appear in the rows (rounding and Polish formatting allowed) or in the question.
Otherwise, or without an OpenAI key, the answer is a deterministic summary of the rows.
`answer.source` is `llm` or `fallback`; `rejected_numbers` lists numbers that failed the check.
`answer.caveats` holds fixed Polish notes about data gaps that apply to the rows and SQL
(`answer.CAVEATS`: `comparable = false`, `votes_exact = false`, EP 2014 turnout,
presidential precinct sums, person linking); the UI shows them under the answer. They contain no
numbers of their own. When you document a new known gap that changes how a result reads, add a rule.

## Observability

Every `/ask` writes one log line (`ask intent=… status=… rows=… latency_ms=… answer=… tokens=…/…`)
and one OpenSearch document in the question log with `status` (`template`, `first_pass`, `repaired`,
`cached`, `clarify`, `rejected`, `failed`, `skipped`), `latency_ms`, `row_count`, `answer_source`,
`prompt_tokens`, `completion_tokens` and `error`. `params._dynamic_sql_meta` in the response holds
the matched entities, examples used, `schema_relations`, `prompt_chars` and per-phase
`timings_ms`. Logging never fails a request. Dashboards over the question log: see
[DEPLOYMENT.md](DEPLOYMENT.md#telemetry-dashboards).

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `MART_DATABASE_URL` | `postgresql://plujka_mart_reader:plujka_mart_reader@localhost:5432/plujka` | Read-only role. `build_mart.py` creates it with this user and password (Compose: `MART_READER_PASSWORD`). |
| `MART_STATEMENT_TIMEOUT_MS` | `5000` | Server-side limit for one generated query |
| `MART_MAX_ROWS` | `500` | Row cap added to generated queries |
| `SQL_CACHE_TTL_SECONDS` / `SQL_CACHE_MAX_ENTRIES` | `3600` / `512` | Validated SQL cache (0 disables) |
| `SQL_EXAMPLES_INDEX` / `SQL_EXAMPLES_K` | `sql_examples` / `4` | Few-shot examples |
| `ANSWER_SUMMARY_ENABLED` | `1` | LLM answer text (numbers are always checked) |
| `ASK_RATE_LIMIT_PER_MINUTE` | `30` | `POST /ask` per API key or client IP (429 above); 0 disables |
| `REDIS_URL` | empty | Shares the rate limit and token budget across workers and replicas; empty: per process |
| `DAILY_TOKEN_LIMIT_PER_CLIENT` | `0` | LLM tokens per API key or client IP and UTC day for `/ask` (429 above); 0 disables |
| `API_KEYS` | empty | Comma-separated keys; when set, `/ask` and `/feedback` require `X-API-Key` |
| `PLUJKA_ENV` | `development` | `production` refuses to start with default passwords or no rate limit |

Production checklist: set `PLUJKA_ENV=production`, a strong `MART_READER_PASSWORD` and owner
password, and rebuild (the build sets the reader password). With several API workers set
`REDIS_URL`; for a non-public API set `API_KEYS`. Backups and dashboards: [DEPLOYMENT.md](DEPLOYMENT.md).
