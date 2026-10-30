# AGENTS.md — guide for AI coding agents

Plujka answers questions in Polish about Polish elections (PKW/KBW data). Every fact in an answer
comes from a SQL query over PostgreSQL. The LLM may interpret the question and write SQL, but it must
never supply facts itself.

Human-oriented docs: `docs/ARCHITECTURE.md` (request flow), `docs/MART.md` (relational model, build,
SQL safety), `docs/DATA.md` (sources), `docs/DEPLOYMENT.md` (API keys, Redis limit, backups, dashboards). Read `docs/MART.md` before touching `api/services/mart/`.

## Repository map

| Path | What it is |
| ---- | ---------- |
| `api/main.py` | FastAPI app: `/ask`, `/health`, `/question-hints`, `/feedback`, `/kbw/*` |
| `api/services/router.py` | `route_question`: `dynamic_sql` first for every question; legacy templates only when it has no rows and the question is a plain Sejm one (no clarification answer, no follow-up, no mart-only election) |
| `api/services/sql_templates.py` | Legacy SQL templates over `kbw_facts` (JSONB rows) |
| `api/services/dynamic_sql.py` | LLM-written SQL over `mart`: entities/clarification, follow-ups (`previous`), relevant schema only (`FAMILY_KEYWORDS`), examples, sqlglot guard, read-only run, one repair, SQL cache |
| `api/services/answer.py` | Polish answer text; every number must appear in the SQL rows, else a deterministic summary; `CAVEATS` (fixed notes about data gaps) |
| `api/services/rate_limit.py` | Limit for `POST /ask`: in-process sliding window, or shared through Redis (`REDIS_URL`) |
| `api/services/mart/schema.py` | `mart` DDL with English `COMMENT`s, read-only role, list of queryable relations |
| `api/services/mart/build_common.py`, `build_precincts.py` | Shared DuckDB macros, header matching, loading; precinct geography/protocol columns |
| `api/services/mart/build_<family>.py` | DuckDB build per family and year (`sejm`+`senat`, `prezydent`, `pe`, `referendum`, `samorzad`) with consistency checks; `build_rady.py` builds councils inside `samorzad` |
| `api/services/mart/derived.py`, `person_overrides.csv` | Persons (name rules, birth-year check for local candidacies, manual overrides), result and cross-election views, party families (`party_families`), turnout basis, seat views and the seat-allocation check (`mart.allocate_seats`), entity lexicon (rebuilt after every load) |
| `api/services/mart/examples.py`, `sql_examples.jsonl` | Verified question → SQL few-shot examples (curated file, OpenSearch; thumbs-up answers join after `add_sql_example.py --review`) |
| `api/services/mart/safe_sql.py` | `validate_mart_sql` (sqlglot) and `run_readonly` |
| `api/services/mart/entities.py` | Question → lexicon matches (inflected Polish names → TERYT / person_id / committee), narrowing, ambiguity |
| `scripts/kbw_stage_duckdb.py` | Strict CSV/XLS/XLSX → Parquet staging |
| `scripts/build_mart.py` | Builds `mart`: every family-year that is staged (`--years`, `--elections`) |
| `scripts/add_sql_example.py` | Adds a curated SQL example after running it; `--check` runs them all; `--review` accepts queued thumbs-up answers |
| `scripts/run_eval.py`, `tests/eval/questions.jsonl` | Evaluation set (question → reference SQL or expected clarification); `--check` runs the reference queries, otherwise grades the model per family (nightly `eval.yml`) |
| `scripts/backup_db.sh` | `pg_dump` backup with retention |
| `scripts/check_sources.py`, `data/source_manifest.json` | Detects republished KBW files (HEAD vs manifest), re-stages them, names the family-years to rebuild |
| `scripts/scrape_list_groups_2006.py`, `data/pkw_html/` | 2006 list blocs scraped from PKW's results pages (not in KBW files), read by `build_rady.add_list_groups` |
| `streamlit_app/app.py`, `streamlit_app/map_units.py`, `streamlit_app/geo/` | UI: answer, table, bar chart, choropleth (TERYT-keyed boundaries; old codes via `geo/teryt_changes.json`), SQL + source, follow-up toggle |
| `tests/` | `unittest`. DB tests run only with `PLUJKA_RUN_DB_TESTS=1` |

## Rules

- **Language.** Code comments, docstrings, log messages, exceptions and `COMMENT ON` texts are in English.
  User-facing UI text and LLM prompts that show examples of questions may be in Polish.
- **Facts come from SQL only.** Do not add code paths where the LLM answers from its own knowledge.
- **LLM-written SQL goes through both guards.** Always call `safe_sql.validate_mart_sql`, then
  `safe_sql.run_readonly`. Never execute generated SQL on `DATABASE_URL`, which is the read-write owner.
- **Schema comments are the LLM's documentation.** A new mart column or view needs a clear English
  `COMMENT`, because `dynamic_sql.schema_description()` sends these comments to the model. Add new
  queryable relations to `BASE_TABLES` or `DERIVED_RELATIONS` in `schema.py`, otherwise the guard
  rejects them.
- **Domain logic belongs in views.** Thresholds, percentages and turnout per unit are defined once in
  `derived.py`. Do not repeat them in prompts or templates.
- **Never weaken a consistency check to make a build pass.** If official totals do not reconcile, find
  the cause in the source files. When the gap is real (for example unpublished precincts or struck-off
  candidates), name it explicitly in the check and document it in `docs/MART.md` under "Known gaps".
