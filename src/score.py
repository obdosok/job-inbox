from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .extraction import is_model_extracted
from .models import Job
from .parse import importance, segment, us_only_requirement

# How loudly a mention counts. A nice-to-have must not weigh the same as a hard
# requirement, and a negated mention must not weigh at all. "stated" is 1.0 so a
# plainly-worded posting scores as it always did.
EMPHASIS = {"mandatory": 1.25, "stated": 1.0, "optional": 0.25, "negated": 0.0}

# Example compensation policy for the fictional demo candidate. These are
# illustrative defaults, not anyone's real numbers - set them to your own.
TRACK_B_HOURLY_FLOOR = 60        # USD per hour, Track B (bridge contracts)
TRACK_A_ANNUAL_FLOOR = 90000     # per year, Track A (stable role) core floor
TRACK_A_ANNUAL_TARGET = 130000   # per year, top of the Track A core range


# (terms, points, note when it is a strength, note when it is a gap). Points are
# scaled by how strongly the posting actually asks for the thing, so a
# nice-to-have cannot dominate a decision the way a hard requirement does.
EVIDENCE_SIGNALS: list[tuple[tuple[str, ...], float, str, str]] = [
    (("typescript", "javascript"), 1.0, "Deep production TypeScript/JavaScript evidence.", ""),
    (("vue", "nuxt"), 1.5, "Direct Vue/Nuxt production depth.", ""),
    (("complex frontend", "data-heavy", "reporting", "billing", "transaction", "application system"), 1.2,
     "Strong evidence in complex, data-heavy application and business flows.", ""),
    (("migration", "modernize", "legacy"), 0.8, "Live-migration experience provides a strong story.", ""),
    (("performance", "api", "graphql", "data flow"), 0.7, "Proven API/data-flow and performance work.", ""),
    (("react", "next.js", "nextjs"), 0.5, "Current production React/Next.js/TypeScript experience is relevant.",
     "React/Next.js production depth is recent rather than the deepest part of the record."),
    (("fastapi", "python", "postgres", "postgresql"), 0.35, "Recent production FastAPI/PostgreSQL experience transfers directly.", ""),
    (("distributed systems", "kafka", "microservices", "deep backend"), -1.1, "", "Deep backend/distributed-systems ownership is still growing."),
    (("shopify",), -2.0, "", "No supported Shopify public-app shipping evidence."),
    (("react native",), -1.8, "", "No supported React Native or dual-app-store shipping evidence."),
    # Deliberately narrow: building component libraries and design systems is
    # ordinary senior frontend work. Only craft-led scope counts against here.
    (("pixel-perfect", "pixel perfect", "brand identity", "full screen mockups"), -0.8, "",
     "Pixel-perfect/design-craft polish is not a comparative strength."),
]


def clamp(value: float) -> float:
    return round(max(0.0, min(10.0, value)), 1)


def emphasis(text: str, *terms: str) -> float:
    """Strongest emphasis any of ``terms`` carries in this posting."""
    found = (importance(text, term) for term in terms)
    return max((EMPHASIS[item.kind] for item in found if item), default=0.0)


def mandatory(text: str, *terms: str) -> bool:
    return any((item := importance(text, term)) and item.kind == "mandatory" for term in terms)


def contains(text: str, *patterns: str) -> bool:
    return any(pattern in text for pattern in patterns)


def contains_term(text: str, term: str) -> bool:
    if len(term) <= 3 and term.isalnum():
        return re.search(rf"\b{re.escape(term)}\b", text) is not None
    return term in text


