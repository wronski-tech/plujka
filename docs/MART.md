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
| `sejm` | 2015, 2019, 2023 | precinct: lists, candidates, protocols |
| `senat` | 2015, 2019, 2023 | precinct: candidates, protocols |
| `prezydent` (both rounds) | 2015, 2020, 2025 | precinct: candidates, protocols |
| `pe` (European Parliament) | 2019, 2024 | precinct: lists, candidates, protocols |
| `referendum` | 2015, 2023 | precinct: answers per question |
| `sejmik` (voivodeship assembly) | 2018, 2024 | gmina: list votes, protocols; candidates with totals |
| `wbp` (wójt / burmistrz / prezydent miasta, both rounds) | 2018, 2024 | gmina: candidates, protocols |

Councils of gminas and powiats are not loaded (tens of thousands of districts, rarely asked about).
`mart.elections` lists what a database actually contains, with dates; `GET /health?details=true`
shows it too.

The build output matches the official results. Examples (from the built mart):

| Check | Result |
| ----- | ------ |
| Sejm: PiS votes / seats | 2015: 5,711,687 / 235 · 2019: 8,051,935 / 235 · 2023: 7,640,854 / 194 |
| Sejm turnout | 2015: 50.92% · 2019: 61.74% · 2023: 74.38% |
| Senate seats | 2015: PiS 61, PO 34 · 2019: PiS 48, KO 43 |
| President, round 2 | 2015: Duda 51.55% · 2020: Duda 51.03% (turnout 68.18%) · 2025: Nawrocki 50.89% (turnout 71.63%) |
| European Parliament | 2019: PiS 45.38% / 27 seats, KE 38.47% / 22 · 2024: KO 37.06% / 21, PiS 36.16% / 20 |
| Referendum | 2015: JOW "yes" 78.75%, turnout 7.80% · 2023: "no" 94.61–96.79%, turnout 40.91% |
| Sejmiki | 2018: PiS 34.13% / 254, KO 26.97% / 194, PSL 70 · 2024: PiS 34.27% / 239, KO 30.59% / 210, TD 80 (552 seats each) |
| Mayors | 2018: 2,477 gminas, 649 run-offs · 2024: 2,477 gminas, 748 run-offs |

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
| Mayors | `wbp_gmina_protocols` (office, per round), `wbp_candidates` (votes, `votes_against` for single candidates) |
| Shared | `elections`, `persons`, `entity_lexicon` |

Committees carry `aliases` (`{PiS,Zjednoczona Prawica}`, `{KO,PO,Platforma Obywatelska}`), assigned
by `COMMITTEE_ALIASES` in `build_common.py`. Compare parties across years by alias, not by name.

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

Local candidates (sejmik, mayors) never create persons. They get a `person_id` only when exactly one
national person has that name and gender and the name occurs once in that local election. Limits:
namesakes can still be merged, and a person who changed surname is not linked.

Current build: 17,587 persons; 3,829 sejmik and 2,030 mayor candidacies linked to them.

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
`prezydent`, `pe`, `referendum`, `samorzad` (sejmik+mayors). Each family-year is built in its own
in-memory DuckDB, checked, and loaded into PostgreSQL in one transaction (its tables and its
`mart.elections` rows). Family-years whose files are not staged are skipped; a failed check skips
that family-year and makes the script exit non-zero. Afterwards it rebuilds persons, views and the
lexicon, runs `ANALYZE`, and refreshes the read-only role.

Code layout: `build_common.py` (macros, header matching, loading), `build_precincts.py` (precinct
geography and protocol columns shared by per-precinct files), one `build_<family>.py` per family
with `TABLES`, `FAMILY`, `available(sources)` and `build_<family>(con, sources) -> list[Election]`.

### Sources

- **KBW mirror** (`data/kbw_mirror/dane/<year>/…`, from danewyborcze.kbw.gov.pl): registers, district
  tables and result files of every election above. Excel workbooks with several sheets are staged as
  one Parquet file per sheet (`<stem>__sheetNN_<hash>.parquet`; EP 2019 has one sheet per district).
- **PKW "dane w arkuszach" bundles** (`data/pkw_all/sejmsenat<year>/zip`): read first, per dataset.
  KBW's 2023 Senate per-precinct candidate file has empty vote columns; the PKW copy has the votes.

The staged Parquet of the KBW mirror is committed (`data/kbw_stage_parquet`, about 140 MB); the raw
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
question ─▶ router ─▶ (mart-only election named?) ─▶ dynamic_sql ─▶ answer text ─▶ /ask response
                 └──▶ legacy SQL templates (Sejm) ──▶ dynamic_sql when they return nothing
```

**Routing** (`router.route_question`). Questions naming an election only the mart covers
(president, referendum, sejmik, wójt/burmistrz, European Parliament, Senate) go to dynamic SQL
first; the legacy templates answer from Sejm data and would give a Sejm answer. Other questions try
the templates first and fall back to dynamic SQL when they return nothing. A request with
`entity_refs` (an answer to a clarification) goes straight to dynamic SQL.

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
3. **Prompt.** Schema comments (cached per mart load), loaded elections, matched entities, up to
   `SQL_EXAMPLES_K` similar verified examples and the SQL rules.
4. **Validation** (`mart/safe_sql.py`). sqlglot parses the query. Only one SELECT (CTEs and UNION
   allowed) over mart relations is accepted. DML, DDL, `SET`, `SELECT INTO`, `pg_*` functions and
   functions such as `set_config` or `dblink` are rejected. `LIMIT` is capped at `MART_MAX_ROWS`.
5. **Execution** as `plujka_mart_reader` (`MART_DATABASE_URL`): read-only transactions, `mart` only,
   statement timeout `MART_STATEMENT_TIMEOUT_MS`. On a database error the model gets one repair
   attempt with the error message.
6. **Cache.** A validated query that ran is cached per (folded question, entity choices, mart load)
   for `SQL_CACHE_TTL_SECONDS`; a repeated question skips the LLM. A new build invalidates it.

Layers 4 and 5 are independent: a query that passes the parser still cannot write or read other
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
the matched entities, examples used and per-phase `timings_ms`. Logging never fails a request.

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `MART_DATABASE_URL` | `postgresql://plujka_mart_reader:plujka_mart_reader@localhost:5432/plujka` | Read-only role. `build_mart.py` creates it with this user and password (Compose: `MART_READER_PASSWORD`). |
| `MART_STATEMENT_TIMEOUT_MS` | `5000` | Server-side limit for one generated query |
| `MART_MAX_ROWS` | `500` | Row cap added to generated queries |
| `SQL_CACHE_TTL_SECONDS` / `SQL_CACHE_MAX_ENTRIES` | `3600` / `512` | Validated SQL cache (0 disables) |
| `SQL_EXAMPLES_INDEX` / `SQL_EXAMPLES_K` | `sql_examples` / `4` | Few-shot examples |
| `ANSWER_SUMMARY_ENABLED` | `1` | LLM answer text (numbers are always checked) |
| `ASK_RATE_LIMIT_PER_MINUTE` | `30` | `POST /ask` per client IP and API process (429 above); 0 disables |
| `PLUJKA_ENV` | `development` | `production` refuses to start with default passwords or no rate limit |

Production checklist: set `PLUJKA_ENV=production`, a strong `MART_READER_PASSWORD` and owner
password, and rebuild (the build sets the reader password). The rate limit is per process; put a
shared limiter in front when running several API workers.
