# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) where releases are tagged.

## [Unreleased]

### Added

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

- Questions about president, referendum, sejmik, mayors, European Parliament or Senate go to LLM SQL over the mart first instead of being matched to Sejm templates.
- Candidate `full_name` is always "Given names SURNAME" (registers that publish "SURNAME Given names" are reordered).

- Dynamic SQL (`api/services/dynamic_sql.py`) now targets only `mart`: queries are validated with sqlglot and run as the read-only `plujka_mart_reader` role (`MART_DATABASE_URL`) with a statement timeout and row cap, with one repair attempt. It no longer queries `kbw_facts` or other public tables.
- `scripts/kbw_stage_duckdb.py` stages strictly (explicit delimiter, no `ignore_errors`, row-count check) and also stages XLS/XLSX; previously `okregi_sejm` lost 33 of 41 rows.
