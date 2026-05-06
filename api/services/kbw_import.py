"""Import KBW mirror tabular files into kbw_election_runs + kbw_facts.

Families are inferred from paths under data/kbw_mirror/dane. Supported inputs:
- CSV
- ZIP (CSV members)
- XLSX (first worksheet)
- XLS (via xlrd, first worksheet)"""

from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from api.services import db

try:
    import xlrd  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dependency at runtime
    xlrd = None

ENCodings_TRIED: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1250")
_SHEET_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class InferredRun:
    family: str
    year: int
    round_num: int
    slice: str
    variant: str
    dataset_hint: str | None


_YEAR = re.compile(r"^(19|20)\d{2}$")
_KW = re.compile(r"kw[_\s]?(\d+)", re.IGNORECASE)


def _segments_after_dane(rel: Path) -> list[str]:
    """Segments after dane."""
    parts = rel.parts
    try:
        idx = parts.index("dane")
    except ValueError:
        return list(parts)
    return list(parts[idx + 1 :])


def _pick_year(segments: list[str], filename: str) -> int | None:
    """Pick year."""
    m = re.search(r"(19\d{2}|20\d{2})", filename)
    if m:
        return int(m.group(1))
    for seg in segments:
        if _YEAR.match(seg):
            return int(seg)
    return None


def _pick_round_prezydent(segments: list[str]) -> int:
    """Pick round prezydent."""
    for i, seg in enumerate(segments):
        if seg == "prezydent" and i + 1 < len(segments) and segments[i + 1].isdigit():
            return int(segments[i + 1])
    return 0


def _rw_slice_variant(filename: str) -> tuple[str, str]:
    """Rw slice variant."""
    m = _KW.search(filename)
    slice_s = f"kw_{m.group(1)}" if m else ""
    lower = filename.lower()
    if "_woj_" in lower or lower.startswith("rejestr_wyborcow_woj"):
        variant = "woj"
    elif "_del_" in lower or "wyborcow_del" in lower:
        variant = "del"
    else:
        variant = "national"
    return slice_s, variant


def _family_from_segments(segments: list[str]) -> str:
    """Family from segments."""
    if not segments:
        return "other"
    if segments[0] == "RW":
        return "rw"
    for seg in segments:
        low = seg.lower()
        if low.startswith("samo") or low.startswith("samorzad"):
            return "samorzad"
    topic_priority = (
        "prezydent",
        "referendum",
        "parlament_eu",
        "samorzad",
        "sejmsenat",
        "sejm",
        "senat",
        "sejmik",
        "rada_powiatu",
        "rada_gminy",
        "powiaty",
        "wbp",
    )
    for topic in topic_priority:
        if topic in segments:
            return topic
    return "other"


def infer_election_run(rel_path: Path) -> InferredRun | None:
    """Map a mirror-relative path to an election run bucket."""
    fn = rel_path.name
    segs = _segments_after_dane(rel_path)
    year = _pick_year(segs, fn)
    if year is None:
        return None

    family = _family_from_segments(segs)
    round_num = 0
    slice_s = ""
    variant = ""
    hint = "/".join(segs[:-1]) if len(segs) >= 1 else None

    if family == "rw":
        slice_s, variant = _rw_slice_variant(fn)
    elif family == "prezydent":
        round_num = _pick_round_prezydent(segs)

    return InferredRun(
        family=family,
        year=year,
        round_num=round_num,
        slice=slice_s,
        variant=variant,
        dataset_hint=hint,
    )


def _guess_delim(line: str) -> str:
    """Guess delim."""
    line = line.rstrip("\r\n")
    if not line:
        return ";"
    tabs, semi, comma = line.count("\t"), line.count(";"), line.count(",")
    best = max((tabs, "\t"), (semi, ";"), (comma, ","), key=lambda x: x[0])
    return best[1] if best[0] > 0 else ";"


def _normalize_cell(raw: str) -> str:
    """Normalize cell."""
    s = (raw or "").strip().strip('"')
    if len(s) >= 4 and s.startswith('="') and s.endswith('"'):
        s = s[2:-1]
    return s


