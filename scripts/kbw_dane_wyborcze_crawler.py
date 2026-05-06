#!/usr/bin/env python3
"""
Crawl https://danewyborcze.kbw.gov.pl/ (Krajowe Biuro Wyborcze — „Dane Wyborcze”),
discover election subpages and download published files under /dane/ (and common extensions).

Hub (Strona główna): https://danewyborcze.kbw.gov.pl/indexc4fa.html?title=Strona_główna

Uses only the Python standard library (no pip install).

Usage:
  python3 scripts/kbw_dane_wyborcze_crawler.py --out data/kbw_mirror

Options:
  --delay SEC       polite pause between requests (default 0.35)
  --resume          skip files that already exist and are non-empty
                    (no `OK …` lines for skips; progress lines `[html N] …` on stdout)
  --max-pages N     safety cap on distinct HTML pages crawled

Outputs:
  Mirror paths under --out (e.g. dane/2019/sejmsenat/….zip)
  _crawl_manifest.jsonl — one JSON object per line (file_ok, file_skip, errors)
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


def request_url_ascii(url: str) -> str:
    """IRI → URI: http.client encodes the request line as ASCII only."""
    p = urlparse(url)
    if not p.scheme:
        return url
    path = p.path or ""
    if path:
        parts = path.split("/")
        enc_parts = [quote(unquote(seg), safe="") for seg in parts]
        path = "/".join(enc_parts)
    query = p.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(pairs, doseq=True, encoding="utf-8", errors="replace")
    return urlunparse((p.scheme, p.netloc, path, p.params, query, p.fragment))


def _ssl_context(insecure: bool) -> ssl.SSLContext:
    """Ssl context."""
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

BASE_HOST = "danewyborcze.kbw.gov.pl"
BASE_URL = f"https://{BASE_HOST}/"

DEFAULT_START = f"{BASE_URL}indexc4fa.html?title=Strona_g%C5%82%C3%B3wna"

INDEX_HTML_RE = re.compile(r"^index[0-9a-f]+\.html$", re.IGNORECASE)

FILE_SUFFIXES = (
    ".zip",
    ".csv",
    ".xlsx",
    ".xls",
    ".pdf",
    ".ods",
    ".doc",
    ".docx",
    ".txt",
    ".xml",
    ".json",
    ".7z",
    ".rar",
)

USER_AGENT = (
    "PlujkaKBWCrawler/1.0 (+local mirror of public KBW Dane Wyborcze; "
    "https://danewyborcze.kbw.gov.pl/) Python-urllib"
)


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        """Init."""
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle starttag."""
        if tag.lower() != "a":
            return
        for key, val in attrs:
            if key.lower() == "href" and val:
                self.hrefs.append(val)


def _normalize_href(href: str) -> str:
    """Normalize href."""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    return href


def canonical_page_url(url: str) -> str:
    """Strip #fragments so TOC anchors do not enqueue the same article dozens of times."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def should_queue_html(url: str) -> bool:
    """Should queue html."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc and parsed.netloc != BASE_HOST:
        return False
    name = Path(parsed.path or "").name
    if not INDEX_HTML_RE.match(name):
        return False
    q = parsed.query.lower()
    if "action=edit" in q or "action=history" in q:
        return False
    if "redlink=1" in q:
        return False
    qs = parse_qs(parsed.query)
    titles = qs.get("title", [])
    if titles:
        t = unquote(titles[0])
        if t.startswith(("Specjalna:", "Dyskusja:", "Pomoc:", "Użytkownik:", "Plik:")):
            return False
    return True


def is_download_target(url: str) -> bool:
    """Is download target."""
    lower = url.lower()
    if "/dane/" in lower:
        return True
    path = urlparse(url).path.lower()
    return any(path.endswith(s) for s in FILE_SUFFIXES)


def local_path_for_url(url: str, out_root: Path) -> Path:
    """Local path for url."""
    parsed = urlparse(url)
    rel = unquote((parsed.path or "").lstrip("/"))
    if not rel:
        rel = "root.bin"
    return out_root / rel


def http_get(url: str, ssl_ctx: ssl.SSLContext, binary: bool = True) -> tuple[bytes | None, str | None]:
    """Http get."""
    req = Request(request_url_ascii(url), headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=180, context=ssl_ctx) as resp:
            body = resp.read()
            if binary:
                return body, None
            return body, None
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, str(e.reason)
    except OSError as e:
        return None, str(e)
    except UnicodeEncodeError as e:
        return None, f"url encoding: {e}"


