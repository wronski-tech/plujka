# PKW AI – Architecture

## 🎯 Goal

Build a deterministic question-answering system over Polish election data (PKW), where:

- Users can ask questions in natural language (Polish)
- System returns answers ONLY based on database (no hallucinations)
- AI is used ONLY for interpretation, not knowledge generation

---

## 🧠 Core Principles

1. **SQL is the source of truth**
2. **LLM does NOT generate facts**
3. **All answers must come from DB queries**
4. **Embeddings are used for matching, not reasoning**
5. **System must be debuggable (show SQL + source)**

---

## 🧱 High-Level Architecture

User → Streamlit UI → Backend Logic → PostgreSQL

Flow:

1. User inputs question
2. Generate embedding
3. Detect intent (semantic router)
4. Resolve entities (candidate, district, etc.)
5. Execute SQL query
6. Return result + visualization

---

## 🗃️ Data Layer

Database: PostgreSQL  
Extension: pgvector

### Tables

#### candidates
- id (PK)
- name (TEXT)
- embedding (VECTOR)

#### elections
- id (PK)
- year (INT)
- type (TEXT)

#### results
- id (PK)
- candidate_id (FK)
- election_id (FK)
- votes (INT)
- district (TEXT)
- list_position (INT)

#### intents
- name (TEXT)
- embedding (VECTOR)

---

## 🔎 Semantic Router

Purpose: classify user question into known query types.

### Supported intents (MVP):
- count_votes
- trend

### Method:
- Embed user query
- Compare with intent embeddings (cosine similarity)
- Pick closest match

---

## 👤 Entity Resolution

Find candidate using embedding similarity:

```sql
SELECT id, name
FROM candidates
ORDER BY embedding <-> :query_embedding
LIMIT 1;

---

## 🔄 Data refresh (loader container)

KBW data is loaded **only** via the `loader` container (Compose `tools` profile): Parquet staging, then import into `kbw_facts` (not via the API).

Run the default pipeline:

```bash
docker compose --profile tools run --rm loader
```

Optional — only re-stage selected years to Parquet:

```bash
docker compose --profile tools run --rm loader python -u scripts/kbw_stage_duckdb.py --root /app/data/kbw_mirror/dane --out /app/data/kbw_stage_parquet --years 1997,2023
```

Optional — only import into Postgres (skip staging):

```bash
docker compose --profile tools run --rm loader python -u scripts/import_kbw_facts.py --root /app/data/kbw_mirror/dane --wait-db-seconds 600 --years 1997,2023
```