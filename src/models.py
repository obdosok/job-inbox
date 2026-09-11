from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Job:
    job_id: str
    text: str
    track: str
    source: str = "manual"
    metadata: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class Evaluation:
    job_id: str
    decision: str
    overall_score: float
    confidence: str
    track: str
    scores: dict[str, float]
    why: list[str]
    strong_matches: list[str]
    gaps: list[str]
    risks: list[str]
    hard_blockers: list[str]
    actual_work_shape: list[str]
    suggested_compensation: str
    proposal_positioning_angle: list[str]
    cv_positioning: str
    career_capital: dict[str, str]
    next_action: str
    source: str
    metadata: dict[str, Any]
    created_at: str
    # Present only when the semantic advisor ran. Older stored evaluations
    # simply lack them, so every consumer must treat them as optional.
    assessment_mode: str = "off"
    role_summary: str = ""
    match: dict[str, str] = field(default_factory=dict)
    working_style: dict[str, Any] = field(default_factory=dict)
    not_my_strengths: list[str] = field(default_factory=list)
    findings: list[dict[str, str]] = field(default_factory=list)
    mitigations: list[dict[str, str]] = field(default_factory=list)
    clarifying_questions: list[str] = field(default_factory=list)
    draft_message: str = ""
    model_opinion: dict[str, Any] = field(default_factory=dict)
    deterministic_scores: dict[str, float] = field(default_factory=dict)
    assessment_note: str = ""
    # Token counts for the assessment call. Kept so cache behaviour is visible in
    # the app itself: a cached_input_tokens of zero across pastes means the
    # profile is being re-billed every time.
    assessment_usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
