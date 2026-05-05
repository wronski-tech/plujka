# plujka

Deterministic PKW analytics stack:

Question -> embedding -> semantic router -> intent -> SQL template + params -> PostgreSQL

Additionally, questions are logged in OpenSearch for search/audit.

## 1) Prepare sample data

```bash
python3 scripts/prepare_sample_data.py
```

This creates:
- `data/raw/wyniki_gl_na_listy_po_obwodach_sejm_utf8.csv`
- `data/sample/sejm_results_sample_1000.csv`

## 1b) Download all PKW CSV archives from official page

```bash
python3 scripts/download_all_pkw_csv.py
```

This script discovers all CSV dataset stems from the official PKW page bundle and downloads available archives into:
- `data/pkw_all/zip/` (raw ZIP archives)
- `data/pkw_all/csv/` (extracted CSV files)

To pull both 2023 and 2019 datasets:

```bash
python3 scripts/download_all_pkw_csv.py \
  --page-url "https://sejmsenat2023.pkw.gov.pl/sejmsenat2023/pl/dane_w_arkuszach" \
  --page-url "https://sejmsenat2019.pkw.gov.pl/sejmsenat2019/pl/dane_w_arkuszach"
```

Output will be split by election prefix, for example:
- `data/pkw_all/sejmsenat2023/zip/`
- `data/pkw_all/sejmsenat2023/csv/`
- `data/pkw_all/sejmsenat2019/zip/`
- `data/pkw_all/sejmsenat2019/csv/`

## 2) Run full stack (Docker Compose)

```bash
docker compose up --build
```

Services:
- API: `http://localhost:8000`
- Streamlit: `http://localhost:8501`
- OpenSearch: `http://localhost:9200`
- PostgreSQL: `localhost:5432`

## Optional: OpenAI for intent/entity and embeddings

Set env vars before `docker compose up`:

```bash
export OPENAI_API_KEY=your_key
export OPENAI_CHAT_MODEL=gpt-4o-mini
export OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

Without API key, app uses deterministic local fallback routing/embeddings.