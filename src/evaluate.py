from __future__ import annotations

from datetime import datetime, timezone

from . import advise
from .models import Evaluation, Job
from .score import TRACK_B_HOURLY_FLOOR, ScoreResult, number, rate, score_job


# Indicator weights. They shape the score shown on the card; with an assessment
# present the decision comes from the match verdict instead, because a weighted
# average of six numbers cannot express "he would be the wrong person for this".
WEIGHTS = {
    "A": {"capability_fit": 0.20, "working_style_fit": 0.25, "conditions_fit": 0.15,
          "employer_benefit": 0.15, "win_probability": 0.10, "career_capital": 0.15},
    "B": {"capability_fit": 0.25, "working_style_fit": 0.20, "conditions_fit": 0.20,
          "employer_benefit": 0.10, "win_probability": 0.15, "career_capital": 0.10},
}


def weighted_score(scores: dict[str, float], track: str) -> float:
    """Weighted average over the dimensions actually present.

    The deterministic scorer cannot judge `employer_benefit`, so it simply omits
    it; renormalising keeps its scores on the same 0-10 scale as an assessed one.
    """
    weights = {name: weight for name, weight in WEIGHTS[track].items() if name in scores}
    total = sum(weights.values())
    if not total:
        return 0.0
    return sum(scores[name] * weight for name, weight in weights.items()) / total


def _compensation(job: Job, result: ScoreResult) -> str:
    meta = job.metadata
    if job.track == "B":
        low, high = rate(meta)
        if low is not None and high is not None and high >= 80 and result.scores["capability_fit"] >= 6.5:
            return "Target about $100/h (within the published range); price the senior application judgment, not React tenure alone."
        if low is not None and high is not None:
            return f"Published ${low:g}-${high:g}/h; do not go below the configured ${TRACK_B_HOURLY_FLOOR}/h floor without a deliberate bridge-cash exception."
        return "Test $80-$100/h for strong-fit work; ask for budget and expected hours before committing."
    annual_low, annual_high = number(meta.get("annual_min")), number(meta.get("annual_max"))
    if annual_low is not None and annual_high is not None:
        currency = meta.get("currency", "EUR")
        return f"Published {currency} {annual_low:,.0f}-{annual_high:,.0f}/year; target the upper half when scope and interview evidence support it."
    return "Target the core range from the candidate profile, adjusted for contract, geography, scope and total hours."


def _positioning(job: Job, result: ScoreResult) -> tuple[list[str], str]:
    t = job.text.casefold()
    if "react" in t:
        angle = [
            "React is newer for me. Senior frontend engineering isn't.",
            "Lead with the deepest production application experience in the profile.",
            "Use the four live migrations, data-heavy flows, performance and API contracts as evidence.",
            "Be explicit that current React/Next.js/TypeScript plus FastAPI/PostgreSQL work is recent production experience.",
        ]
        return angle, "frontend-heavy"
    if any(contains_term(t, term) for term in ("ai", "llm", "machine learning")):
        return ["Lead with shipped AI products and AI-native delivery.", "Separate verified production evidence from transferable experience.", "Emphasize domain modeling, testing and verification guardrails."], "AI/product"
    if contains_any(t, "frontend", "vue", "nuxt", "typescript"):
        return ["Lead with deep production frontend/application experience.", "Use migrations, complex business flows and performance as proof.", "Connect hands-on implementation to product and system context."], "frontend-heavy"
    return ["Lead with senior product/application engineering, not generic full-stack claims.", "Use problem-context-constraint-action-result evidence.", "State exact-stack gaps plainly and explain the transferable production judgment."], "product/software"


def contains_any(text: str, *patterns: str) -> bool:
    return any(pattern in text for pattern in patterns)