def append_manifest(manifest_path: Path, record: dict) -> None:
    """Append manifest."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def crawl(
    out_root: Path,
    start_urls: list[str],
    delay: float,
    resume: bool,
    max_pages: int | None,
    manifest_path: Path,
    ssl_ctx: ssl.SSLContext,
) -> int:
    """Crawl."""
    seen_html: set[str] = set()
    downloaded_urls: set[str] = set()
    queue: deque[str] = deque()
    for u in start_urls:
        queue.append(canonical_page_url(urljoin(BASE_URL, u)))

    errors = 0
    pages_processed = 0
    files_written = 0
    resume_skips = 0

    # stdout (nie stderr): wiele IDE pokazuje tylko stdout — tu ma być widać żywy postęp.
    print(
        "Crawl started."
        + (" --resume: istniejące pliki są pomijane (OK … tylko przy realnym pobraniu)." if resume else ""),
        flush=True,
    )

    while queue:
        url = canonical_page_url(queue.popleft())
        if url in seen_html:
            continue
        if not should_queue_html(url):
            continue
        if max_pages is not None and pages_processed >= max_pages:
            print(f"Stopped: reached --max-pages={max_pages}", flush=True)
            break
        seen_html.add(url)
        pages_processed += 1
        short_url = url if len(url) <= 96 else url[:93] + "..."
        print(f"[html {pages_processed}] kolejka={len(queue)}  {short_url}", flush=True)

        time.sleep(delay)
        body, err = http_get(url, ssl_ctx, binary=True)
        if err or body is None:
            errors += 1
            append_manifest(manifest_path, {"kind": "html_error", "url": url, "error": err})
            continue

        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        parser = _HrefCollector()
        try:
            parser.feed(text)
        except Exception:
            pass

        for raw in parser.hrefs:
            href = _normalize_href(raw)
            absolute = urljoin(url, href)
            parsed = urlparse(absolute)
            if parsed.netloc and parsed.netloc != BASE_HOST:
                continue

            if is_download_target(absolute):
                if absolute in downloaded_urls:
                    continue
                downloaded_urls.add(absolute)
                dest = local_path_for_url(absolute, out_root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if resume and dest.is_file() and dest.stat().st_size > 0:
                    resume_skips += 1
                    if resume_skips % 250 == 0:
                        print(
                            f"... pominięto {resume_skips} plików już na dysku (--resume), bez linii OK",
                            flush=True,
                        )
                    append_manifest(
                        manifest_path,
                        {
                            "kind": "file_skip",
                            "url": absolute,
                            "path": str(dest),
                            "bytes": dest.stat().st_size,
                        },
                    )
                    continue
                time.sleep(delay)
                data, derr = http_get(absolute, ssl_ctx, binary=True)
                if derr or data is None:
                    errors += 1
                    append_manifest(manifest_path, {"kind": "file_error", "url": absolute, "error": derr})
                    continue
                dest.write_bytes(data)
                files_written += 1
                append_manifest(
                    manifest_path,
                    {"kind": "file_ok", "url": absolute, "path": str(dest), "bytes": len(data)},
                )
                print(f"OK {len(data):>10}  {absolute}")
            elif should_queue_html(absolute):
                c = canonical_page_url(absolute)
                if c not in seen_html:
                    queue.append(c)

    summary = (
        "Done. html_pages="
        f"{len(seen_html)} files_written={files_written} "
        f"files_skipped_resume={resume_skips} queue_errors={errors}"
    )
    print(summary, flush=True)
    print(summary, file=sys.stderr, flush=True)
    return errors


def main() -> None:
    """Main."""
    ap = argparse.ArgumentParser(description="Mirror KBW Dane Wyborcze index pages and download /dane/ files.")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("data/kbw_mirror"),
        help="Output root",
    )
    ap.add_argument("--delay", type=float, default=0.35, help="Seconds between HTTP requests")
    ap.add_argument("--resume", action="store_true", help="Skip existing non-empty files")
    ap.add_argument("--max-pages", type=int, default=None, help="Max distinct HTML pages to crawl")
    ap.add_argument("--start", action="append", dest="starts", metavar="URL", help="Extra start URL (repeatable)")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (use if SSL fails on your Python install)",
    )
    args = ap.parse_args()

    ssl_ctx = _ssl_context(args.insecure)

    out_root = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "_crawl_manifest.jsonl"

    starts = [DEFAULT_START]
    if args.starts:
        starts.extend(args.starts)

    err = crawl(
        out_root=out_root,
        start_urls=starts,
        delay=args.delay,
        resume=args.resume,
        max_pages=args.max_pages,
        manifest_path=manifest_path,
        ssl_ctx=ssl_ctx,
    )
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