def _parse_number(raw: str) -> float | None:
    """Parse number."""
    s = _normalize_cell(raw).replace(" ", "").replace("\xa0", "")
    if not s or s in ("-", ".", ","):
        return None
    # Strip Excel text markers
    if s.startswith("="):
        s = s.lstrip("=").strip('"')
    s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v:  # NaN
        return None
    return v


_GEO_SUBSTRINGS = (
    "teryt",
    "kod ",
    "kod_",
    "gmina",
    "powiat",
    "wojew",
    "delegatura",
    "okręg",
    "okreg",
    "obwód",
    "obwod",
    "symbol",
    "numer",
    "nr komisji",
    "typ gminy",
    "l. obwodów",
    "l.obwodów",
    "siedziba",
    "komisji",
)


def _is_geo_column(name: str) -> bool:
    """Is geo column."""
    n = name.strip().lower()
    if not n:
        return True
    return any(tok in n for tok in _GEO_SUBSTRINGS)


def _is_pct_column(name: str) -> bool:
    """Is pct column."""
    n = name.strip().lower()
    return "proc" in n or "%" in n or "procent" in n or "proc." in n


def _is_prez_ballot_stat_column(col: str) -> bool:
    """Is prez ballot stat column."""
    n = col.lower()
    keys = (
        "frekw",
        "uprawnien",
        "karta",
        "kart ",
        "głos",
        "glos",
        "ważn",
        "wazn",
        "wa¿ne",
        "oddane",
        "wydane",
        "obwod",
        "l. obwod",
        "liczba",
        "mieszka",
        "komisji",
        "urn",
    )
    return any(k in n for k in keys)


def _subject_for_column(family: str, col: str) -> dict[str, Any]:
    """Subject for column."""
    if family == "prezydent":
        if _is_prez_ballot_stat_column(col):
            return {"kind": "ballot_stat", "column": col}
        return {"kind": "candidate", "label": col}
    if family == "rw":
        return {"kind": "registry_stat", "column": col}
    if family == "referendum":
        return {"kind": "referendum_stat", "column": col}
    return {"kind": "series", "column": col}


def _metric_slug(col: str) -> str:
    """Metric slug."""
    s = re.sub(r"[^\w]+", "_", col.strip().lower())
    return s[:180] if s else "value"


def ensure_election_run(cur: Any, inferred: InferredRun) -> int:
    """Ensure election run."""
    cur.execute(
        """
        INSERT INTO kbw_election_runs (family, year, round, slice, variant, dataset_hint)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (family, year, round, slice, variant) DO UPDATE SET
            dataset_hint = COALESCE(kbw_election_runs.dataset_hint, EXCLUDED.dataset_hint)
        RETURNING id
        """,
        (
            inferred.family,
            inferred.year,
            inferred.round_num,
            inferred.slice,
            inferred.variant,
            inferred.dataset_hint,
        ),
    )
    row = cur.fetchone()
    assert row is not None
    return row[0]


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    """Return header, rows as dicts, encoding used."""
    best: tuple[list[str], list[dict[str, str]], str] | None = None
    last_err = ""
    for encoding in ENCodings_TRIED:
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                sample = f.read(65536)
            if not sample.strip():
                return [], [], encoding
            first_line = sample.splitlines()[0] if sample else ""
            delim = _guess_delim(first_line)
            f = path.open("r", encoding=encoding, newline="")
            reader = csv.reader(f, delimiter=delim)
            header_raw = next(reader, [])
            header = [_normalize_cell(h).replace("\ufeff", "") for h in header_raw]
            rows: list[dict[str, str]] = []
            for raw in reader:
                row = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))}
                rows.append(row)
            f.close()
            # Prefer encoding that yields mostly valid Polish / ASCII headers for prez files
            if any(h for h in header):
                return header, rows, encoding
            best = (header, rows, encoding)
        except UnicodeDecodeError as e:
            last_err = str(e)
            continue
        except OSError as e:
            last_err = str(e)
            break
    if best:
        return best[0], best[1], best[2]
    raise ValueError(last_err or "could not read CSV")


