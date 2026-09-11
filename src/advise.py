"""Semantic assessment of one posting against the candidate profile.

The deterministic scorer in :mod:`src.score` reads words. It cannot weigh a
proportion ("one testing bullet among twenty is not a testing-heavy role") and
it cannot read 40k characters of operating-manual prose. That is what this
module is for.

Two things keep it honest. Every finding must quote both the posting and the
profile, so you can check it by eye. And the model's own verdict is never the
only answer available: it returns per-dimension scores that the existing
weights and thresholds in :mod:`src.evaluate` can turn into a decision, so you
can run the model as an input to the rules or as a second opinion beside them.

Hard blockers stay deterministic in every mode. A rate below the configured
floor is arithmetic, not judgement.
"""

from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path
from typing import Any, Protocol

from . import candidate, providers


ROOT = Path(__file__).resolve().parents[1]
# A match has two sides. `employer_benefit` exists so a role the candidate would
# enjoy but be the wrong person for cannot score as a good match, and
# `working_style_fit` is separated from capability because a mismatch there
# usually costs more than a missing skill.
DIMENSIONS = ("capability_fit", "working_style_fit", "conditions_fit",
              "employer_benefit", "win_probability", "career_capital")
MODES = ("off", "findings", "verdict")
MATCH_VERDICTS = ("strong", "workable", "poor")

ASSESSMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "role_summary": {"type": "string"},
        "dimension_scores": {
            "type": "object", "additionalProperties": False,
            "properties": {name: {"type": "integer", "minimum": 0, "maximum": 10} for name in DIMENSIONS},
            "required": list(DIMENSIONS),
        },
        "match": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": list(MATCH_VERDICTS)},
                "for_candidate": {"type": "string"},
                "for_employer": {"type": "string"},
                "decisive_factor": {"type": "string"},
            },
            "required": ["verdict", "for_candidate", "for_employer", "decisive_factor"],
        },
        "working_style": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "how_this_role_runs": {"type": "string"},
                "suits_him": {"type": "array", "items": {"type": "string"}},
                "drains_him": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["how_this_role_runs", "suits_him", "drains_him"],
        },
        "not_my_strengths": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "dimension": {"type": "string", "enum": list(DIMENSIONS)},
                    "verdict": {"type": "string", "enum": ["strength", "concern", "blocker"]},
                    "claim": {"type": "string"},
                    "job_evidence": {"type": "string"},
                    "profile_evidence": {"type": "string"},
                },
                "required": ["dimension", "verdict", "claim", "job_evidence", "profile_evidence"],
            },
        },
        "mitigations": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"concern": {"type": "string"}, "how_to_address": {"type": "string"}},
                "required": ["concern", "how_to_address"],
            },
        },
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
        "model_decision": {"type": "string", "enum": ["APPLY", "MAYBE", "SKIP"]},
        "model_score": {"type": "integer", "minimum": 0, "maximum": 10},
        "model_reasoning": {"type": "string"},
        "draft_message": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": [
        "role_summary", "match", "working_style", "not_my_strengths", "dimension_scores",
        "findings", "mitigations", "clarifying_questions",
        "model_decision", "model_score", "model_reasoning", "draft_message", "confidence",
    ],
}

# A match verdict is about the pairing; the decision is about what to do next.
MATCH_TO_DECISION = {"strong": "APPLY", "workable": "MAYBE", "poor": "SKIP"}


class JobAdvisor(Protocol):
    def assess(self, description: str) -> dict[str, Any]: ...


