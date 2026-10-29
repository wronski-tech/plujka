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
| `sejm` | 2011, 2015, 2019, 2023 | precinct: lists, candidates, protocols |
| `senat` | 2011, 2015, 2019, 2023 | precinct: candidates, protocols |
| `prezydent` (both rounds) | 2010, 2015, 2020, 2025 | precinct: candidates, protocols |
| `pe` (European Parliament) | 2014, 2019, 2024 | precinct: lists, candidates, protocols |
| `referendum` | 2015, 2023 | precinct: answers per question |
| `sejmik` (voivodeship assembly) | 2014, 2018, 2024 | gmina: list votes, protocols; candidates with totals |
| `wbp` (wójt / burmistrz / prezydent miasta, both rounds) | 2014, 2018, 2024 | gmina: candidates, protocols |
| `rada` (councils: rada gminy / miasta, rada powiatu, rady dzielnic Warszawy) | 2014, 2018, 2024 | district: candidates with totals, lists, seats |

Councils are loaded at district level only (no per-precinct council results); list totals are the
sum of their candidates' votes (see [Known gaps](#known-gaps-in-the-source-data)).
`mart.elections` lists what a database actually contains, with dates; `GET /health?details=true`
shows it too.

The build output matches the official results. Examples (from the built mart):

| Check | Result |
| ----- | ------ |
| Sejm: PiS votes / seats | 2011: 4,295,016 / 157 · 2015: 5,711,687 / 235 · 2019: 8,051,935 / 235 · 2023: 7,640,854 / 194 |
| Sejm 2011 | PO 5,629,773 (39.18%, 207), RP 40, PSL 28, SLD 27, MN 1 |
| Sejm turnout | 2011: 48.92% · 2015: 50.92% · 2019: 61.74% · 2023: 74.38% |
| Senate seats | 2011: PO 63, PiS 31, PSL 2, independents 4 · 2015: PiS 61, PO 34 · 2019: PiS 48, KO 43 |
| President, round 2 | 2010: Komorowski 53.01% · 2015: Duda 51.55% · 2020: Duda 51.03% (turnout 68.18%) · 2025: Nawrocki 50.89% (turnout 71.63%) |
| European Parliament | 2014: PO 32.13% / 19, PiS 31.78% / 19, 51 seats · 2019: PiS 45.38% / 27 seats, KE 38.47% / 22 · 2024: KO 37.06% / 21, PiS 36.16% / 20 |
| Referendum | 2015: JOW "yes" 78.75%, turnout 7.80% · 2023: "no" 94.61–96.79%, turnout 40.91% |
| Sejmiki | 2018: PiS 34.13% / 254, KO 26.97% / 194, PSL 70 · 2024: PiS 34.27% / 239, KO 30.59% / 210, TD 80 (552 seats each) |
| Sejmiki 2014 | PO 179, PiS 171, PSL 157, SLD 28, MN 7 (555 seats); turnout 47.21% |
| Mayors | 2014: 2,477 gminas, 890 run-offs · 2018: 2,477 gminas, 649 run-offs · 2024: 2,477 gminas, 748 run-offs |
| Councils, seats | 2014: gmina 37,842 + cities 1,694, powiat 6,276, Warsaw districts 423 · 2018: ≤20k 32,175, >20k 7,351, powiat 6,244, Warsaw districts 425 · 2024: ≤20k 32,370, >20k 7,046, powiat 6,170, Warsaw districts 420 |

## Data model

Every table has `year` in its key; two-round elections also have `round`. Elections that publish
per-precinct files carry the precinct geography (TERYT, gmina, powiat, województwo) in their
protocol table. Sejm and Senate share `precincts`.

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