def _decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode bytes."""
    last_err = ""
    for encoding in ENCodings_TRIED:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as e:
            last_err = str(e)
    raise ValueError(last_err or "could not decode bytes")


def _read_csv_rows_from_bytes(data: bytes) -> tuple[list[str], list[dict[str, str]], str]:
    """Read csv rows from bytes."""
    text, encoding = _decode_bytes(data)
    sample = text[:65536]
    if not sample.strip():
        return [], [], encoding
    first_line = sample.splitlines()[0] if sample else ""
    delim = _guess_delim(first_line)
    reader = csv.reader(StringIO(text), delimiter=delim)
    header_raw = next(reader, [])
    header = [_normalize_cell(h).replace("\ufeff", "") for h in header_raw]
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))}
        rows.append(row)
    return header, rows, encoding


def _xlsx_col_to_idx(cell_ref: str) -> int:
    """Xlsx col to idx."""
    col = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Xlsx shared strings."""
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for si in root.findall("s:si", _SHEET_NS):
        parts: list[str] = []
        t = si.find("s:t", _SHEET_NS)
        if t is not None and t.text is not None:
            parts.append(t.text)
        for run_t in si.findall(".//s:r/s:t", _SHEET_NS):
            if run_t.text:
                parts.append(run_t.text)
        values.append("".join(parts))
    return values


def _read_xlsx_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read xlsx rows."""
    with zipfile.ZipFile(path) as zf:
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in zf.namelist():
            sheets = [n for n in zf.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")]
            if not sheets:
                return [], []
            sheet_name = sorted(sheets)[0]

        shared = _xlsx_shared_strings(zf)
        root = ET.fromstring(zf.read(sheet_name))
        sheet_data = root.find("s:sheetData", _SHEET_NS)
        if sheet_data is None:
            return [], []

        table: list[list[str]] = []
        for row_node in sheet_data.findall("s:row", _SHEET_NS):
            row_values: dict[int, str] = {}
            max_idx = -1
            for c in row_node.findall("s:c", _SHEET_NS):
                ref = c.attrib.get("r", "")
                col_idx = _xlsx_col_to_idx(ref) if ref else (max_idx + 1)
                max_idx = max(max_idx, col_idx)
                c_type = c.attrib.get("t", "")
                text_val = ""
                if c_type == "inlineStr":
                    inline_t = c.find("s:is/s:t", _SHEET_NS)
                    text_val = inline_t.text or "" if inline_t is not None else ""
                else:
                    v = c.find("s:v", _SHEET_NS)
                    raw = v.text if v is not None and v.text is not None else ""
                    if c_type == "s":
                        try:
                            text_val = shared[int(raw)]
                        except (ValueError, IndexError):
                            text_val = raw
                    else:
                        text_val = raw
                row_values[col_idx] = text_val
            if max_idx < 0:
                continue
            dense = [""] * (max_idx + 1)
            for i, v in row_values.items():
                dense[i] = v
            table.append(dense)

    if not table:
        return [], []

    header = [_normalize_cell(v).replace("\ufeff", "") for v in table[0]]
    rows: list[dict[str, str]] = []
    for raw in table[1:]:
        row = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))}
        rows.append(row)
    return header, rows


def _read_xls_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read xls rows."""
    if xlrd is None:
        raise RuntimeError("xlrd is required to import .xls files (pip install xlrd)")

    book = xlrd.open_workbook(path.as_posix(), on_demand=True)
    if book.nsheets == 0:
        return [], []
    sheet = book.sheet_by_index(0)
    if sheet.nrows <= 0:
        return [], []

    header = [_normalize_cell(str(v)).replace("\ufeff", "") for v in sheet.row_values(0)]
    rows: list[dict[str, str]] = []
    for i in range(1, sheet.nrows):
        values = [str(v) for v in sheet.row_values(i)]
        row = {header[j]: (values[j] if j < len(values) else "") for j in range(len(header))}
        rows.append(row)
    return header, rows


