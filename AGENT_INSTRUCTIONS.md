
---

# 🤖 Cursor Agent Brief (ważniejsze niż myślisz)

# Agent Instructions

You are building a deterministic analytics system over election data.

## DO

- Use PostgreSQL as the only source of truth
- Use SQL queries for ALL answers
- Use embeddings ONLY for:
  - intent classification
  - entity matching
- Keep logic simple and explicit
- Prefer hardcoded SQL templates over dynamic generation

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