def _score(value: Any, field: str) -> int:
    """A 0-10 score, coerced rather than rejected.

    Anthropic's structured outputs drop ``minimum``/``maximum`` from the wire
    schema, so the range is a prompt instruction there and nothing enforces it.
    Discarding an otherwise good assessment over an 11 or a 7.5 would be the
    wrong trade, so round and clamp the way every other score here is clamped.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number from 0 to 10, got {value!r}")
    return max(0, min(10, round(value)))


def validate_assessment(data: dict[str, Any]) -> dict[str, Any]:
    """Re-check the model's output. A strict schema is a request, not a promise."""
    missing = set(ASSESSMENT_SCHEMA["required"]) - set(data)
    if missing:
        raise ValueError(f"Assessment is missing: {sorted(missing)}")
    scores = data["dimension_scores"]
    if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
        raise ValueError(f"Assessment must score exactly these {len(DIMENSIONS)} dimensions: {', '.join(DIMENSIONS)}")
    for name, value in scores.items():
        scores[name] = _score(value, f"Dimension {name}")
    match = data["match"]
    if not isinstance(match, dict) or set(match) != {"verdict", "for_candidate", "for_employer", "decisive_factor"}:
        raise ValueError("Assessment must describe the match for both sides")
    if match["verdict"] not in MATCH_VERDICTS:
        raise ValueError(f"Unknown match verdict: {match['verdict']!r}")
    if not match["for_employer"].strip():
        raise ValueError("The employer's side of the match must be stated, not left blank")
    style = data["working_style"]
    if not isinstance(style, dict) or set(style) != {"how_this_role_runs", "suits_him", "drains_him"}:
        raise ValueError("Working style must match the assessment schema")
    if not isinstance(data["not_my_strengths"], list):
        raise ValueError("not_my_strengths must be a list")
    if not isinstance(data["findings"], list) or not data["findings"]:
        raise ValueError("Assessment must contain at least one finding")
    for item in data["findings"]:
        if not isinstance(item, dict) or set(item) != {"dimension", "verdict", "claim", "job_evidence", "profile_evidence"}:
            raise ValueError("Each finding must match the assessment schema")
        if item["dimension"] not in DIMENSIONS:
            raise ValueError("Finding names an unknown dimension")
        if item["verdict"] not in {"strength", "concern", "blocker"}:
            raise ValueError("Finding has an unknown verdict")
        if not item["job_evidence"].strip() or not item["profile_evidence"].strip():
            raise ValueError("Every finding must quote both the posting and the profile")
    for item in data["mitigations"]:
        if not isinstance(item, dict) or set(item) != {"concern", "how_to_address"}:
            raise ValueError("Each mitigation must match the assessment schema")
    if data["model_decision"] not in {"APPLY", "MAYBE", "SKIP"}:
        raise ValueError("Unknown model decision")
    data["model_score"] = _score(data["model_score"], "Model score")
    if data["confidence"] not in {"low", "medium", "high"}:
        raise ValueError("Unknown assessment confidence")
    return data


class ModelAdvisor:
    def __init__(self, provider: providers.Provider):
        self.provider = provider
        self.prompt = (ROOT / "prompts" / "assess_job.md").read_text(encoding="utf-8")

    def _instructions(self) -> str:
        profile = candidate.load()
        if not profile:
            raise ValueError("Candidate profile is empty; run `python -m src.cli profile refresh`")
        # The profile is byte-identical on every call and dwarfs the posting, so
        # it leads the instructions where a provider's prompt cache can reuse it.
        return f"{self.prompt}\n\n# Candidate profile\n\n{profile}"

    def assess(self, description: str) -> dict[str, Any]:
        data, usage = self.provider.complete_json(
            self._instructions(), f"# Job posting\n\n{description}", ASSESSMENT_SCHEMA, "job_assessment")
        assessment = validate_assessment(data)
        assessment["advisor"] = self.provider.name
        assessment["model"] = self.provider.model
        # Kept so the cost of an assessment is visible rather than guessed; the
        # profile dominates the input and should be cache-hitting.
        assessment["usage"] = usage
        return assessment


def mode() -> str:
    """``off``, ``findings`` (rules decide) or ``verdict`` (the model decides)."""
    configured = os.environ.get("JOB_INBOX_ADVISOR", "").strip().lower()
    if configured in MODES:
        return configured
    if not providers.has_key() or not candidate.is_available():
        return "off"
    return "findings"


def is_active() -> bool:
    """Whether an assessment will actually run, not merely what is configured.

    ``mode()`` reports intent so the evaluator knows who is authoritative when
    an assessment is handed to it. This reports capability, so the interface
    does not promise advice that no API key can deliver.
    """
    return mode() != "off" and providers.has_key() and candidate.is_available()


def get_default_advisor() -> JobAdvisor | None:
    if not is_active():
        return None
    provider = providers.build(os.environ.get("JOB_INBOX_ADVISOR_MODEL", ""), timeout=90)
    return ModelAdvisor(provider) if provider else None


def assess(description: str, advisor: JobAdvisor | None = None) -> dict[str, Any] | None:
    """Assess a posting, or return ``None`` so the caller stays deterministic.

    An advisor failure must never lose a job. The reason is recorded on the
    evaluation so a silent fallback is visible rather than mysterious.
    """
    advisor = advisor if advisor is not None else get_default_advisor()
    if advisor is None:
        return None
    try:
        return advisor.assess(description)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as error:
        return {"advisor_error": f"{type(error).__name__}: {error}"}
