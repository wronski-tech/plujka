# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) where releases are tagged.

## [Unreleased]

### Added

- Year-specific committee alias rules (`YEAR_COMMITTEE_ALIASES`): the 2010 sejmik and council committee "KOALICYJNY KOMITET WYBORCZY LEWICA" is `KKW Lewica`, no longer counted with SLD in the Lewica family.
- Mart: Sejm and Senate 2005/2007, president 2005 (both rounds) and European Parliament 2004/2009 (gmina level), reconciled with PKW; `residence_teryt` for national candidates (published from 2019), used to link local candidacies to one of several national namesakes living in the same powiat; committee aliases for LiD, Samoobrona, LPR, SDPL, PD, UW, UPR, OKO, KROB and the 2010 German minority (TSKN); the `SKUTECZNI` needle narrowed to `SKUTECZNI PIOTRA`.
- Local persons: mayors, councillors and sejmik candidates without a national candidacy are linked across elections by name, gender, birth year and powiat (or voivodeship); runs containing one national person join that person.
- Answer caveats (`answer.caveats`): fixed notes about data gaps (non-comparable party changes, approximate council list totals, president 2010, Sejm 2005/2007, president 2005 and EP 2014 turnout, presidential precinct sums, person linking), shown under the answer.
- Daily OpenAI token budget per API key or IP (`DAILY_TOKEN_LIMIT_PER_CLIENT`, shared through Redis).
- Security tests: attack list and seeded fuzzer for the SQL guard; previous-turn SQL is never executed.
- CI `compose` job (build all images, start the stack, smoke-test `/health`, `/ask` and the UI); release workflow for `v*` tags with notes from CHANGELOG.
- Source freshness: `data/source_manifest.json`, `scripts/check_sources.py` (HEAD check, `--update`, `--rebuild`) and a weekly workflow.

- Mart: Sejm and Senate 2011, president 2010, European Parliament 2014, local elections 2010 and 2014 (sejmiki, mayors), and councils of gminas, powiats and Warsaw districts 2010/2014/2018/2024 (`rada_*` tables, `v_rada_results`), each reconciled with official PKW figures; known gaps documented in `docs/MART.md`.
- Persons: local candidacies linked to one person must agree on the birth year (from `age`, now also in `sejmik_candidates` and `v_person_history`); manual corrections in `api/services/mart/person_overrides.csv` (`match_rule = 'manual'`).
- Committee aliases for RN, TR, RAŚ, Zieloni, Wolność w Samorządzie, WiS, KNP, Ruch Palikota, PJN, Solidarna Polska, Polska Razem, Europa Plus.
- Follow-up questions: `/ask` accepts `previous` (`{question, sql, entity_refs}`); missing entity kinds are inherited and the previous SQL goes into the prompt. The UI keeps the conversation and shows a "Dopytaj o poprzedni wynik" toggle (on by default for questions such as "a w 2019?").
- UI: bar chart for results with one labelled unit per row, choropleth maps of gminas, powiats and voivodeships (`streamlit_app/geo/`, public-domain boundaries keyed by TERYT), and an SQL + source panel.
- Deployment: optional `API_KEYS` (`X-API-Key` on `/ask` and `/feedback`), rate limit shared through Redis (`REDIS_URL`, Compose profile `redis`, in-process fallback), `scripts/backup_db.sh`, OpenSearch Dashboards (Compose profile `observability`), `docs/DEPLOYMENT.md`.
- CI builds the whole mart from the committed parquet on PostgreSQL 16 and runs `scripts/add_sql_example.py --check`; `data/pkw_stage_parquet` is now committed.
- `scripts/kbw_stage_duckdb.py --prefix` stages only paths under the given prefixes; `MART_DUCKDB_MEMORY_LIMIT` caps DuckDB memory in builds.

