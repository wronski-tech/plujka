# Relational election mart (`mart` schema)

The `mart` schema holds Sejm and Senate results as ordinary relational tables: one row per precinct,
candidate, list or district, with named columns instead of raw CSV headers in JSONB. Every value is
checked against the official PKW totals before it is loaded.

It exists so that questions do not need a hand-written SQL template each. An LLM can write SQL over a
small, documented schema, and guards keep that SQL read-only and inside `mart` (see
[Answering questions with SQL](#answering-questions-with-sql)).

## What is loaded

| Election | Sejm | Senate | Precinct detail |
| -------- | ---- | ------ | --------------- |
| 2023 | yes | yes | lists, candidates, protocols |
| 2019 | yes | yes | lists, candidates, protocols |
| 2015 | yes | no (Senate files not staged yet) | lists, candidates, protocols |

The build output matches the official PKW results exactly. Examples:

| Check | 2015 | 2019 | 2023 |
| ----- | ---- | ---- | ---- |
| PiS votes / seats | 5,711,687 / 235 | 8,051,935 / 235 | 7,640,854 / 194 |
| Turnout | 50.92% | 61.74% | 74.38% |
| Sejm seats / Senate seats | 460 / – | 460 / 100 | 460 / 100 |

## Data model

```
precincts ─┬─ sejm_precinct_protocols         (turnout, ballots per precinct)
           ├─ sejm_precinct_list_votes ───── sejm_committees ── sejm_district_lists ── sejm_districts
           ├─ sejm_precinct_candidate_votes ─ sejm_candidates ──┐
           ├─ senat_precinct_protocols                          ├── persons (same person across years)
           └─ senat_precinct_candidate_votes ─ senat_candidates ┘          └─ senat_districts
```

Every table has `year` in its key. A precinct is identified by `(year, precinct_id)`.

| Relation | One row per | Notes |
| -------- | ----------- | ----- |
| `precincts` | precinct | TERYT gmina, gmina/powiat/województwo names, Sejm and Senate district |
| `sejm_districts` / `senat_districts` | district | seat city, eligible voters (Sejm also seats and województwo) |
| `sejm_committees` | committee | `aliases` (`{PiS,Zjednoczona Prawica}`, `{KO,PO}`), coalition and minority flags (thresholds 8% / 5% / none) |
| `sejm_district_lists` | committee list in a district | official votes and seats |
| `sejm_candidates` / `senat_candidates` | candidate | official votes, `elected`, `person_id` |
| `*_precinct_protocols` | precinct | eligible voters, ballots issued, valid ballots, invalid votes |
| `sejm_precinct_list_votes` | precinct × list | |
| `*_precinct_candidate_votes` | precinct × candidate | |
| `persons` | person | links candidacies across years and chambers |
| `entity_lexicon` | name | gminy, powiaty, województwa, districts, persons, committees; used by entity resolution only |

Domain logic lives in views so that nobody (person or LLM) re-derives it:

| View | Content |
| ---- | ------- |
| `v_sejm_national_results` | national votes, percentage, `threshold_pct`, `passed_threshold`, seats per committee |
| `v_sejm_list_results` | list votes and share at `country`, `wojewodztwo`, `powiat`, `gmina` and `sejm_district` level |
| `v_sejm_turnout` | turnout at the same levels |
| `v_person_history` | every candidacy of a person, with committee and aliases |

The first three are materialized views and are refreshed by the build. Percentages are 0–100.

Every table, view and column has a PostgreSQL `COMMENT` in English. These comments are the schema
documentation: `api/services/dynamic_sql.py` reads them at runtime and sends them to the LLM. When you
add a column, write its comment in `api/services/mart/schema.py` (tables) or
`api/services/mart/derived.py` (views).

## Building it

The `loader` service (`docker compose --profile tools run --rm loader`) does everything. To run the
steps by hand:

```bash
# 1. Stage sources to Parquet (strict: a file that would lose rows fails).
python3 scripts/kbw_stage_duckdb.py --root data/kbw_mirror/dane --out data/kbw_stage_parquet
for d in data/pkw_all/sejmsenat*/zip; do
  python3 scripts/kbw_stage_duckdb.py --root "$d" --out data/pkw_stage_parquet
done

# 2. Build the mart (about 2 minutes for all years).
DATABASE_URL=postgresql://plujka:plujka@localhost:5432/plujka python3 scripts/build_mart.py
python3 scripts/build_mart.py --years 2023   # one year only
```

`build_mart.py` builds each year in DuckDB, runs the consistency checks, and loads the year into
PostgreSQL in one transaction (delete and insert). It then rebuilds persons, the views and the lexicon,
and refreshes the read-only role.

### Sources

- **KBW mirror** (`data/kbw_mirror/dane/<year>/…`, from danewyborcze.kbw.gov.pl): precinct register,
  candidate registers, district tables, 2015 Excel files.
- **PKW "dane w arkuszach" bundles** (`data/pkw_all/sejmsenat<year>/zip`): read first, per dataset.
  KBW's 2023 Senate per-precinct candidate file has empty vote columns; the PKW copy has the votes. The
  Sejm files are byte-identical in both sources.

### Consistency checks

A year is not loaded if any check fails. Among them:

- 41 Sejm districts, 460 seats and 460 elected candidates (100 for the Senate);
- every precinct in a result file exists in the precinct register;
- every result column maps to exactly one registered candidate;
- precinct list votes add up to the official district list totals;
- precinct candidate votes add up to the official candidate totals;
- no list has fewer votes than its candidates.

### Known gaps in the source data

These are properties of the official files, not build errors. The checks allow for them explicitly.

- **Unpublished precincts.** A few military precincts abroad have no published results (2 in 2023,
  6 in 2019). In 2019 they appear, empty, in all 100 Senate files.
- **Struck-off candidates.** Some candidates have result columns but no register entry (2019: two;
  2015: about fifteen). Their votes count in the official list totals. In 2015 their per-precinct
  columns are empty, so 2015 list totals come from the district summary file (`2015-gl-lis-okr`).

### Persons

PKW files carry no personal identifier. Candidacies with the same normalized full name and gender are
treated as one person. When a name is shared by several candidates in one election, residence must
match as well. `persons.match_rule` records which rule linked the candidacies.

Limits: namesakes in different years can be merged, and a person who changed surname is not linked.
For all three years this gives 16,625 persons, 2,652 of them with more than one candidacy.

### Adding an election year

1. Download the files and stage them (step 1 above). Add the year to `SUPPORTED_YEARS` in
   `scripts/build_mart.py`.
2. Check the header layout. `build_sejm.available_layout` picks the CSV layout (2019/2023) or the Excel
   layout (2015). For a new layout, add column aliases or a header parser in `build_common.py`.
3. Add short names for new committees to `COMMITTEE_ALIASES` (`build_common.py`); entries match committee names by pattern.
4. Run `build_mart.py --years <year>` until all checks pass, then compare the national results with the
   official PKW page.

## Answering questions with SQL

`api/services/dynamic_sql.try_dynamic_query` is the fallback path of `router.route_question`:

1. **Entity resolution** (`mart/entities.py`). Words of the question are folded (lower case, no
   diacritics), common Polish endings are stripped, and the result is matched against
   `entity_lexicon`. For example, "w Krakowie" becomes `m. Kraków (teryt_gmina = '126101')` and the
   Kraków Sejm districts 12 and 13. The model gets exact keys instead of guessing names.
2. **Prompt.** It contains the schema comments, the loaded years with their committees, the matched
   entities and the SQL rules.
3. **Validation** (`mart/safe_sql.py`). sqlglot parses the query. Only one SELECT (CTEs and UNION
   allowed) over mart relations is accepted. DML, DDL, `SET`, `SELECT INTO`, `pg_*` functions and
   functions such as `set_config` or `dblink` are rejected. `LIMIT` is capped at `MART_MAX_ROWS` (500).
4. **Execution** as `plujka_mart_reader` (`MART_DATABASE_URL`). This role can read only `mart`, every
   transaction is read-only, and it has a statement timeout (`MART_STATEMENT_TIMEOUT_MS`, 5 s). If the
   query fails, the model gets one repair attempt with the error message.

Layers 3 and 4 are independent. A query that passes the parser still cannot write data or read other
schemas. `params._dynamic_sql_meta` in the `/ask` response shows the status (`first_pass`, `repaired`,
`rejected`, `failed`, `skipped`) and the matched entities.

The dynamic SQL path covers only `mart`, so Sejm and Senate 2015–2023. Questions about presidential
elections, referendums or the raw `kbw_facts` table no longer go through it; they still use the SQL
templates.

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `MART_DATABASE_URL` | `postgresql://plujka_mart_reader:plujka_mart_reader@localhost:5432/plujka` | Read-only role. `build_mart.py` creates it with this user and password. |
| `MART_STATEMENT_TIMEOUT_MS` | `5000` | Server-side limit for one generated query |
| `MART_MAX_ROWS` | `500` | Row cap added to generated queries |

Change the reader password in production. It is set from `MART_DATABASE_URL` each time the build runs.
