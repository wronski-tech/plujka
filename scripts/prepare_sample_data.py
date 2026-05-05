#!/usr/bin/env python3
"""Prepare raw and sample PKW Sejm data from the provided ZIP archive."""

from __future__ import annotations

import argparse
import csv
import io
import zipfile
from pathlib import Path


DEFAULT_ZIP_PATH = Path(
    "/Users/DominikWro/Downloads/wyniki_gl_na_listy_po_obwodach_sejm_csv.zip"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract and create deterministic sample data for the project."
    )
    parser.add_argument(
        "--zip-path",
        type=Path,
        default=DEFAULT_ZIP_PATH,
        help="Path to source PKW ZIP file.",
    )
    parser.add_argument(
        "--raw-output",
        type=Path,
        default=Path("data/raw/wyniki_gl_na_listy_po_obwodach_sejm_utf8.csv"),
        help="Where to store full extracted CSV.",
    )
    parser.add_argument(
        "--sample-output",
        type=Path,
        default=Path("data/sample/sejm_results_sample_1000.csv"),
        help="Where to store sample CSV.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=1000,
        help="Number of rows to keep in sample output.",
    )
    return parser.parse_args()


def _read_csv_from_zip(zip_path: Path) -> tuple[list[str], list[list[str]], str]:
    with zipfile.ZipFile(zip_path) as archive:
        csv_names = [name for name in archive.namelist() if name.endswith(".csv")]
        if not csv_names:
            raise ValueError(f"No CSV file found inside archive: {zip_path}")
        csv_name = csv_names[0]
        with archive.open(csv_name) as csv_file:
            decoded = io.TextIOWrapper(csv_file, encoding="utf-8", newline="")
            reader = csv.reader(decoded, delimiter=";")
            rows = list(reader)

    if not rows:
        raise ValueError(f"CSV file is empty in archive: {zip_path}")

    header = [column.replace("\ufeff", "").strip('"') for column in rows[0]]
    data_rows = rows[1:]
    return header, data_rows, csv_name


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter=";")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    header, rows, csv_name = _read_csv_from_zip(args.zip_path)

    _write_csv(args.raw_output, header, rows)

    sample_rows = rows[: args.sample_size]
    _write_csv(args.sample_output, header, sample_rows)

    print(f"Source CSV in archive: {csv_name}")
    print(f"Total rows loaded: {len(rows)}")
    print(f"Raw CSV written to: {args.raw_output}")
    print(f"Sample rows written: {len(sample_rows)}")
    print(f"Sample CSV written to: {args.sample_output}")


if __name__ == "__main__":
    main()