def number(value: Any) -> float | None:
    """Metadata is hand-written JSON and adapter output, so a field can be
    missing, null, or a string. None of that may reach a comparison operator."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_number(value: Any, default: float = 0.0) -> float:
    parsed = number(value)
    return default if parsed is None else parsed


def rate(meta: dict[str, Any]) -> tuple[float | None, float | None]:
    """A published range. A posting that states only one end still counts."""
    low, high = number(meta.get("rate_min")), number(meta.get("rate_max"))
    if low is None and high is None:
        return (None, None)
    return (low if low is not None else high, high if high is not None else low)


@dataclass
class ScoreResult:
    scores: dict[str, float]
    strong_matches: list[str]
    gaps: list[str]
    risks: list[str]
    blockers: list[str]
    work_shape: list[str]
    career_items: list[str]


def _profile_text(job: Job) -> str:
    """Extracted prose that may paraphrase what the raw posting says.

    Activity descriptions are only usable when a model wrote them. The offline
    extractor emits fixed category labels such as "Audit, QA and detail
    verification", and reading those back as evidence would let the scorer
    confirm its own guess: a posting that merely mentions Vitest would be
    labelled QA work and then penalised for being QA work.
    """
    profile = job.profile or {}
    fragments = [profile.get("summary", "")]
    if is_model_extracted(profile):
        fragments.extend(item.get("description", "") for item in profile.get("activities", []))
    fragments.extend(item.get("requirement", "") for item in profile.get("requirements", []))
    fragments.extend(profile.get("scope_risks", []))
    return "\n".join(str(item) for item in fragments if item).casefold()


def hard_blockers(job: Job) -> list[str]:
    text = job.text.casefold()
    structured = _profile_text(job)
    meta = job.metadata
    result: list[str] = []
    us_clause = "" if meta.get("us_eligible", False) else us_only_requirement(job.text)
    if us_clause:
        result.append(f'Role requires US location; the candidate is based outside the US and no US eligibility is evidenced — "{us_clause}"')
    mandatory_requirements = " ".join(
        item.get("requirement", "") for item in (job.profile or {}).get("requirements", []) if item.get("importance") == "mandatory"
    ).casefold()
    if mandatory(job.text, "shopify public app") or "shopify public app" in mandatory_requirements:
        result.append("Mandatory shipped Shopify public-app evidence is absent.")
    mobile_requirement = (
        re.search(r"(?:at least|minimum|mandatory|required).*?\b2\+?\s+apps?.*?(?:both|ios.*android|android.*ios)", text, re.S)
        or (mandatory(job.text, "react native") and contains(text, "app-store release", "app store release", "publish to both app stores"))
        or ("react native" in mandatory_requirements and contains(mandatory_requirements, "app store", "play store", "published app"))
    )
    if mobile_requirement:
        result.append("Mandatory React Native/app-store publishing evidence is absent.")
    if meta.get("timezone_compatible") is False or contains(text, "strict pacific hours", "must work 9am-5pm pst", "fixed pst schedule"):
        result.append("The mandatory schedule/timezone is incompatible with the configured availability.")
    low, high = rate(meta)
    if job.track == "B" and high is not None and meta.get("rate_period", "hour") == "hour" and high < TRACK_B_HOURLY_FLOOR:
        result.append(f"Maximum rate ${high:g}/h is below the configured Track B floor of ${TRACK_B_HOURLY_FLOOR}/h.")
    annual_max = number(meta.get("annual_max"))
    if job.track == "A" and annual_max is not None and annual_max < TRACK_A_ANNUAL_FLOOR and meta.get("currency", "EUR") in {"EUR", "USD"}:
        result.append("Published annual maximum is below the configured Track A core floor.")
    return result


def responsibility_text(text: str) -> str:
    """The part of a posting that says what you will actually do.

    When a posting has a responsibilities section, decomposing the whole
    document instead lets a skills laundry list invent work that nobody asked
    for. Unstructured postings fall back to the full text unchanged.
    """
    sections = segment(text)
    duties = [item.body for item in sections if item.kind == "duties"]
    if duties:
        return "\n".join(duties)
    usable = [item.body for item in sections if item.kind != "noise"]
    return "\n".join(usable) if usable else text


def actual_work_shape(text: str, profile: dict[str, Any] | None = None) -> list[str]:
    activities = (profile or {}).get("activities", [])
    if activities and is_model_extracted(profile):
        ranked = sorted(activities, key=lambda item: item.get("estimated_share", 0), reverse=True)
        return [f"~{item['estimated_share']}% {item['description'].strip().lower()}" for item in ranked[:5] if item.get("estimated_share", 0) > 0]
    t = responsibility_text(text).casefold()
    buckets: list[tuple[str, int]] = []
    signals = [
        ("Product/system discovery, domain understanding and architecture", 18, ("product", "architecture", "domain", "workflow", "0→1", "0-to-1")),
        ("Hands-on frontend/application implementation", 22, ("frontend", "react", "vue", "typescript", "next.js", "nuxt")),
        ("Backend, API and data implementation", 18, ("backend", "api", "postgres", "sql", "graphql", "fastapi", "node")),
        ("Audit, QA, UX inspection and detail verification", 28, ("audit", "qa", "quality assurance", "ux review", "pixel-perfect", "test every", "inspect every")),
        ("Mobile implementation and app-store delivery", 25, ("react native", "mobile app", "app store", "play store")),
        ("Design-system, mockup and visual/brand work", 22, ("design system", "mockup", "brand identity", "figma", "visual design")),
        ("Maintenance, operations and production support", 17, ("maintenance", "on-call", "operations", "runbook", "support rotation")),
        ("Meetings, customer interaction and coordination", 16, ("customer-facing", "client calls", "stakeholder meetings", "daily meetings", "sales")),
    ]
    for label, weight, words in signals:
        hits = sum(word in t for word in words)
        if hits:
            buckets.append((label, weight + min(12, (hits - 1) * 4)))
    if not buckets:
        return ["Insufficient detail to decompose responsibilities; clarify the first 30-60 days and recurring weekly work."]
    total = sum(weight for _, weight in buckets)
    raw_shares = [(label, weight * 100 / total) for label, weight in buckets]
    shares = [(label, int(share)) for label, share in raw_shares]
    remainder = 100 - sum(share for _, share in shares)
    order = sorted(range(len(raw_shares)), key=lambda i: raw_shares[i][1] % 1, reverse=True)
    for index in order[:remainder]:
        label, share = shares[index]
        shares[index] = (label, share + 1)
    ranked = sorted(shares, key=lambda item: item[1], reverse=True)
    return [f"~{share}% {label.lower()}" for label, share in ranked[:5]]


def score_job(job: Job) -> ScoreResult:
    t = job.text.casefold()
    semantic = _profile_text(job)
    scoring_text = f"{t}\n{semantic}"
    meta = job.metadata
    strong: list[str] = []
    gaps: list[str] = []
    risks: list[str] = []
    blockers = hard_blockers(job)

    def strength(*terms: str) -> float:
        """Emphasis from the posting itself, falling back to the extracted profile.

        A term the posting negates scores 0.0 and must not be revived by the
        semantic summary, so the fallback only applies when the raw text never
        mentions the term at all.
        """
        value = emphasis(job.text, *terms)
        if value or any(term in t for term in terms):
            return value
        return EMPHASIS["stated"] if any(term in semantic for term in terms) else 0.0

    evidence = 4.2
    for terms, points, match_note, gap_note in EVIDENCE_SIGNALS:
        factor = strength(*terms)
        if not factor:
            continue
        evidence += points * factor
        if match_note:
            strong.append(match_note)
        if gap_note:
            gaps.append(gap_note)

    work = 5.0
    work += 1.0 * strength("product", "ownership", "end-to-end", "end to end", "architecture", "0→1", "0-to-1")
    work += 1.2 * strength("existing system", "legacy", "migration", "improve", "iterate", "experiment", "autonomy", "flexible hours")
    # "pixel-perfect" belongs to design craft below, not here: attention to
    # visual detail is not the same responsibility as exhaustive manual QA.
    audit_load = strength("audit", "qa", "quality assurance", "inspect every", "test every")
    if audit_load:
        work -= 1.8 * audit_load
        risks.append("A material share of the real work appears to be audit/QA/detail inspection.")
        gaps.append("Exhaustive manual QA and detail inspection are not a comparative strength.")
    # Bare "design system" is ordinary frontend work and is deliberately absent.
    design_load = strength("full screen mockups", "brand identity", "visual design", "pixel-perfect", "pixel perfect")
    if design_load:
        work -= 1.4 * design_load
        risks.append("The scope includes design/visual craft outside the candidate's strongest leverage.")
    meeting_load = strength("daily meetings", "meeting-heavy", "strict schedule", "fixed schedule")
    if meeting_load:
        work -= 1.2 * meeting_load
        risks.append("Schedule or meeting structure may fragment autonomous focus.")
    maintenance_load = strength("maintenance", "routine support", "ticket queue")
    if maintenance_load:
        work -= 0.8 * maintenance_load
        risks.append("Routine maintenance/ticket throughput may dominate contextual problem solving.")
    if "30-week" in t or "30 week" in t or _as_number(meta.get("duration_weeks")) >= 20:
        work -= 0.7
        risks.append("Long full-time commitment raises the cost of a scope mismatch.")
    # Only responsibilities the posting actually asks for widen the role. A
    # nice-to-have or a negated mention must not inflate perceived breadth.
    breadth = sum(strength(word) >= 1.0 for word in (
        "frontend", "backend", "architecture", "design system", "mockups", "brand identity",
        "react native", "qa", "security review", "analytics",
    ))
    if breadth >= 7:
        work -= 2.0
        risks.append("This is an all-in-one product-team scope with excessive role breadth for one person.")
    if is_model_extracted(job.profile):
        shares = {item.get("category"): item.get("estimated_share", 0) for item in job.profile.get("activities", [])}
        positive = shares.get("product_system", 0) + shares.get("implementation", 0) * 0.35 + shares.get("backend_data", 0) * 0.25
        negative = shares.get("audit_qa", 0) + shares.get("design_craft", 0) + shares.get("maintenance_ops", 0) * 0.7 + shares.get("meetings_coordination", 0) * 0.8 + shares.get("customer_sales", 0)
        semantic_work = 5.0 + positive / 35 - negative / 28
        work = (work * 0.35) + (semantic_work * 0.65)
        if shares.get("audit_qa", 0) >= 30:
            risks.append("Structured extraction indicates audit/QA dominates the actual work.")
        if shares.get("design_craft", 0) >= 25:
            risks.append("Structured extraction indicates design craft is a major responsibility.")

    economics = 5.0
    low, high = rate(meta)
    if job.track == "B":
        if high is not None:
            midpoint = (low + high) / 2
            economics += 1.5 if midpoint >= 100 else 0.7 if midpoint >= 75 else -0.4 if midpoint < TRACK_B_HOURLY_FLOOR else 0
        freshness = number(meta.get("freshness_minutes"))
        if freshness is not None:
            economics += 1.5 if freshness <= 60 else 0.9 if freshness <= 1440 else 0.2 if freshness <= 4320 else -1.0
        proposals = number(meta.get("proposals"))
        interviewing = number(meta.get("interviewing"))
        if proposals is not None and proposals >= 50:
            economics -= 1.2
            risks.append("50+ proposals materially reduce read probability.")
        if interviewing is not None and interviewing >= 10:
            economics -= 1.5
            risks.append(f"The client is already interviewing {interviewing:g}, indicating a mature shortlist.")
        elif interviewing == 0:
            economics += 0.4
        if _as_number(meta.get("connect_cost")) >= 16:
            economics -= 0.5
            risks.append("High Connect cost increases waste if read probability is low.")
    else:
        if meta.get("remote") is True or contains(t, "remote", "flexible hours"):
            economics += 0.8
        if meta.get("eu_compatible") is True:
            economics += 0.6
        annual_max = number(meta.get("annual_max"))
        if annual_max is not None:
            economics += 1.0 if annual_max >= TRACK_A_ANNUAL_TARGET else 0.4 if annual_max >= TRACK_A_ANNUAL_FLOOR else -1.2
        if contains(t, "55 hours", "60 hours", "mandatory overtime"):
            economics -= 1.3; risks.append("Expected hours impose a high energy and family-time cost.")

    win = 0.55 * evidence + 1.6
    if _as_number(meta.get("proposals")) >= 50:
        win -= 1.0
    if _as_number(meta.get("interviewing")) >= 10:
        win -= 1.2
    if contains(t, "must have", "mandatory", "required") and gaps:
        win -= 0.7
    if contains(t, "frontend", "typescript", "application"):
        win += 0.5
    if blockers:
        win = min(win, 2.5)

    career = 4.0
    career_items: list[str] = []
    capital_signals = [
        (("react", "next.js"), "React/Next production evidence"),
        (("node", "backend"), "backend/Node ownership"),
        (("postgres", "sql", "data modeling"), "PostgreSQL/data depth"),
        (("transaction", "concurrency", "idempotency"), "transactions/concurrency evidence"),
        (("queue", "event-driven", "kafka"), "queues/events experience"),
        (("aws", "azure", "gcp", "cloud"), "cloud production evidence"),
        (("ai", "llm", "machine learning"), "production AI credibility"),
        (("product ownership", "end-to-end ownership", "0→1"), "product ownership"),
        (("health", "medical"), "digital-health credibility"),
        (("audit", "consulting"), "consulting/audit credibility"),
    ]
    for words, label in capital_signals:
        if any(contains_term(t, word) for word in words):
            career += 0.65; career_items.append(label)

    scores = {
        "capability_fit": clamp(evidence),
        "working_style_fit": clamp(work),
        "conditions_fit": clamp(economics),
        "win_probability": clamp(win),
        "career_capital": clamp(career),
    }
    return ScoreResult(scores, list(dict.fromkeys(strong)), list(dict.fromkeys(gaps)), list(dict.fromkeys(risks)), blockers, actual_work_shape(job.text, job.profile), career_items)