def _import_rows(
    *,
    header: list[str],
    rows: list[dict[str, str]],
    source_file_id: int | None,
    inferred: InferredRun,
    clear_existing: bool,
) -> int:
    """Import rows."""
    if not header:
        return 0
    value_cols = [h for h in header if h and not _is_geo_column(h)]
    if not value_cols:
        return 0

    geo_cols = [h for h in header if h and _is_geo_column(h)]
    batch: list[tuple[Any, ...]] = []
    fact_rows = 0

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            if source_file_id is not None and clear_existing:
                cur.execute("DELETE FROM kbw_facts WHERE source_file_id = %s", (source_file_id,))
            run_id = ensure_election_run(cur, inferred)

            for row in rows:
                geo: dict[str, Any] = {}
                for gc in geo_cols:
                    v = _normalize_cell(row.get(gc, "") or "")
                    if v:
                        geo[gc] = v
                if not geo and not any(row.get(vc) for vc in value_cols):
                    continue

                for vc in value_cols:
                    raw = row.get(vc, "") or ""
                    num = _parse_number(raw)
                    if num is None:
                        continue
                    sub = _subject_for_column(inferred.family, vc)
                    batch.append(
                        (
                            run_id,
                            json.dumps(geo, ensure_ascii=False),
                            json.dumps(sub, ensure_ascii=False),
                            _metric_slug(vc),
                            num,
                            _is_pct_column(vc),
                            source_file_id,
                        )
                    )
                    fact_rows += 1

                    if len(batch) >= 4000:
                        cur.executemany(
                            """
                            INSERT INTO kbw_facts
                                (election_run_id, geography, subject, metric, value, is_percentage, source_file_id)
                            VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                            """,
                            batch,
                        )
                        batch.clear()

            if batch:
                cur.executemany(
                    """
                    INSERT INTO kbw_facts
                        (election_run_id, geography, subject, metric, value, is_percentage, source_file_id)
                    VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                    """,
                    batch,
                )
    return fact_rows


def import_csv_path(
    path: Path,
    rel_str: str,
    *,
    source_file_id: int | None,
    inferred: InferredRun,
    clear_existing: bool = True,
) -> int:
    """Parse one CSV into kbw_facts; replaces prior rows from same source_file_id."""
    header, rows, _enc = _read_csv_rows(path)
    return _import_rows(
        header=header,
        rows=rows,
        source_file_id=source_file_id,
        inferred=inferred,
        clear_existing=clear_existing,
    )


def import_xlsx_path(
    path: Path,
    *,
    source_file_id: int | None,
    inferred: InferredRun,
    clear_existing: bool = True,
) -> int:
    """Import xlsx path."""
    header, rows = _read_xlsx_rows(path)
    return _import_rows(
        header=header,
        rows=rows,
        source_file_id=source_file_id,
        inferred=inferred,
        clear_existing=clear_existing,
    )


def import_xls_path(
    path: Path,
    *,
    source_file_id: int | None,
    inferred: InferredRun,
    clear_existing: bool = True,
) -> int:
    """Import xls path."""
    header, rows = _read_xls_rows(path)
    return _import_rows(
        header=header,
        rows=rows,
        source_file_id=source_file_id,
        inferred=inferred,
        clear_existing=clear_existing,
    )


def import_zip_csv_members(
    path: Path,
    *,
    source_file_id: int | None,
    inferred: InferredRun,
    rel_str: str,
) -> tuple[int, int]:
    """Import every CSV member from ZIP. Returns (facts, imported_member_count)."""
    facts = 0
    members = 0
    with zipfile.ZipFile(path) as zf:
        csv_members = [n for n in zf.namelist() if n.lower().endswith(".csv") and not n.endswith("/")]
        first = True
        for member in csv_members:
            data = zf.read(member)
            header, rows, _enc = _read_csv_rows_from_bytes(data)
            inner_inferred = infer_election_run(Path(f"{rel_str}/{member}")) or inferred
            facts += _import_rows(
                header=header,
                rows=rows,
                source_file_id=source_file_id,
                inferred=inner_inferred,
                clear_existing=first,
            )
            first = False
            members += 1
    return facts, members


