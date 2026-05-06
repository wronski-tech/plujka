# Data

## What loads KBW data into PostgreSQL

The **API does not import** mirror files on startup. Use the **`loader` container** (Compose profile `tools`): it writes Parquet under `data/kbw_stage_parquet` and runs `scripts/import_kbw_facts.py` into `kbw_facts` / `kbw_election_runs`.

For local iteration, `SEED_SAMPLE_CSV` in `api/services/config.py` still defaults to `data/sample/sejm_results_sample_1000.csv` for legacy helpers in `api/services/seed.py` (not used by the API on startup).

## Generate sample files locally

From the repo root:

```bash
python3 scripts/prepare_sample_data.py
```

Produces:

- `data/raw/wyniki_gl_na_listy_po_obwodach_sejm_utf8.csv`
- `data/sample/sejm_results_sample_1000.csv`

Use these for faster iteration without downloading full national dumps.

## Download official PKW CSV bundles

```bash
python3 scripts/download_all_pkw_csv.py
```

Writes archives to `data/pkw_all/zip/` and extracted CSVs to `data/pkw_all/csv/`.

Example — two election datasets:

```bash
python3 scripts/download_all_pkw_csv.py \
  --page-url "https://sejmsenat2023.pkw.gov.pl/sejmsenat2023/pl/dane_w_arkuszach" \
  --page-url "https://sejmsenat2019.pkw.gov.pl/sejmsenat2019/pl/dane_w_arkuszach"
```

Outputs are grouped by election prefix (e.g. `data/pkw_all/sejmsenat2023/`, `data/pkw_all/sejmsenat2019/`).

## PKW data and attribution

Election datasets published by Państwowa Komisja Wyborcza (PKW) are subject to their terms of use. When redistributing derived datasets or citing aggregates, follow PKW’s rules and cite the official source. This repository does not grant rights beyond what PKW and applicable law allow.

## Git and large files

Full CSV trees can be large. Prefer Git LFS or keeping bulk data out of version control (download via scripts). `.gitignore` excludes common env files; add patterns if you store raw dumps locally.
