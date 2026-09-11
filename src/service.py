from __future__ import annotations

import hashlib
from typing import Any

from . import advise, funnel
from .adapters import ADAPTERS
from .db import JobDatabase
from .evaluate import evaluate
from .extraction import HeuristicExtractor, get_default_extractor
from .ingest import infer_metadata
from .models import Job


def ingest_jobs(db: JobDatabase, adapter_name: str, payload: dict[str, Any], track: str) -> tuple[list[dict[str, Any]], "funnel.FunnelReport"]:
    if adapter_name not in ADAPTERS:
        raise ValueError(f"Unknown ingestion adapter: {adapter_name}")
    track = track.upper()
    if track not in {"A", "B"}:
        raise ValueError("Track must be A or B")
    raw_payload = dict(payload)
    if raw_payload.get("description"):
        raw_payload["content_hash"] = hashlib.sha256(str(raw_payload["description"]).encode("utf-8")).hexdigest()[:16]
    policy = funnel.FunnelPolicy.from_env(raw_payload)
    report = funnel.FunnelReport()
    # A feed adapter that fetches per-posting details can skip the ones the title
    # filter would reject anyway, sparing the source those requests entirely. The
    # predicate counts as it goes so the report still says how many were seen.
    feed = adapter_name not in {"manual", "file"}

    def accept_title(title: str) -> bool:
        report.fetched += 1
        if policy.wants_title(title):
            return True
        report.title_rejected += 1
        if len(report.rejected_titles) < 12:
            report.rejected_titles.append(title)
        return False

    if feed:
        raw_payload["accept_title"] = accept_title
    items = ADAPTERS[adapter_name].ingest(raw_payload)
    if not report.fetched:
        # The adapter ignored the predicate and returned everything it found.
        report.fetched = len(items)
    # A feed is extracted offline, always. The point of the funnel is to spend
    # nothing on postings that will not be assessed, and a model extraction runs
    # *before* the gate — it would bill every fetched posting to decide which
    # ones are worth billing. The advisor reads the raw posting anyway, so the
    # richer profile buys nothing on this path.
    extractor = HeuristicExtractor() if feed else get_default_extractor()
    advisor = advise.get_default_advisor()

    # A single pasted job is a deliberate act, so it is never filtered out by its
    # title and always earns an assessment. Only feeds go through the funnel.
    prepared: list[tuple[Job, Any, dict[str, Any]]] = []
    seen: set[str] = set()
    for item in items:
        if not item.description:
            continue
        if feed and not policy.wants_title(item.title):
            report.title_rejected += 1
            if len(report.rejected_titles) < 12:
                report.rejected_titles.append(item.title)
            continue
        if feed:
            key = funnel.duplicate_key(item.company, item.title)
            if key in seen:
                report.duplicates += 1
                continue
            seen.add(key)
        metadata = infer_metadata(item.description)
        metadata.update(item.metadata)
        profile = extractor.extract(item.description)
        digest = hashlib.sha256(item.source_ref.encode("utf-8")).hexdigest()[:20]
        job = Job(job_id=digest, text=item.description, track=track, source=item.source, metadata=metadata, profile=profile)
        prepared.append((job, item, profile))
    report.scored = len(prepared)

    if not feed:
        chosen = {job.job_id for job, _, _ in prepared} if advisor else set()
    elif advisor:
        ranked = [(job.job_id, evaluate(job).overall_score) for job, _, _ in prepared]
        picked, skipped = funnel.shortlist(ranked, policy)
        chosen, report.skipped_by_gate = set(picked), skipped
    else:
        chosen = set()

    saved = []
    for job, item, profile in prepared:
        assessment = None
        if job.job_id in chosen and advisor:
            assessment = advise.assess(item.description, advisor)
            report.assessed += 1
        result = evaluate(job, assessment)
        title = item.title or profile.get("role_title") or "Untitled job"
        company = profile.get("company") or item.company
        saved.append(db.upsert_job({
            "id": job.job_id, "title": title[:200], "company": company[:200], "source": item.source,
            "source_ref": item.source_ref, "source_url": item.source_url, "raw_text": item.description,
            "track": track, "metadata": job.metadata, "profile": profile, "evaluation": result.to_dict(),
            "decision": result.decision, "overall_score": result.overall_score, "discovered_at": item.discovered_at,
        }))
    return saved, report


def assess_job(db: JobDatabase, job_id: str) -> dict[str, Any]:
    """Run the advisor on one already-stored job, on demand.

    Feeds are ingested without spending anything, so most rows arrive with a
    deterministic score only. This is the explicit, per-job decision to pay for
    the semantic read — one click, one assessment, nothing implicit.
    """
    row = db.get_job(job_id)
    advisor = advise.get_default_advisor()
    if advisor is None:
        raise ValueError(
            "No advisor available. Put a key in .env and refresh the candidate profile "
            "(python -m src.cli profile refresh).")
    job = Job(job_id=row["id"], text=row["raw_text"], track=row["track"], source=row["source"],
              metadata=row["metadata"], profile=row["profile"])
    assessment = advise.assess(row["raw_text"], advisor)
    if assessment and "advisor_error" in assessment:
        raise ValueError(assessment["advisor_error"])
    result = evaluate(job, assessment)
    return db.upsert_job({
        "id": row["id"], "title": row["title"], "company": row["company"], "source": row["source"],
        "source_ref": row["source_ref"], "source_url": row["source_url"], "raw_text": row["raw_text"],
        "track": row["track"], "metadata": row["metadata"], "profile": row["profile"],
        "evaluation": result.to_dict(), "decision": result.decision,
        "overall_score": result.overall_score, "discovered_at": row["discovered_at"],
    })
