"""Catalog files under data/kbw_mirror/dane (KBW Dane Wyborcze mirror).

Mixed formats (CSV, XLS/XLSX, ZIP) and evolving layouts: we store inventory +
CSV headers for routing future imports without coupling to PKW `data/pkw_all`."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.services import db

DEFAULT_KBW_ROOT = Path("data/kbw_mirror/dane")

_YEAR_DIR = re.compile(r"^\d{4}$")


def _posix_rel(path: Path) -> str:
    """Posix rel."""
    return path.as_posix()


def _parts_after_dane(rel: Path) -> list[str]:
    """Parts after dane."""
    parts = rel.parts
    try:
        idx = parts.index("dane")
    except ValueError:
        return list(rel.parts)
    return list(parts[idx + 1 :])


def _dataset_key(rel: Path) -> str | None:
    """Dataset key."""
    inner = _parts_after_dane(rel)
    if len(inner) < 2:
        return None
    return "/".join(inner[:-1])


def _year_from_path(rel: Path) -> int | None:
    """Year from path."""
    inner = _parts_after_dane(rel)
    if not inner:
        return None
    if inner[0] == "RW" and len(inner) > 1 and _YEAR_DIR.match(inner[1]):
        return int(inner[1])
    if _YEAR_DIR.match(inner[0]):
        return int(inner[0])
    return None


def _guess_delimiter(line: str) -> str:
    """Guess delimiter."""
    line = line.rstrip("\r\n")
    if not line:
        return ";"
    tabs = line.count("\t")
    semi = line.count(";")
    comma = line.count(",")
    best = max((tabs, "\t"), (semi, ";"), (comma, ","), key=lambda x: x[0])
    return best[1] if best[0] > 0 else ";"


def _read_csv_header(path: Path) -> tuple[list[str] | None, str | None, str | None, str | None]:
    """Returns (header columns, encoding, delimiter, error)."""
    for encoding in ("utf-8-sig", "utf-8", "cp1250"):
        try:
            with path.open("r", encoding=encoding, newline="") as file:
                first = file.readline()
        except OSError as exc:
            return None, None, None, str(exc)
        except UnicodeDecodeError:
            continue
        if not first:
            return [], encoding, ";", None
        first = first.replace("\ufeff", "")
        delim = _guess_delimiter(first)
        try:
            row = next(csv.reader([first], delimiter=delim))
        except Exception as exc:  # noqa: BLE001 — surface odd CSV quirks in profile_error
            return None, encoding, delim, str(exc)
        header = [c.strip().strip('"') for c in row]
        return header, encoding, delim, None
    return None, None, None, "could not decode (tried utf-8-sig, utf-8, cp1250)"


def _file_kind(ext: str) -> str:
    """File kind."""
    e = ext.lower().lstrip(".")
    if e == "csv":
        return "csv"
    if e in ("xls", "xlsx"):
        return "spreadsheet"
    if e == "zip":
        return "archive"
    if e == "txt":
        return "text"
    return "other"


def profile_kbw_dane_files(root: Path | None = None) -> dict[str, int]:
    """Walk mirror tree and upsert rows into kbw_dane_files. Safe to re-run as downloads grow."""
    root = root or DEFAULT_KBW_ROOT
    stats = {"files": 0, "csv_ok": 0, "csv_err": 0, "skipped_hidden": 0}

    if not root.is_dir():
        return stats

    repo_paths = sorted(root.rglob("*"))
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            for path in repo_paths:
                if not path.is_file():
                    continue
                if path.name.startswith(".") or path.name == "Thumbs.db":
                    stats["skipped_hidden"] += 1
                    continue

                # Stable relative path from repo cwd (matches crawler output layout).
                rel_path = path if not path.is_absolute() else path.relative_to(Path.cwd())
                rel_str = _posix_rel(rel_path)
                ext = path.suffix.lower().lstrip(".") or "none"
                kind = _file_kind(ext)
                try:
                    st = path.stat()
                except OSError:
                    continue

                size_b = st.st_size
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                ds_key = _dataset_key(rel_path)
                year_guess = _year_from_path(rel_path)

                header_json: Any | None = None
                col_count: int | None = None
                delim: str | None = None
                enc_used: str | None = None
                profile_error: str | None = None

                if kind == "csv":
                    hdr, enc_used, delim, profile_error = _read_csv_header(path)
                    stats["files"] += 1
                    if hdr is not None and profile_error is None:
                        header_json = hdr
                        col_count = len(hdr)
                        stats["csv_ok"] += 1
                    else:
                        stats["csv_err"] += 1
                else:
                    stats["files"] += 1

                cur.execute(
                    """
                    INSERT INTO kbw_dane_files (
                        rel_path, file_name, file_ext, file_kind,
                        size_bytes, mtime, dataset_key, year,
                        csv_delimiter, encoding_used, column_count, header, profile_error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (rel_path) DO UPDATE SET
                        file_name = EXCLUDED.file_name,
                        file_ext = EXCLUDED.file_ext,
                        file_kind = EXCLUDED.file_kind,
                        size_bytes = EXCLUDED.size_bytes,
                        mtime = EXCLUDED.mtime,
                        dataset_key = EXCLUDED.dataset_key,
                        year = EXCLUDED.year,
                        csv_delimiter = EXCLUDED.csv_delimiter,
                        encoding_used = EXCLUDED.encoding_used,
                        column_count = EXCLUDED.column_count,
                        header = EXCLUDED.header,
                        profile_error = EXCLUDED.profile_error,
                        profiled_at = NOW()
                    """,
                    (
                        rel_str,
                        path.name,
                        ext,
                        kind,
                        size_b,
                        mtime,
                        ds_key,
                        year_guess,
                        delim,
                        enc_used,
                        col_count,
                        json.dumps(header_json) if header_json is not None else None,
                        profile_error,
                    ),
                )

    return stats


def profile_mirror_if_present(root: Path | None = None) -> None:
    """No-op when `data/kbw_mirror/dane` is missing (e.g. fresh clone)."""
    root = root or DEFAULT_KBW_ROOT
    if root.is_dir():
        profile_kbw_dane_files(root)
