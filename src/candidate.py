"""The candidate side of the match.

The source of truth is the two PDFs in the project root: the public CV and the
personal operating manual. Encoding them as keyword tables is what made the old
scorer lossy, so the advisor reads them as prose instead.

Extraction needs ``pypdf`` and runs only when you refresh the cache. The cached
Markdown is gitignored (it is private); a fictional example ships instead, so runs stay dependency-free
on a stock Python.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "candidate" / "profile.md"
EXAMPLE = ROOT / "candidate" / "profile.example.md"
SOURCES = (
    ("CV", "cv.pdf"),
    ("Operating manual", "operating-manual.pdf"),
)


def refresh_from_pdfs() -> Path:
    """Re-extract the cached profile from the PDFs. Requires ``pypdf``."""
    from pypdf import PdfReader

    parts = ["# Candidate profile", "", "Generated from the PDFs in the project root. Do not edit by hand; run `python -m src.cli profile refresh`.", ""]
    for title, filename in SOURCES:
        source = ROOT / filename
        if not source.exists():
            continue
        pages = PdfReader(str(source)).pages
        text = "\n".join(page.extract_text() or "" for page in pages)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        parts += [f"## {title}", "", text, ""]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text("\n".join(parts), encoding="utf-8")
    return CACHE


def load() -> str:
    """The candidate profile as prose, or an empty string when never refreshed."""
    # A real profile, once refreshed from your own PDFs, wins. Otherwise the
    # fictional example shipped with the repository keeps the demo working.
    source = CACHE if CACHE.exists() else EXAMPLE
    if not source.exists():
        return ""
    return source.read_text(encoding="utf-8").strip()


def is_available() -> bool:
    return len(load()) > 500
