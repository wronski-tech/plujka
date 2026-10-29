# Data

## What loads KBW data into PostgreSQL

The **API does not import** mirror files on startup. Use the **`loader` container** (Compose profile `tools`): it writes Parquet under `data/kbw_stage_parquet` and runs `scripts/import_kbw_facts.py` into `kbw_facts` / `kbw_election_runs`.

As its last step the loader runs `scripts/build_mart.py`, which builds the relational model in the PostgreSQL schema **`mart`**: Sejm and Senate 2001/2005/2007/2011/2015/2019/2023, president 2000/2005/2010/2015/2020/2025 (two rounds from 2005), European Parliament 2004/2009/2014/2019/2024, referendums 2003/2015/2023, and local elections 2002/2006/2010/2014/2018/2024 (voivodeship assemblies, wójt/burmistrz/prezydent miasta, and councils of gminas, powiats and Warsaw districts). The staged Parquet is committed so the mart can be rebuilt (and is, in CI) without the raw mirror; stage a subset with `scripts/kbw_stage_duckdb.py --prefix <path under --root>`. It reads the staged KBW mirror plus the staged PKW bundles (`data/pkw_stage_parquet`, preferred per dataset) and refuses to load an election whose votes do not add up to the official totals. Tables, sources, checks and known gaps in the official files are described in [MART.md](MART.md).

Staging (`scripts/kbw_stage_duckdb.py`) is strict: the delimiter is detected from the header, CP1250 files are transcoded, and a file fails instead of silently dropping malformed rows. XLS/XLSX tables are staged too; a workbook with several sheets becomes one Parquet file per sheet (`<stem>__sheetNN_<hash>.parquet`). ZIP member names in CP437/CP852 are decoded, and `__MACOSX/` entries are skipped.

The KBW files used by the mart (per election under `data/kbw_mirror/dane/<year>/`) come from danewyborcze.kbw.gov.pl. The raw mirror is not committed; the staged Parquet in `data/kbw_stage_parquet` is (about 285 MB), so the mart can be built without downloading.

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

## Data scraped from PKW pages

One dataset is not in any KBW file: the 2006 list blocs (*grupy list*), published only on PKW's
wybory2006.pkw.gov.pl results pages. `scripts/scrape_list_groups_2006.py` reads them (about 1,400
pages, ~50 minutes with its default delay) into `data/pkw_html/list_groups_2006.csv`, one row per list
with its source URL; the file is committed and read by the mart build (`build_rady.add_list_groups`).

## GUS statistics

`scripts/fetch_gus_bdl.py` downloads selected variables of the GUS Bank Danych Lokalnych
(https://bdl.stat.gov.pl/api/v1/) into `data/gus/` (committed). The API works without a key at 5
requests/s, 100 per 15 min, 1,000 per 12 h and 10,000 per 7 days; a free key (`BDL_API_KEY`, header
`X-ClientId`) raises this to 10 / 500 / 5,000 / 50,000. A full run is about 366 requests (about an
hour without a key); pages are cached under `.tmp/gus_bdl` so an interrupted run resumes. GUS
publishes BDL data under Creative Commons Attribution 4.0 (CC BY 4.0); cite "Źródło: GUS, Bank Danych
Lokalnych". Data fetched on 2026-10-29; 2025 values are not included yet.

## PKW data and attribution

Election datasets published by Państwowa Komisja Wyborcza (PKW) are subject to their terms of use. When redistributing derived datasets or citing aggregates, follow PKW’s rules and cite the official source. This repository does not grant rights beyond what PKW and applicable law allow.

## Git and large files

Full CSV trees can be large. Prefer Git LFS or keeping bulk data out of version control (download via scripts). `.gitignore` excludes common env files; add patterns if you store raw dumps locally.
