# Architecture

Plujka answers Polish election (PKW-style) questions with SQL against **PostgreSQL**: **LLM-written SQL over the relational `mart` schema** behind a parser guard and a read-only role ([MART.md](MART.md)), and **fixed SQL templates** for Sejm questions they cover. The answer text is either written by the LLM and checked number by number against the SQL rows, or a deterministic summary. Optional **OpenAI** writes SQL and answer text and improves intent extraction and embeddings; without it, the stack uses deterministic fallbacks.

## Request path (`POST /ask`)

```mermaid
flowchart LR
  Q[Question + optional previous turn] --> D[dynamic_sql: entities, clarification, relevant schema, examples, LLM SQL, cache]
  D --> G[sqlglot guard] --> RO[read-only role] --> M[(mart schema)]
  D -->|no rows, plain Sejm question| T[legacy SQL template + params]
  T --> DB[(PostgreSQL kbw_facts)]
  M --> A[answer text: numbers checked against rows]
  DB --> A
  A --> OS[OpenSearch log + telemetry]
```

1. **Access** — when `API_KEYS` is set, `POST /ask`, `POST /feedback` and `GET /persons*` require an `X-API-Key` header (401 otherwise). `POST /ask` is rate-limited per API key or client IP (`ASK_RATE_LIMIT_PER_MINUTE`, HTTP 429); with `REDIS_URL` the limit is shared by all workers.
2. **Dynamic SQL first** — `route_question` in `api/services/router.py` calls `dynamic_sql.try_dynamic_query` for every question. It resolves entities against `mart.entity_lexicon` (returning `intent = "clarification"` with options when several people or same-name gminas match), describes only the mart relations of the election kinds the question names, adds similar verified examples, asks the LLM for one SELECT over `mart`, validates it with sqlglot and runs it as `plujka_mart_reader` (one repair attempt on error; validated SQL is cached per mart load). Telemetry is in `params._dynamic_sql_meta`.
3. **Follow-ups** — a request may carry `previous: {question, sql, entity_refs}`. Entity kinds the new question does not name (person, place, committee) are inherited from the previous question, and the previous question and SQL go into the prompt, so "A w 2019?" after "Frekwencja w Krakowie w 2023?" asks about Kraków in 2019.
4. **Template fallback** — only when dynamic SQL returned no rows (no OpenAI key, failed or empty query), and the request is not a clarification answer, not a follow-up and does not name an election only the mart covers, the legacy path runs: `extract_intent_and_entity` and embeddings pick one of `SQL_TEMPLATES` (Sejm data in `kbw_facts`).
5. **Answer** — `answer.compose_answer` turns the rows into Polish text. LLM text is used only when every number in it appears in the rows or the question; otherwise the answer is a summary built from the rows. Numeric values in `result` are JSON numbers (PostgreSQL `numeric` 34.27 is sent as `34.27`, not `"34.27"`).
6. **Persistence and telemetry** — Results come from PostgreSQL only. Each request logs one line and one OpenSearch document with status, latency, row count, answer source and token usage (`api/services/opensearch_store.py`); OpenSearch Dashboards (Compose profile `observability`) charts them ([DEPLOYMENT.md](DEPLOYMENT.md)).

A clarification answer is sent back as `POST /ask` with `entity_refs: [{"kind": "person", "ref": "<person_id>"}]`; the Streamlit UI shows the options as buttons and does this for the user. The UI also keeps the last answered turn and sends it as `previous` when the "Dopytaj o poprzedni wynik" toggle is on (on by default for questions such as "a w 2019?"). It draws a bar chart when each row is one labelled unit with a number, a choropleth when rows carry `teryt_gmina`, `teryt_powiat` or `wojewodztwo` (boundaries in `streamlit_app/geo/`), and shows the SQL with the PKW/KBW source. Maps match `teryt_gmina`/`teryt_powiat` against `streamlit_app/geo/*.json` through `streamlit_app/map_units.py`: old codes are translated by `geo/teryt_changes.json` (votes of merged units are summed, percentages skipped with a caption), and features with `parts` are pre-division outlines (Warsaw, Kamienica, Supraśl, powiat wałbrzyski) drawn only when the result uses the old unit. See `streamlit_app/geo/README.md`.

The Streamlit UI also offers a person search ("Szukaj osoby") that calls `GET /persons?name=…` and, for the chosen match, `GET /persons/{id}`; it shows the same timeline as persons found in an answer. It is a Streamlit fragment, so its reruns do not touch the answer. Which result columns are charted is decided in `streamlit_app/chart_columns.py`: `numeric_columns` separates measures from codes (years, rounds, list numbers, ids, TERYT), and `scatter_columns` picks a scatter plot (label, x, y) when every row is a distinct territorial unit with exactly two measures; otherwise the bar chart and map use `_value_column`.

## Startup and data loading

On API startup (`lifespan` in `api/main.py`):

1. Production check — with `PLUJKA_ENV=production` the API refuses to start with the default database passwords or a disabled rate limit (otherwise it logs warnings).
2. `db.init_database()` — schema / migrations as implemented in `api/services/db.py`.
3. `opensearch_store.ensure_index()` — creates the `question_logs` index if missing; `examples.ensure_index()` creates the `sql_examples` index and indexes the curated examples (failures fall back to the curated file).

KBW rows are **not** imported by the API. Run the **`loader` service** (Compose profile `tools`): it stages mirror CSVs and the PKW bundles to Parquet, runs `scripts/import_kbw_facts.py` into `kbw_facts`, and finally `scripts/build_mart.py`, which builds the checked relational `mart` schema. That script **profiles the mirror tree into `kbw_dane_files`** first so `GET /kbw/catalog/summary` is meaningful after an import even when `kbw_facts` was empty before.