Committees carry `aliases` (`{PiS,Zjednoczona Prawica}`, `{KO,PO,Platforma Obywatelska}`), assigned
by `COMMITTEE_ALIASES` in `build_common.py` (first matching needle wins; needles are specific, e.g.
`PARTIA RAZEM`, not `RAZEM`, so local committees such as "ZAWSZE RAZEM" get no alias). Compare
parties across years by alias, not by name.

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
| `v_turnout` | turnout of **every** election per level (`election`, `year`, `round`) |
| `v_party_results` | party families across Sejm, EP and sejmik per level (`unit_key` joins elections) |
| `v_party_change` | change against the previous election of the same kind, with a `comparable` flag |
| `v_gmina_continuity` | which gmina TERYT codes appear in which elections |
| `v_person_history` | every candidacy of a person |

Cross-election rules:

- **Party families** (`PARTY_FAMILIES` in `derived.py`): PO and KO are `KO`; SLD, ZL, NL and Lewica
  are `Lewica`; PSL and TD are `TD/PSL`; KE (EP 2019 coalition) stays `KE`; committees without aliases
  are `other`. `lead_aliases` shows which committees formed the family in that election.
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
Parliament) with the same normalized full name and gender are one person. When a name is shared by
several candidates within one election event (Sejm+Senate of a year, a presidential round, an EP
election), residence must match too. A missing gender (2015 presidential files) is taken from other
candidacies with the same name when they agree. `persons.match_rule` records the rule.

Local candidates (sejmik, mayors, councils) never create persons. They get a `person_id` only when
exactly one national person has that name and gender and the name occurs once in that local
election (once among all council candidates of the year). Local registers also publish the age on
election day (`age`), national registers do not. All local candidacies linked to one person must
agree on the birth year (year − age, at most 1 apart); otherwise the name belongs to several people
and none of that person's local candidacies is linked. `v_person_history.age` shows the age.

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

Limits: national namesakes can still be merged (no age in national registers), and a person who
changed surname is linked only by an override.

Current build: 23,937 persons; 5,886 sejmik, 3,766 mayor and 17,934 council candidacies linked to
them. The birth-year check kept 2,165 local candidacies (740 names) unlinked that the name rule
alone would have linked.

## Building it

The `loader` service (`docker compose --profile tools run --rm loader`) does everything. By hand:

```bash
# 1. Stage sources to Parquet (strict: a file that would lose rows fails).
python3 scripts/kbw_stage_duckdb.py --root data/kbw_mirror/dane --out data/kbw_stage_parquet
for d in data/pkw_all/sejmsenat*/zip; do
  python3 scripts/kbw_stage_duckdb.py --root "$d" --out data/pkw_stage_parquet
done

# 2. Build the mart (about 3 minutes for everything).
DATABASE_URL=postgresql://plujka:plujka@localhost:5432/plujka python3 scripts/build_mart.py
python3 scripts/build_mart.py --years 2023                  # every family of one year
python3 scripts/build_mart.py --elections prezydent,pe      # some families, all years

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

The staged Parquet is committed (`data/kbw_stage_parquet` about 185 MB, `data/pkw_stage_parquet` about 40 MB; CI builds the mart from it); the raw
mirror is not.

### Consistency checks

A family-year is not loaded if any check fails. Among them:

- seats: 460 Sejm, 100 Senate, 552 sejmik councillors, EP seats per district; elected counts match;
- every precinct in a result file exists in the precinct register or protocol file;
- every result column maps to exactly one registered candidate or committee;
- precinct (or gmina) votes add up to the official candidate, list, district and territorial totals;
- president: precinct sums match the official per-gmina file; referendum: answers match territorial
  totals and yes + no = valid votes;
- mayors: candidate votes add up to valid votes; with a single candidate, votes for + against do;
  no gmina has two elected mayors; round 2 only where round 1 took place.

### Known gaps in the source data

These are properties of the official files, not build errors. Each check names its exception.

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
- **President 2010 turnout.** The 2010 files have no count of valid ballots, so `valid_ballots` holds
  votes cast ("Głosy oddane"): turnout 54.92% / 55.30% against PKW's 54.94% / 55.31%, which PKW
  computes from ballots issued. Invalid votes are cast minus valid.
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
   common Polish endings are stripped, and the result is matched against `entity_lexicon`: "w
   Krakowie" becomes `m. Kraków (teryt_gmina = '126101')`. Persons whose given name appears in the
   question are kept first; then those elected more often. `narrow_matches` drops matches the
   question rules out (other given names, surnames that are matched places, places named like the
   given name of a matched person).
2. **Clarification.** If several persons or same-name gminas remain, the API returns
   `intent = "clarification"` with options instead of guessing. The UI shows them as buttons; a
   choice is sent back as `entity_refs: [{"kind": "person", "ref": "<person_id>"}]`, which pins that
   entity.
3. **Follow-ups.** With `previous` (`{question, sql, entity_refs}` of the preceding turn), entity
   kinds the new question does not name are inherited from the previous question, and the previous
   question and SQL are added to the prompt with the instruction to keep their filters unless the
   new question changes them. The UI sends the accumulated questions of the conversation as
   `previous.question`, so a chain of follow-ups keeps the original place or person.
4. **Prompt.** Schema comments (cached per mart load) of the relevant relations only, loaded
   elections, matched entities, up to `SQL_EXAMPLES_K` similar verified examples and the SQL rules.
   `dynamic_sql.FAMILY_KEYWORDS` maps words naming an election kind ("prezydenckie", "sejmik",
   "radnych", "europejskiego", ...) to relation prefixes; the prompt then describes those relations
   plus `SHARED_RELATIONS` (elections, persons, cross-election views). A question that names no
   kind gets the whole schema. `params._dynamic_sql_meta` reports `schema_relations` and
   `prompt_chars`. When adding a relation group, add its prefix to `FAMILY_KEYWORDS`.
5. **Validation** (`mart/safe_sql.py`). sqlglot parses the query. Only one SELECT (CTEs and UNION
   allowed) over mart relations is accepted. DML, DDL, `SET`, `SELECT INTO`, `pg_*` functions and
   functions such as `set_config` or `dblink` are rejected. `LIMIT` is capped at `MART_MAX_ROWS`.
6. **Execution** as `plujka_mart_reader` (`MART_DATABASE_URL`): read-only transactions, `mart` only,
   statement timeout `MART_STATEMENT_TIMEOUT_MS`. On a database error the model gets one repair
   attempt with the error message.
7. **Cache.** A validated query that ran is cached per (folded question, entity choices, previous turn, mart load)
   for `SQL_CACHE_TTL_SECONDS`; a repeated question skips the LLM. A new build invalidates it.

Layers 5 and 6 are independent: a query that passes the parser still cannot write or read other
schemas.

**SQL examples** (`mart/examples.py`). Curated pairs live in `api/services/mart/sql_examples.jsonl`
(add one with `scripts/add_sql_example.py --question ... --sql ...`, which validates and runs it).
A thumbs-up on a `dynamic_sql` answer stores the pair as `user_confirmed`. Both are indexed in the
OpenSearch index `SQL_EXAMPLES_INDEX` and retrieved by kNN on the question embedding plus text
match; without OpenSearch the curated file is searched by word overlap.

**Answer text** (`api/services/answer.py`). The LLM may describe the rows in Polish, but every number
in its text must appear in the rows (rounding and Polish formatting allowed) or in the question.
Otherwise, or without an OpenAI key, the answer is a deterministic summary of the rows.
`answer.source` is `llm` or `fallback`; `rejected_numbers` lists numbers that failed the check.

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
| `REDIS_URL` | empty | Shares the rate limit across workers and replicas (fixed one-minute window); empty: per process |
| `API_KEYS` | empty | Comma-separated keys; when set, `/ask` and `/feedback` require `X-API-Key` |
| `PLUJKA_ENV` | `development` | `production` refuses to start with default passwords or no rate limit |

Production checklist: set `PLUJKA_ENV=production`, a strong `MART_READER_PASSWORD` and owner
password, and rebuild (the build sets the reader password). With several API workers set
`REDIS_URL`; for a non-public API set `API_KEYS`. Backups and dashboards: [DEPLOYMENT.md](DEPLOYMENT.md).
