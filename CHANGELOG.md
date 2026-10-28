# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) where releases are tagged.

## [Unreleased]

### Added

- Documentation: `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, `docs/DATA.md`, `SECURITY.md`, `CHANGELOG.md`, GitHub PR and bug-report templates; README “Documentation” index.
- Streamlit thumbs up/down; `POST /feedback` persists thumbs-down (`needs_fix`) with full ask payload to `data/feedback/feedback.jsonl` on the API host.
- Relational election model (`mart` schema): Sejm 2015/2019/2023 and Senate 2019/2023 with precinct-level list, candidate and protocol data, built by `scripts/build_mart.py` as the last loader step. Each year must reconcile with official PKW totals before it is loaded. Includes persons linked across elections, domain views (`v_sejm_national_results`, `v_sejm_list_results`, `v_sejm_turnout`, `v_person_history`) and an entity lexicon. See `docs/MART.md`.
- Entity resolution for questions (`api/services/mart/entities.py`): inflected Polish place, person and committee names are mapped to exact keys before SQL generation.
- `AGENTS.md` (rules for AI coding agents) and `docs/MART.md`.
- `POST /question-hints` (OpenSearch text + kNN) and Streamlit podpowiedzi przy wpisywaniu oraz „Inne z historii” pod wynikiem.

### Changed

- Dynamic SQL (`api/services/dynamic_sql.py`) now targets only `mart`: queries are validated with sqlglot and run as the read-only `plujka_mart_reader` role (`MART_DATABASE_URL`) with a statement timeout and row cap, with one repair attempt. It no longer queries `kbw_facts` or other public tables.
- `scripts/kbw_stage_duckdb.py` stages strictly (explicit delimiter, no `ignore_errors`, row-count check) and also stages XLS/XLSX; previously `okregi_sejm` lost 33 of 41 rows.