`GET /health` returns `data_ready: true` when `kbw_facts` has rows or the mart has loaded elections; `?details=true` lists the loaded elections (`mart_elections`). Streamlit polls `/health` so users see when the database has been populated.

`GET /kbw/catalog/summary` returns counts from `kbw_dane_files` (mirror inventory filled by `kbw_catalog.profile_kbw_dane_files`, typically from the loader). The Streamlit UI exposes catalog rollups and `GET /health?details=1` (`kbw_stats`) in collapsible `@st.fragment` sections that refresh on an interval so full-page reruns stay cheap.

## Question hints (`POST /question-hints`)

The Streamlit app calls this while the user types (≥ 2 characters) and after an answer to show **related past questions**. Responses combine **full-text** `match` on `question` and **kNN** on `question_embedding` (same 128-dim vectors as `log_question`). Short prefixes skip semantic search until `QUESTION_HINTS_SEMANTIC_MIN_CHARS` to limit embedding latency and OpenAI usage.

## Feedback (`POST /feedback`)

The Streamlit UI sends thumbs-up/down after each answer. A thumbs-up on a `dynamic_sql` answer also stores the question and its validated SQL as a few-shot example (`mart/examples.py`). **Thumbs-down** requests append one JSON line per event to `FEEDBACK_JSONL_PATH` (default `data/feedback/feedback.jsonl`) via `api/services/feedback_store.py`, including `needs_fix: true` and the full **`ask_response`** payload (question routing snapshot: `result`, `intent`, `sql`, `params`, etc.) for offline review. Thumbs-up lines omit `ask_response` and set `needs_fix: false`. The person card's **„Zgłoś błędne połączenie osoby”** form posts to `POST /feedback/person`, which appends a `kind: "person_link"` line (person id, the candidacies the user marked, a note) to the same file; fixes go to `person_overrides.csv`. In Docker, `./data` is mounted into the API container so logs land on the host under `data/feedback/`.

## Person timeline (`GET /persons/{person_id}`, `GET /persons?name=`)

`GET /persons/{person_id}` returns `{person, candidacies}`: the `mart.persons` row (with `match_rule`, how
candidacies were linked, plus `first_year`, `last_year`, `elected_count`) and every candidacy from
`mart.v_person_history` (election, year, round, district, area, committee, list position, votes,
elected, age where published) ordered by the election date in `mart.elections`; 404 for an unknown id
(`person_id` changes on every mart load). `GET /persons?name=…&limit=20` (max 50) is a prefix search
over persons in `mart.entity_lexicon` (national persons, elected mayors and sejmik councillors): every
word must start a word of the name and one must start the surname; case and diacritics are ignored.
Both run fixed, parameterized SQL as `plujka_mart_reader` through `safe_sql.run_readonly(sql, params=…)`
(read-only transaction, `MART_STATEMENT_TIMEOUT_MS`); a database error, e.g. while the mart is rebuilt,
returns 503. They require `X-API-Key` when `API_KEYS` is set but do not count against
`ASK_RATE_LIMIT_PER_MINUTE` or the token budget (no LLM). The Streamlit UI offers "Zobacz wszystkie
kandydatury" when an answer's rows carry `person_id` or its entities or clarification options are
persons, and shows the timeline with the linking rule and the namesake caveat. Every result table has
a "Pobierz CSV" button (`;` separator, decimal comma, UTF-8 with BOM for Excel in Polish locale).

## Configuration

Central defaults and env vars are in `api/services/config.py` (`DATABASE_URL`, `MART_DATABASE_URL`, `MART_STATEMENT_TIMEOUT_MS`, `MART_MAX_ROWS`, `SQL_CACHE_*`, `SQL_EXAMPLES_*`, `ANSWER_SUMMARY_ENABLED`, `ASK_RATE_LIMIT_PER_MINUTE`, `REDIS_URL`, `API_KEYS`, `PLUJKA_ENV`, `OPENSEARCH_URL`, OpenAI models, `EMBEDDING_DIM`, `FEEDBACK_JSONL_PATH`). See the table in [MART.md](MART.md#configuration). Docker Compose wires these for containers (see `docker-compose.yml`).

## Where to change behavior

| Concern              | Location                          |
| -------------------- | --------------------------------- |
| Intents / SQL shapes | `api/services/sql_templates.py`   |
| Routing rules        | `api/services/router.py`          |
| Rate limit / API keys| `api/services/rate_limit.py`, `require_api_key` in `api/main.py` |
| LLM SQL over `mart`  | `api/services/dynamic_sql.py`, `api/services/mart/safe_sql.py`, `api/services/mart/entities.py`, `api/services/mart/examples.py` |
| Answer text          | `api/services/answer.py`          |
| `mart` schema / build| `api/services/mart/`, `scripts/build_mart.py` |
| LLM prompts / parsing| `api/services/llm.py`             |
| Embeddings           | `api/services/embeddings.py`      |
| KBW import           | `api/services/kbw_import.py`, `scripts/import_kbw_facts.py`, `loader` service |
| Legacy PKW helpers   | `api/services/seed.py`            |
| UI (charts, map, follow-ups, person search) | `streamlit_app/app.py`, `streamlit_app/chart_columns.py`, `streamlit_app/map_units.py`, `streamlit_app/geo/` |
| Feedback JSONL       | `api/services/feedback_store.py`  |
| Person timeline / CSV | `/persons*` in `api/main.py`, `streamlit_app/app.py` |