- **Answer text is checked.** `answer.compose_answer` accepts LLM text only when every number in it
  appears in the SQL rows or the question. Do not bypass it or let the LLM compute new numbers.
- **Dynamic SQL answers first; templates are a fallback.** Do not add new SQL templates; add views,
  schema comments or curated examples instead. Questions naming an election the templates do not
  know (president, referendum, sejmik, councils, mayors, EP, Senate) must never fall back to them
  (`router._MART_ONLY_ELECTION_RE`).
- **The prompt describes only relevant relations.** A new relation group needs its prefix and Polish
  keywords in `dynamic_sql.FAMILY_KEYWORDS` (cross-election relations go to `SHARED_RELATIONS`),
  otherwise questions naming that election never see its tables.
- **Known gaps that change how a result reads get a caveat** in `answer.CAVEATS` (rule on rows/SQL,
  Polish text without numbers of its own).
- **New downloaded sources go into the manifest:** run `scripts/check_sources.py --record` after
  adding files to the mirror, and commit `data/source_manifest.json`.
- **API responses carry plain JSON numbers.** Rows pass through `main._json_value`; the UI charts and
  maps rely on numbers, not strings.
- **Curated SQL examples must run.** After changing views or rebuilding, run
  `scripts/add_sql_example.py --check`; fix or remove examples that fail.
- **Staging must not lose rows.** No `ignore_errors`, no guessed delimiters. Layout fallbacks (title lines,
  list rows, `="…"` wrappers, unquoted 2010 files) are explicit and each has a test in `TestStrictStaging`.
- **Person corrections go to `person_overrides.csv`,** never into SQL special cases; every row needs a
  `note`, and a row that matches nothing fails the build.
- **Committee alias needles must be specific.** `COMMITTEE_ALIASES` matches by substring, first rule
  wins (`YEAR_COMMITTEE_ALIASES` for one year come first, for committees sharing a later committee's
  name); check every loaded committee's aliases before and after a change (local committees are named
  freely, e.g. "ZAWSZE RAZEM").
- **Prefer PKW bundles over the KBW mirror per dataset.** KBW's 2023 Senate precinct file has empty votes.
- PostgreSQL in CI is 16 and local images may be 15: avoid `any_value` and other 16-only functions in
  PostgreSQL SQL. DuckDB SQL in `build_*.py` may use DuckDB features.
- Keep changes small and explicit. Follow existing patterns before adding new helpers.

## Commands

```bash
make test                         # unit tests, no DB
make test-integration             # + DB tests (needs DATABASE_URL with pgvector, PLUJKA_RUN_DB_TESTS=1)
make compile                      # byte-compile api, scripts, streamlit_app, tests

python3 scripts/kbw_stage_duckdb.py --root data/kbw_mirror/dane --out data/kbw_stage_parquet
for d in data/pkw_all/sejmsenat*/zip; do python3 scripts/kbw_stage_duckdb.py --root "$d" --out data/pkw_stage_parquet; done
DATABASE_URL=postgresql://plujka:plujka@localhost:5432/plujka python3 scripts/build_mart.py [--years 2023] [--elections prezydent,pe]
MART_DATABASE_URL=postgresql://plujka_mart_reader:plujka_mart_reader@localhost:5432/plujka python3 scripts/add_sql_example.py --check

docker compose up -d                                  # postgres, opensearch, api, streamlit
docker compose --profile tools run --rm loader        # stage + import + build mart
```

Dependencies: `api/requirements.txt` (API, includes `sqlglot`), `loader/requirements.txt` (DuckDB,
pyarrow, polars; needed by the mart build and its tests).

## Checklists

**Changing the mart schema.** Edit the DDL and its comments in `schema.py`. Update the family's
`build_<family>.py` (and its `TABLES`), and `derived.py` if views use the column. Rebuild with
`build_mart.py`; every check must pass. Update `docs/MART.md`. `CREATE TABLE IF NOT EXISTS` does not
change existing databases: add a new column to an existing table with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
next to its DDL.

**Adding an election year.** Follow "Adding an election year" in `docs/MART.md`. Compare the national
results with the official PKW page before you finish.

**Adding an election family.** Write `build_<family>.py` with `TABLES`, `FAMILY`, `available()` and a
build function returning `Election` rows; add the tables to `schema.py` (a `*_TABLES` group in
`BASE_TABLES`), the family to `FAMILIES` in `scripts/build_mart.py`, result views and lexicon or
person sources to `derived.py`, its keywords to `router._MART_ONLY_ELECTION_RE` and
`dynamic_sql.FAMILY_KEYWORDS`, and a few curated examples to `sql_examples.jsonl`. CI builds the whole
mart from the committed parquet, so commit the staged parquet of the new family too.

**Changing dynamic SQL or its guards.** Extend `tests/test_mart.py`: accepted and rejected queries,
entity matches including false positives, clarification, follow-ups, prompt relation selection
(`TestPromptRelations`), and the `try_dynamic_query` flow (`TestDynamicQueryFlow` stubs the LLM and
the database). Run `tests/test_integration_mart_db.py` against a real
PostgreSQL to confirm the role still denies writes, reads outside `mart`, and long queries.

**Testing without an OpenAI key.** Stub `dynamic_sql._llm_sql` and call `try_dynamic_query` against a
built mart. The validation, execution and repair paths need no key.