def contains_term(text: str, term: str) -> bool:
    if len(term) <= 3 and term.isalnum():
        import re
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def evaluate(job: Job, assessment: dict | None = None) -> Evaluation:
    """Score a job, optionally using a semantic assessment.

    Without an assessment this is the deterministic path, unchanged. With one,
    ``JOB_INBOX_ADVISOR`` decides who is authoritative: ``findings`` feeds the
    model's per-dimension scores through the weights and thresholds below, and
    ``verdict`` takes the model's own call. Deterministic hard blockers win in
    both, because a rate under the floor is arithmetic rather than judgement.
    """
    result = score_job(job)
    assessment = assessment or {}
    usable = "dimension_scores" in assessment
    active = advise.mode()
    if usable and active == "off":
        active = "findings"
    elif not usable:
        active = "off"

    scores = {name: float(value) for name, value in assessment["dimension_scores"].items()} if usable else result.scores
    weighted = weighted_score(scores, job.track)
    if result.blockers:
        weighted = min(weighted, 3.8)
    overall = round(weighted, 1)
    metadata_count = sum(key in job.metadata for key in ("rate_min", "freshness_minutes", "proposals", "interviewing"))
    confidence = assessment.get("confidence") if usable else None
    if not confidence:
        confidence = "high" if result.blockers or (len(job.text) >= 300 and metadata_count >= 3) else "medium" if len(job.text) >= 350 or metadata_count >= 2 else "low"
    if result.blockers:
        confidence = "high"
    if active == "verdict":
        overall = float(assessment["model_score"])
        decision = assessment["model_decision"]
    elif usable:
        # The match verdict decides, not the weighted average: a role can score
        # respectably and still be the wrong pairing for one of the two sides.
        decision = advise.MATCH_TO_DECISION[assessment["match"]["verdict"]]
        if decision == "APPLY" and confidence == "low":
            decision = "MAYBE"
    elif overall < 5.3:
        decision = "SKIP"
    elif overall < 7.0 or confidence == "low":
        # A thin posting can reach an APPLY score on keywords alone. Ask for the
        # missing scope rather than recommending a proposal on that basis.
        decision = "MAYBE"
    else:
        decision = "APPLY"
    if result.blockers:
        decision = "SKIP"
        overall = min(overall, 3.8)
    angle, cv = _positioning(job, result)
    capital_level = "high" if scores["career_capital"] >= 7 else "medium" if scores["career_capital"] >= 4.5 else "low"
    capital_reason = ", ".join(result.career_items[:4]) if result.career_items else "Limited new evidence beyond the current profile."
    if usable:
        # An assessed job explains itself through evidence-backed findings, which
        # say more than a restated score ever did.
        ranked = sorted(assessment["findings"], key=lambda item: {"blocker": 0, "concern": 1, "strength": 2}[item["verdict"]])
        why = [f"{item['claim']} ({item['dimension'].replace('_', ' ')})" for item in ranked[:5]]
    else:
        strongest_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:2]
        why = [f"{name.replace('_', ' ').title()}: {value}/10." for name, value in strongest_scores]
        why.extend(result.strong_matches[:2])
        why.extend(result.risks[:2])
    if result.blockers:
        why.insert(0, result.blockers[0])
    why = why[:6]
    if decision == "APPLY":
        next_action = "apply now"
    elif decision == "MAYBE":
        next_action = "ask one clarifying question" if confidence != "low" else "paste the full description or ask for scope"
    else:
        next_action = "skip"
    return Evaluation(
        job_id=job.job_id, decision=decision, overall_score=overall, confidence=confidence, track=job.track,
        scores=scores, why=why, strong_matches=result.strong_matches or ["No strong evidence match detected from the supplied text."],
        gaps=result.gaps or ["No material evidence gap detected from the supplied text."],
        risks=result.risks or ["No specific risk detected; validate scope, team and process."],
        hard_blockers=result.blockers, actual_work_shape=result.work_shape,
        suggested_compensation=_compensation(job, result), proposal_positioning_angle=angle,
        cv_positioning=cv, career_capital={"level": capital_level, "why": capital_reason},
        next_action=next_action, source=job.source, metadata=job.metadata,
        created_at=datetime.now(timezone.utc).isoformat(),
        assessment_mode=active, role_summary=assessment.get("role_summary", ""),
        match=assessment.get("match", {}), working_style=assessment.get("working_style", {}),
        not_my_strengths=assessment.get("not_my_strengths", []),
        findings=assessment.get("findings", []), mitigations=assessment.get("mitigations", []),
        clarifying_questions=assessment.get("clarifying_questions", []),
        draft_message=assessment.get("draft_message", ""),
        model_opinion={
            "decision": assessment["model_decision"], "score": assessment["model_score"],
            "reasoning": assessment.get("model_reasoning", ""), "model": assessment.get("model", ""),
            "agrees": assessment["model_decision"] == decision,
        } if usable else {},
        deterministic_scores=result.scores if usable else {},
        assessment_note=assessment.get("advisor_error", ""),
        assessment_usage=assessment.get("usage", {}),
    )