- Mart: presidential elections 2015/2020/2025 (both rounds), European Parliament 2019/2024, referendums 2015/2023, Senate 2015, and local elections 2018/2024 (sejmiki and wójt/burmistrz/prezydent miasta), each reconciled with official totals; `mart.elections` lists what is loaded. `scripts/build_mart.py` builds election families (`--elections`, `--years`).
- Cross-election views: `v_turnout` (every election), `v_party_results` and `v_party_change` (party families with a `comparable` flag), `v_gmina_continuity` (TERYT presence); result views for president, EP, referendum and sejmik. Local candidates are linked to national persons when the name is unambiguous.
- Polish answer text for `/ask` (`answer`), with every number checked against the SQL rows; deterministic summary otherwise.
- Clarification for ambiguous people and same-name gminas (`intent = "clarification"`, options as buttons in the UI, `entity_refs` in `/ask`).
- Few-shot SQL examples: curated `sql_examples.jsonl` (`scripts/add_sql_example.py`, `--check`) and thumbs-up answers, retrieved from OpenSearch.
- Observability: per-request log line and question-log fields (status, latency, rows, answer source, tokens, error).
- Production readiness: `PLUJKA_ENV=production` startup check, configurable reader password (`MART_READER_PASSWORD`), `/ask` rate limit (`ASK_RATE_LIMIT_PER_MINUTE`), validated-SQL cache, extra indexes.

- Documentation: `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, `docs/DATA.md`, `SECURITY.md`, `CHANGELOG.md`, GitHub PR and bug-report templates; README “Documentation” index.
- Streamlit thumbs up/down; `POST /feedback` persists thumbs-down (`needs_fix`) with full ask payload to `data/feedback/feedback.jsonl` on the API host.
- Relational election model (`mart` schema): Sejm 2015/2019/2023 and Senate 2019/2023 with precinct-level list, candidate and protocol data, built by `scripts/build_mart.py` as the last loader step. Each year must reconcile with official PKW totals before it is loaded. Includes persons linked across elections, domain views (`v_sejm_national_results`, `v_sejm_list_results`, `v_sejm_turnout`, `v_person_history`) and an entity lexicon. See `docs/MART.md`.
- Entity resolution for questions (`api/services/mart/entities.py`): inflected Polish place, person and committee names are mapped to exact keys before SQL generation.
- `AGENTS.md` (rules for AI coding agents) and `docs/MART.md`.
- `POST /question-hints` (OpenSearch text + kNN) and Streamlit podpowiedzi przy wpisywaniu oraz „Inne z historii” pod wynikiem.

### Changed

- SQL guard also rejects quoted system functions (`"pg_read_file"(...)`) and sequence functions (`nextval`, `setval`, ...).

- `Razem` and `Bezpartyjni` aliases match only the party and the Bezpartyjni Samorządowcy movement (local committees such as "ZAWSZE RAZEM" no longer count as Razem in `v_party_results`).
- Every question goes to LLM SQL over the mart first; the legacy Sejm templates run only when it returns nothing for a plain Sejm question (not a clarification answer, follow-up or mart-only election).
- The SQL prompt describes only the relations of the election kinds the question names (plus elections, persons and cross-election views); about 7–17k instead of 27k characters of schema for such questions.
- `/ask` returns numeric values as JSON numbers (`34.27`, not `"34.27"`); questions are limited to 1000 characters.

- Questions about president, referendum, sejmik, mayors, European Parliament or Senate go to LLM SQL over the mart first instead of being matched to Sejm templates.
- Candidate `full_name` is always "Given names SURNAME" (registers that publish "SURNAME Given names" are reordered).

- Dynamic SQL (`api/services/dynamic_sql.py`) now targets only `mart`: queries are validated with sqlglot and run as the read-only `plujka_mart_reader` role (`MART_DATABASE_URL`) with a statement timeout and row cap, with one repair attempt. It no longer queries `kbw_facts` or other public tables.
- `scripts/kbw_stage_duckdb.py` stages strictly (explicit delimiter, no `ignore_errors`, row-count check) and also stages XLS/XLSX; previously `okregi_sejm` lost 33 of 41 rows.
