from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import Job


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def infer_metadata(text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    hourly = re.search(r"\$\s*([\d,]+)(?:\.\d+)?\s*[-–—]\s*\$?\s*([\d,]+)(?:\.\d+)?\s*(?:/\s*h|/\s*hr|per hour|hourly)", text, re.I)
    if hourly:
        meta.update(rate_min=_number(hourly.group(1)), rate_max=_number(hourly.group(2)), currency="USD", rate_period="hour")
    annual = re.search(r"(?:EUR\s*|€\s*|USD\s*|\$\s*)([\d,]+)\s*[-–—]\s*(?:EUR\s*|€\s*|USD\s*|\$\s*)?([\d,]+)\s*(?:/\s*year|per year|yearly|annual)", text, re.I)
    if not annual:
        annual = re.search(r"annual(?:\s+salary)?(?:\s+range)?\s*[:\-]?\s*(?:EUR\s*|€\s*|USD\s*|\$\s*)([\d,]+)\s*[-–—]\s*(?:EUR\s*|€\s*|USD\s*|\$\s*)?([\d,]+)", text, re.I)
    if annual:
        currency = "EUR" if re.search(r"EUR|€", annual.group(0), re.I) else "USD"
        meta.update(annual_min=_number(annual.group(1)), annual_max=_number(annual.group(2)), currency=currency)
    proposals = re.search(r"(\d+)\s*\+?\s*proposals", text, re.I)
    if proposals:
        meta["proposals"] = int(proposals.group(1))
    interviewing = re.search(r"(\d+)\s+(?:people\s+)?interviewing|interviewing\s*[:\-]?\s*(\d+)", text, re.I)
    if interviewing:
        meta["interviewing"] = int(interviewing.group(1) or interviewing.group(2))
    minutes = re.search(r"(\d+)\s*minutes?\s*(?:old|ago)", text, re.I)
    days = re.search(r"(\d+)\s*days?\s*(?:old|ago)", text, re.I)
    if minutes:
        meta["freshness_minutes"] = int(minutes.group(1))
    elif days:
        meta["freshness_minutes"] = int(days.group(1)) * 1440
    if re.search(r"\bremote\b", text, re.I):
        meta["remote"] = True
    if re.search(r"\b(?:EU|Europe|CET)\b", text):
        meta["eu_compatible"] = True
    return meta


def load_job(path: str | None, text: str | None, track: str, meta_path: str | None = None, extract_profile: bool = True, extractor: Any = None) -> Job:
    if path:
        source_path = Path(path)
        raw = source_path.read_text(encoding="utf-8")
        job_id = source_path.stem
        source = str(source_path)
    else:
        raw = text or ""
        job_id = "stdin-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        source = "stdin"
    if not raw.strip():
        raise ValueError("Job description is empty")
    metadata = infer_metadata(raw)
    if meta_path:
        metadata.update(json.loads(Path(meta_path).read_text(encoding="utf-8")))
    profile = {}
    if extract_profile:
        from .extraction import get_default_extractor

        profile = (extractor or get_default_extractor()).extract(raw.strip())
    return Job(job_id=job_id, text=raw.strip(), track=track.upper(), source=source, metadata=metadata, profile=profile)
