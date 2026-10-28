
---

# 🤖 Cursor Agent Brief (ważniejsze niż myślisz)

# Agent Instructions

> Current, detailed rules for coding agents are in [`AGENTS.md`](AGENTS.md). This brief keeps the original principles.

You are building a deterministic analytics system over election data.

## DO

- Use PostgreSQL as the only source of truth
- Use SQL queries for ALL answers
- Use embeddings ONLY for:
  - intent classification
  - entity matching
- Keep logic simple and explicit
- LLM-written SQL is allowed only over the `mart` schema, and only through `safe_sql.validate_mart_sql`
  and the read-only role (`safe_sql.run_readonly`); see `docs/MART.md`

## DO NOT

- Do NOT generate answers using LLM knowledge
- Do NOT invent data
- Do NOT skip SQL layer
- Do NOT over-engineer

## Implementation Order

1. Database schema (PostgreSQL)
2. ETL script (CSV → DB)
3. Embedding generation
4. Semantic router (intents)
5. Candidate matching
6. SQL execution layer
7. Streamlit UI

## Coding Style

- Keep functions small and explicit
- Avoid abstractions early
- Prefer clarity over flexibility

## Output Requirements

Every answer must include:
- result data
- detected intent
- matched entity