def import_all_kbw_csv_facts(
    *,
    root: Path | None = None,
    limit: int | None = None,
    progress_every: int = 25,
    years: set[int] | None = None,
    exts: set[str] | None = None,
) -> dict[str, int]:
    """Import every supported catalogued file under mirror into kbw_facts."""
    root = root or Path("data/kbw_mirror/dane")
    stats = {
        "files_seen": 0,
        "files_imported": 0,
        "files_imported_csv": 0,
        "files_imported_zip": 0,
        "zip_members_imported_csv": 0,
        "files_imported_xlsx": 0,
        "files_imported_xls": 0,
        "facts_written": 0,
        "skipped_no_year": 0,
        "skipped_missing_file": 0,
        "skipped_unsupported": 0,
        "skipped_year_filter": 0,
        "skipped_ext_filter": 0,
        "errors": 0,
    }
    error_details: list[dict[str, str]] = []

    query = """
        SELECT id, rel_path
        FROM kbw_dane_files
        WHERE file_ext = ANY(%(allowed_exts)s)
          AND (%(years)s::int[] IS NULL OR year = ANY(%(years)s::int[]))
          AND (%(exts)s::text[] IS NULL OR file_ext = ANY(%(exts)s::text[]))
        ORDER BY rel_path
    """
    params = {
        "allowed_exts": ["csv", "zip", "xlsx", "xls"],
        "years": sorted(years) if years else None,
        "exts": sorted(exts) if exts else None,
    }
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    for fid, rel_str in rows:
        if limit is not None and stats["files_imported"] >= limit:
            break
        stats["files_seen"] += 1
        path = Path(rel_str)
        print(f"[kbw-import] START #{stats['files_seen']} {rel_str}", flush=True)
        if not path.is_file():
            stats["skipped_missing_file"] += 1
            print(f"[kbw-import] SKIP missing_file {rel_str}", flush=True)
            continue

        inferred = infer_election_run(path)
        if inferred is None:
            stats["skipped_no_year"] += 1
            print(f"[kbw-import] SKIP no_year {rel_str}", flush=True)
            continue
        if years is not None and inferred.year not in years:
            stats["skipped_year_filter"] += 1
            print(f"[kbw-import] SKIP year_filter year={inferred.year} {rel_str}", flush=True)
            continue

        try:
            n = 0
            ext = path.suffix.lower().lstrip(".")
            # SQL already filters requested extensions; keep this guard for safety.
            if exts is not None and ext not in exts:
                stats["skipped_ext_filter"] += 1
                print(f"[kbw-import] SKIP ext_filter ext={ext} {rel_str}", flush=True)
                continue
            if ext == "csv":
                n = import_csv_path(path, rel_str, source_file_id=fid, inferred=inferred)
                stats["files_imported_csv"] += 1
            elif ext == "zip":
                n, member_count = import_zip_csv_members(
                    path,
                    source_file_id=fid,
                    inferred=inferred,
                    rel_str=rel_str,
                )
                stats["files_imported_zip"] += 1
                stats["zip_members_imported_csv"] += member_count
            elif ext == "xlsx":
                n = import_xlsx_path(path, source_file_id=fid, inferred=inferred)
                stats["files_imported_xlsx"] += 1
            elif ext == "xls":
                n = import_xls_path(path, source_file_id=fid, inferred=inferred)
                stats["files_imported_xls"] += 1
            else:
                stats["skipped_unsupported"] += 1
                print(f"[kbw-import] SKIP unsupported ext={ext} {rel_str}", flush=True)
                continue
            stats["facts_written"] += n
            stats["files_imported"] += 1
            print(f"[kbw-import] OK ext={ext} facts={n} {rel_str}", flush=True)
            if progress_every > 0 and stats["files_seen"] % progress_every == 0:
                print(
                    f"[kbw-import] seen={stats['files_seen']} imported={stats['files_imported']} "
                    f"facts={stats['facts_written']} errors={stats['errors']}"
                )
        except Exception as exc:
            stats["errors"] += 1
            print(f"[kbw-import] ERR {type(exc).__name__}: {exc} {rel_str}", flush=True)
            if len(error_details) < 200:
                error_details.append(
                    {
                        "rel_path": rel_str,
                        "ext": path.suffix.lower().lstrip("."),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            if progress_every > 0 and stats["files_seen"] % progress_every == 0:
                print(
                    f"[kbw-import] seen={stats['files_seen']} imported={stats['files_imported']} "
                    f"facts={stats['facts_written']} errors={stats['errors']}"
                )

    if error_details:
        stats["error_samples"] = len(error_details)
        stats["error_details"] = error_details
    return stats


def clear_kbw_imported_facts() -> None:
    """Remove all KBW fact rows and election runs (catalog kbw_dane_files unchanged)."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            # TRUNCATE is much faster than DELETE for full table resets on large datasets.
            cur.execute("TRUNCATE TABLE kbw_facts, kbw_election_runs RESTART IDENTITY CASCADE")
