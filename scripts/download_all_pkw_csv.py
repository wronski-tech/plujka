#!/usr/bin/env python3
"""Download all PKW CSV archives exposed on PKW 'dane_w_arkuszach' pages."""

from __future__ import annotations

import argparse
import re
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests

PAGE_URL = "https://sejmsenat2023.pkw.gov.pl/sejmsenat2023/pl/dane_w_arkuszach"


def parse_args() -> argparse.Namespace:
    """Parse args."""
    parser = argparse.ArgumentParser(description="Download PKW CSV ZIP archives.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/pkw_all"),
        help="Output directory for downloaded ZIPs and extracted CSVs.",
    )
    parser.add_argument(
        "--page-url",
        action="append",
        default=[PAGE_URL],
        help="PKW page URL with dane_w_arkuszach (can be provided multiple times).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds.",
    )
    return parser.parse_args()


def _election_prefix_from_page(page_url: str) -> str:
    """Election prefix from page."""
    path_parts = [part for part in urlparse(page_url).path.split("/") if part]
    if not path_parts:
        raise RuntimeError(f"Cannot infer election prefix from URL: {page_url}")
    return path_parts[0]


def _load_bundle_url(page_url: str, timeout: int) -> str:
    """Load bundle url."""
    response = requests.get(page_url, timeout=timeout)
    response.raise_for_status()
    match = re.search(r'<script src="([^"]*?/j/[^"]+\.min\.js)"', response.text)
    if not match:
        raise RuntimeError("Could not locate PKW JS bundle URL in page HTML.")
    return urljoin(page_url, match.group(1))


def _extract_dataset_stems(bundle_text: str) -> list[str]:
    # PKW builds data-sheet links from these stem names in the frontend model.
    """Extract dataset stems."""
    candidates = set(re.findall(r'"([a-z0-9]+(?:_[a-z0-9]+){2,})"', bundle_text))
    keywords = (
        "wyniki_",
        "protokoly_",
        "sklad_komisji_",
    )
    stems = sorted(stem for stem in candidates if stem.startswith(keywords))
    if not stems:
        raise RuntimeError("No PKW CSV stems discovered in the JS bundle.")
    return stems


def _is_zip_content(content: bytes) -> bool:
    """Is zip content."""
    return content.startswith(b"PK\x03\x04")


def _download_and_extract(
    stem: str,
    out_dir: Path,
    timeout: int,
    base_data_url: str,
) -> tuple[bool, str]:
    """Download and extract."""
    url = urljoin(base_data_url, f"data/csv/{stem}_csv.zip")
    response = requests.get(url, timeout=timeout)
    if response.status_code != 200:
        return False, f"{stem}: HTTP {response.status_code}"

    payload = response.content
    if not _is_zip_content(payload):
        return False, f"{stem}: response is not ZIP"

    zip_path = out_dir / "zip" / f"{stem}_csv.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(payload)

    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        archive.extractall(csv_dir)

    return True, stem


def main() -> None:
    """Main."""
    args = parse_args()
    all_downloaded = 0
    all_skipped = 0

    for page_url in args.page_url:
        election_prefix = _election_prefix_from_page(page_url)
        target_dir = args.out_dir / election_prefix
        base_data_url = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}/{election_prefix}/"

        bundle_url = _load_bundle_url(page_url, args.timeout)
        bundle_response = requests.get(bundle_url, timeout=args.timeout)
        bundle_response.raise_for_status()

        stems = _extract_dataset_stems(bundle_response.text)
        print(f"\n[{election_prefix}] Discovered dataset stems: {len(stems)}")

        downloaded: list[str] = []
        skipped: list[str] = []
        for stem in stems:
            ok, message = _download_and_extract(
                stem=stem,
                out_dir=target_dir,
                timeout=args.timeout,
                base_data_url=base_data_url,
            )
            if ok:
                downloaded.append(message)
                print(f"[{election_prefix}] [OK] {message}")
            else:
                skipped.append(message)
                print(f"[{election_prefix}] [SKIP] {message}")

        print(f"[{election_prefix}] Downloaded ZIPs: {len(downloaded)}")
        print(f"[{election_prefix}] Skipped entries: {len(skipped)}")
        all_downloaded += len(downloaded)
        all_skipped += len(skipped)

    print(f"\nTotal downloaded ZIPs: {all_downloaded}")
    print(f"Total skipped entries: {all_skipped}")


if __name__ == "__main__":
    main()
