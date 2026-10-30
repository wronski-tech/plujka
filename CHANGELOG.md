# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) where releases are tagged.

## [Unreleased]

### Added

- Source files per result: `GET /sources?election=&year=` lists the KBW files an election was built from (one chamber's files in the shared Sejm/Senate directory, plus the PKW bundle page for 2019/2023); the election card and the answer's SQL panel link them.
- UI: answers with one row per year (and optionally per party) draw a line chart over time; the person card has a „Zgłoś błędne połączenie osoby” form (`POST /feedback/person`, appended to `feedback.jsonl`).
- Exports: every table also downloads as XLSX and Parquet; `GET /packages/{election}/{year}` and an election-card button give a zip with turnout and results at every level and round (plain CSV) and a README with the column dictionary.
- Mart: Sejm and Senate 1991, 1993 and 1997 (precinct level; Senate 1991 candidates only), reconciled with PKW, with the national lists as district 0, list blocs, pre-1999 geography and seat checks (Hare-Niemeyer and modified Sainte-Laguë 1991, D'Hondt 1993/1997); party families for UD/KLD (UW/PD), POC/PC, NSZZ Solidarność (AWS), WAK/Ojczyzna.
- Mart: candidate ages (`age`) in `sejm_candidates`/`senat_candidates` for 1991–1997 (checked: every candidate 21+), shown in `v_person_history`; Sejm 1997 national-list positions checked against the registered national lists (OGPOL), with named exceptions.
- Mart: president 1990 and 1995 (both rounds) and referendums 1996 and 1997 at precinct level, reconciled with PKW; pre-1999 gminas keep their old code (`teryt_gmina_pre1999`) and voivodeship, views show them at country level. President 2000: Warszawa-Centrum precincts split into today's districts through the 2001 precinct addresses.
- Mart: local elections 1998 (sejmiki, gmina and powiat councils, Rada m.st. Warszawy and the Warszawa-Centrum districts), reconciled with PKW's announcement; seat check with the 1998 thresholds; party families AWS (with AWSP), Przymierze Społeczne (PSL family), RPO, Rodzina Polska. UI maps cover 1998 codes.
- Local elections 1998: use the KBW per-precinct files (sejmik and powiat per-district workbooks, gminas up to 20,000 inhabitants, mazowieckie summary) for precinct-by-precinct checks of protocols, list and candidate votes, district totals and PKW's council totals; load sejmik ballots issued; fix the Powiat Kartuski district 4 list number and four surnames "1" (PRAWDA in the per-precinct files); named-gap checks for the sejmik totals and the small-gmina district/candidate/list counts.
- GUS context statistics per gmina, powiat and voivodeship, 1995–2024 (population, urban/rural, area, density, registered unemployment, salary) as the `gus` mart family (`gus_units`, `gus_indicators`, `v_gus_units`), fetched from the GUS Local Data Bank by `scripts/fetch_gus_bdl.py`; answers that read them show the CC BY 4.0 attribution.
- Seat check for the European Parliament 2004–2024: D'Hondt nationally among committees with 5%, then largest remainders between a committee's district lists (`mart.allocate_seats(..., 'hare_niemeyer')`).
- Weekly source workflow opens a pull request with re-staged parquet and manifest when KBW republishes files, after building the whole mart with every check (draft when a check fails).
- Person timeline: `GET /persons/{person_id}` and `GET /persons?name=` (read-only mart role, fixed SQL); the UI shows every candidacy of a person from an answer with how candidacies were linked; "Pobierz CSV" under every result table (`;`, decimal comma, UTF-8 BOM).
- Maps: results from 1990–1997 by voivodeship draw the 49 voivodeships of 1975–1998 (unions of current gminas). Gmina results with `teryt_gmina_pre1999` colour current gminas through `geo/gminy_pre1999.json` (all 2527 old codes listed; 2526 mapped, Ostrowice left out). Gminas split off later are drawn inside their parent for the years before the split.
- Maps: 2001 voivodeship maps keep Sławków in małopolskie, and maps up to 2002 keep Szerzyny in podkarpackie. Dukla includes Jaśliska before 2010.
- Browsing without the LLM: API `/elections`, `/turnout`, `/results`, `/units`, `/units/{teryt}` (fixed read-only SQL) and UI panels "Przeglądaj wyniki" (turnout or party/candidate results per unit with table, chart, map and CSV), "Karta gminy / powiatu / województwa" (turnout over time, winners, GUS) and "Co jest w bazie".
- Sejm seat simulator (2001 on): `GET /seats/simulate` and a UI panel recompute seats per committee with another method (D'Hondt, Sainte-Laguë, modified Sainte-Laguë, Hare-Niemeyer), thresholds for committees and coalitions, minority exemption or one national district; the official settings reproduce every official result 2001–2023.
- Browse: `GET /change` and a "Zmiana" view show each party's change in percentage points against the previous Sejm, sejmik or EP election per unit; a ranking switch shows the 20 highest or lowest units; the unit card compares turnout with a second unit; the seat simulator shows votes per seat and the votes for committees without a seat.
- Seat simulator: merge committees into one coalition list (`merge=` committee ids; votes summed per district, coalition threshold).
- `GET /data-version`: when the mart was loaded, how many elections it holds, and the release's source manifest (file count, newest Last-Modified, SHA-256); shown under "Co jest w bazie".
- Mart: `gmina_codes_pre1999` maps every 1975–1998 gmina code in the result tables to its 1999 TERYT, today's voivodeship and powiat and the current gmina codes (from `geo/gminy_pre1999.json`, checked against the result tables on every build); 1990s questions get it in the prompt, with a curated example (president 1995 by today's voivodeship).
- Mart: 1990–1997 results and turnout (Sejm, Senate, president, referendum) are no longer national only: result views, `v_turnout`, `v_party_results` and `v_party_change` place them in today's gminas, powiats and voivodeships through `gmina_codes_pre1999` and add level `wojewodztwo_1975` for the 49 old voivodeships; answers by gmina or powiat for those years get a caveat; "Przeglądaj wyniki" offers these levels for 1990–1997.
- Candidates: `GET /candidates/stats` (per election year: candidates, elected, women's share of both, median age) and `GET /candidates/ages` (10 youngest and oldest candidates or elected), with a "Kandydaci: kobiety i wiek" panel; ages exist for local elections and Sejm/Senate 1991–1997.
- Mayors: `GET /mayors/terms` (regular elections won since 2002: histogram and longest-serving) and `GET /mayors/gmina/{teryt}` (mayor of each election and whether the same person won again), in the unit card and the candidates panel.
- Persons: local candidacies before 2010 that give only the first given name join the single full name with the same surname, first given name, gender and powiat and a birth year at most 1 apart (local persons 1,121,831 → 1,058,801; mayors with six terms 158 → 191). The same test on 2010+ files, where one given name is the full name, matches 1.4% of single-name candidacies, an upper bound for wrong merges.
- Rankings: `GET /rankings/persons` (top 50 by candidacies, wins, career length or votes; persons with a national candidacy, or one election) and `GET /rankings/party-switches` (moves between party families from one Sejm, Senate or EP candidacy to the next, with examples), in a "Rankingi osób i zmiany komitetów" panel.
- Elected: `GET /elected` lists the elected candidates of a Sejm, Senate, EP or sejmik election by district, optionally for one committee; "Wybrani według okręgu" panel with committee and district filters.
- Browse: election card (`GET /elections/{election}/{year}`: date, turnout, precincts or gminas, districts, seats, seat method and Sejm thresholds) above the results, and a "II tura" view (`GET /runoff`) with each presidential finalist's round 1 and round 2 result per unit and the net gain.
- Browse: the "II tura" view also covers mayoral run-offs (gmina level, map of each winner's gain), and a "Głosy zmarnowane" view (`GET /wasted`) shows Sejm votes for committees below the national threshold per voivodeship, powiat or gmina.
- Shareable links: the page URL keeps the "Przeglądaj wyniki" selection (`?wybory=…&rok=…&tura=…&pokaz=…&poziom=…&woj=…`) and opens it.
- Data dictionary: `GET /data-dictionary` (every queryable mart relation and column with its `COMMENT ON`) and a searchable "Słownik danych" panel with CSV export.
- Known gaps and sources: `GET /known-gaps` (the "Known gaps" section of `docs/MART.md`, shipped in the API image, and the answer caveats) and `GET /sources` (source manifest files, most recently republished first), in a "Znane luki w danych i pliki źródłowe" panel.
- Maps: a legend with colour swatches, a choice between a continuous scale and 5 quantile classes, and a colour-blind-safe viridis palette. Session history: the sidebar lists the questions of this session; a click shows that answer again without asking the API.
- UI: "Szukaj osoby" box (collapsed expander under the question) searches persons by name through `GET /persons` without asking a question; picking a match opens the same candidacy timeline with CSV download. Handles short input, no matches and a busy database (503); runs as a fragment with its own session keys (`person_search_*`), so it does not disturb the current answer.
- UI: results with one row per territorial unit and exactly two measures (e.g. `turnout_pct` and `unemployment_rate` per powiat) get a scatter plot instead of the bar chart (hover shows the unit). Column rule in `streamlit_app/chart_columns.py`; `list_position`, `list_group`, `rank` and `place` no longer count as chartable values.
- Smaller prompt for questions naming no election kind: base tables are described by their comment and column names only (46k → 31k characters).

- Mart: local elections 2002 and 2006 (sejmiki, mayors, councils), reconciled with PKW; 2006 list blocs (`list_group`, scraped from PKW's results pages into `data/pkw_html/list_groups_2006.csv`). The seat check allocates in two stages (lists and blocs, then inside a bloc) and covers 2002's thresholds.
- Mart: Sejm and Senate 2001, president 2000 and the 2003 EU accession referendum, reconciled with PKW (turnout from ballots issued for 2000/2001). Staging keeps a group row that names a single list (Excel). Committee aliases for AWS(P) and the 2001 Samoobrona spelling.
- UI maps: old TERYT codes are mapped to current units (`streamlit_app/geo/teryt_changes.json`: Wałbrzych 2004–2012, Rejowiec 2004–2005, gmina Zielona Góra 2004–2014); votes are summed where old units merged, percentages are skipped with a caption. Warsaw is drawn as 18 districts, or as one unit when a result only has 146501. Szczawa and Grabówka (2025) are added, with their parent gminas' pre-2025 outlines for older results. District and 2025 polygons © OpenStreetMap contributors (ODbL).
- Turnout as PKW computed it: `v_turnout` / `v_sejm_turnout` gain `ballots_issued` and `turnout_basis` (ballots issued for Sejm/Senate 2005 and 2007, president 2005 and 2010); the turnout caveats for those years are gone.
- Seat allocation: `mart.allocate_seats(votes, seats, method)` (D'Hondt, Sainte-Laguë, modified Sainte-Laguë), `v_seat_methods` (Sejm and sejmik seats per method), and a build check that recomputes every Sejm, sejmik and proportional council district (exact ties logged).
- Party lineages: `PARTY_FAMILIES` covers Nowoczesna, Solidarna Polska, Polska Razem, Wiosna, KORWiN/KNP/RN and UW/PD; queryable as `mart.party_families` with a note per alias.
- Persons: residence text of pre-2019 national candidates is mapped to a powiat through place names published with a TERYT since 2019, so more local candidacies reach the right national namesake.
- Year-specific committee alias rules (`YEAR_COMMITTEE_ALIASES`): the 2010 sejmik and council committee "KOALICYJNY KOMITET WYBORCZY LEWICA" is `KKW Lewica`, no longer counted with SLD in the Lewica family.
- Mart: Sejm and Senate 2005/2007, president 2005 (both rounds) and European Parliament 2004/2009 (gmina level), reconciled with PKW; `residence_teryt` for national candidates (published from 2019), used to link local candidacies to one of several national namesakes living in the same powiat; committee aliases for LiD, Samoobrona, LPR, SDPL, PD, UW, UPR, OKO, KROB and the 2010 German minority (TSKN); the `SKUTECZNI` needle narrowed to `SKUTECZNI PIOTRA`.
- Local persons: mayors, councillors and sejmik candidates without a national candidacy are linked across elections by name, gender, birth year and powiat (or voivodeship); runs containing one national person join that person.
- Answer caveats (`answer.caveats`): fixed notes about data gaps (non-comparable party changes, approximate council list totals, EP 2014 turnout, presidential precinct sums, person linking), shown under the answer.
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

- Production setup: Terraform for Hetzner (`infra/hetzner`: server with in-place rescale, data volume, firewall, Storage Box, cloud-init), `deploy/docker-compose.prod.yml` with Caddy (TLS, `/api` to FastAPI), GHCR images (`images.yml`), a deploy workflow (staging after every main build, production after approval) with `deploy/update_app.sh` and `deploy/smoke_test.sh`.
- Mart deployment without downtime: CI publishes `mart.dump` (release `mart-latest`); `deploy/deploy_mart.sh` restores it into the unused mart database and switches the API (`MART_DB`); `build_mart.py --derived-only`.
- CI: lint job (ruff, mypy with a baseline, pip-audit, offline Markdown links), mart dump cache keyed by data and builders, size report; Dependabot.
- Docs: `docs/RUNBOOK.md`, `NOTICE` (data sources and licences), `docs/ROADMAP.md`; superseded design notes moved to `docs/history/`.
- Dependencies: FastAPI 0.162 (Starlette 1.10), uvicorn 0.54, pydantic 2.14, pyarrow 26 (published advisories for Starlette 0.38 and pyarrow 18).
- `build_mart.py --jobs N` builds families in parallel processes (CI uses 3); `--skip-derived` loads family-years without rebuilding persons and views.
- Entity resolution: a capitalized or named surname beats a voivodeship or powiat adjective ("Mazowieckiego", "Tadeusz Mazowiecki" are the person; "w województwie mazowieckim" the place); a matched party alias is not a place ("PiS" is not Pisz); 1975–1998 voivodeships are dropped when a current place matched and the question names no earlier year or "województwo".
- Entity resolution: a person matches only through a word that names the surname (capitalized and not a party name when no given name is given, never the given-name word), so common words such as "w gminach", "turę", "zdobyły" or "PiS" no longer suggest persons (Gmiński, Tur, Zdobylak, Piś).
- Persons: national namesakes with one given name are split by voivodeship of residence (unless occupation, place or birth year link them) and by 1990s register birth years more than 3 apart; abbreviated 1991–2009 names ("Donald TUSK") join the person with the full name when residence or birth year confirms it (≈3,080 candidacies); local candidacies whose age contradicts a national candidacy (minimum age, 1990s ages) are no longer linked; overrides for Bartoszewski (father and son), Hofman, Kośla and Hatka. 62,535 → 60,681 national persons.
- Persons: the EP 2014 candidate and local candidate from Głowaczów is no longer merged with former president Aleksander Kwaśniewski (`person_overrides.csv`).
- `v_person_history` is a materialized view with indexes on `person_id` and `full_name` (trigram): person questions take milliseconds instead of 1.4 s.
- SQL guard also rejects quoted system functions (`"pg_read_file"(...)`) and sequence functions (`nextval`, `setval`, ...).

- `Razem` and `Bezpartyjni` aliases match only the party and the Bezpartyjni Samorządowcy movement (local committees such as "ZAWSZE RAZEM" no longer count as Razem in `v_party_results`).
- Every question goes to LLM SQL over the mart first; the legacy Sejm templates run only when it returns nothing for a plain Sejm question (not a clarification answer, follow-up or mart-only election).
- The SQL prompt describes only the relations of the election kinds the question names (plus elections, persons and cross-election views); about 7–17k instead of 27k characters of schema for such questions.
- `/ask` returns numeric values as JSON numbers (`34.27`, not `"34.27"`); questions are limited to 1000 characters.

- Questions about president, referendum, sejmik, mayors, European Parliament or Senate go to LLM SQL over the mart first instead of being matched to Sejm templates.
- Candidate `full_name` is always "Given names SURNAME" (registers that publish "SURNAME Given names" are reordered).

- Dynamic SQL (`api/services/dynamic_sql.py`) now targets only `mart`: queries are validated with sqlglot and run as the read-only `plujka_mart_reader` role (`MART_DATABASE_URL`) with a statement timeout and row cap, with one repair attempt. It no longer queries `kbw_facts` or other public tables.
- `scripts/kbw_stage_duckdb.py` stages strictly (explicit delimiter, no `ignore_errors`, row-count check) and also stages XLS/XLSX; previously `okregi_sejm` lost 33 of 41 rows.
