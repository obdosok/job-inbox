from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path
from typing import Any, Protocol

from . import providers
from .parse import MANDATORY_MARKER, OPTIONAL_MARKER, clauses, segment


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_LINE = re.compile(r"\b(must|required|requirement|expert-level|experience|proficien\w+|\d+\+?\s*years)\b", re.I)
CATEGORIES = (
    "product_system", "implementation", "backend_data", "audit_qa", "design_craft",
    "mobile", "maintenance_ops", "meetings_coordination", "customer_sales", "other",
)

JOB_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "role_title": {"type": "string"},
        "company": {"type": "string"},
        "summary": {"type": "string"},
        "activities": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "description": {"type": "string"},
                    "estimated_share": {"type": "integer", "minimum": 0, "maximum": 100},
                    "evidence": {"type": "string"},
                },
                "required": ["category", "description", "estimated_share", "evidence"],
            },
        },
        "requirements": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "requirement": {"type": "string"},
                    "importance": {"type": "string", "enum": ["mandatory", "preferred", "unclear"]},
                    "evidence": {"type": "string"},
                },
                "required": ["requirement", "importance", "evidence"],
            },
        },
        "work_conditions": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "schedule": {"type": "string"}, "hours": {"type": "string"},
                "timezone": {"type": "string"}, "remote_scope": {"type": "string"},
            },
            "required": ["schedule", "hours", "timezone", "remote_scope"],
        },
        "scope_risks": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["role_title", "company", "summary", "activities", "requirements", "work_conditions", "scope_risks", "unknowns", "confidence"],
}


MODEL_EXTRACTORS = {"openai", "anthropic"}


class JobProfileExtractor(Protocol):
    def extract(self, description: str) -> dict[str, Any]: ...


def is_model_extracted(profile: dict[str, Any] | None) -> bool:
    """Whether a model wrote this profile's prose, or the offline extractor did.

    Activity descriptions may only be read back as evidence when a model wrote
    them; the heuristic extractor emits fixed category labels, and treating
    those as evidence lets the scorer confirm its own guess.
    """
    return (profile or {}).get("extractor") in MODEL_EXTRACTORS


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    required = set(JOB_PROFILE_SCHEMA["required"])
    missing = required - set(profile)
    if missing:
        raise ValueError(f"Structured job profile is missing: {sorted(missing)}")
    activities = profile.get("activities")
    if not isinstance(activities, list) or not activities:
        raise ValueError("Structured job profile must contain activities")
    for item in activities:
        if not isinstance(item, dict) or set(item) != {"category", "description", "estimated_share", "evidence"}:
            raise ValueError("Each activity must match the structured schema")
        if item["category"] not in CATEGORIES:
            raise ValueError("Structured job profile contains an unknown activity category")
        if not isinstance(item["estimated_share"], int) or not 0 <= item["estimated_share"] <= 100:
            raise ValueError("Activity share must be an integer from 0 to 100")
        if not isinstance(item["description"], str) or not isinstance(item["evidence"], str):
            raise ValueError("Activity text fields must be strings")
    total = sum(item["estimated_share"] for item in activities)
    if total != 100:
        raise ValueError(f"Activity shares must total 100, got {total}")
    requirements = profile.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError("Requirements must be a list")
    for item in requirements:
        if not isinstance(item, dict) or set(item) != {"requirement", "importance", "evidence"}:
            raise ValueError("Each requirement must match the structured schema")
        if item["importance"] not in {"mandatory", "preferred", "unclear"}:
            raise ValueError("Unknown requirement importance")
    conditions = profile.get("work_conditions")
    if not isinstance(conditions, dict) or set(conditions) != {"schedule", "hours", "timezone", "remote_scope"}:
        raise ValueError("Work conditions must match the structured schema")
    if profile.get("confidence") not in {"low", "medium", "high"}:
        raise ValueError("Unknown extraction confidence")
    if not isinstance(profile.get("scope_risks"), list) or not isinstance(profile.get("unknowns"), list):
        raise ValueError("Scope risks and unknowns must be lists")
    return profile


class HeuristicExtractor:
    patterns = [
        ("product_system", "Product/system discovery and architecture", ("product", "architecture", "domain", "workflow", "ownership")),
        ("implementation", "Hands-on frontend/application implementation", ("frontend", "react", "vue", "typescript", "implementation")),
        ("backend_data", "Backend, API and data implementation", ("backend", "api", "postgres", "sql", "graphql", "node")),
        ("audit_qa", "Audit, QA and detail verification", ("audit", "qa", "quality assurance", "pixel-perfect", "testing")),
        ("design_craft", "Design-system and visual craft", ("design system", "mockup", "brand identity", "figma", "visual detail")),
        ("mobile", "Mobile and app-store delivery", ("react native", "mobile app", "app store", "expo")),
        ("maintenance_ops", "Maintenance and operations", ("maintenance", "on-call", "operations", "support rotation")),
        ("meetings_coordination", "Meetings and coordination", ("stakeholder meetings", "daily meetings", "coordination", "roadmap presentation")),
        ("customer_sales", "Customer-facing or sales work", ("customer-facing", "sales", "discovery calls", "client calls")),
    ]

    @staticmethod
    def _requirements(description: str) -> list[dict[str, str]]:
        """Read each requirement's importance instead of assuming it is mandatory.

        Every requirement used to be labelled "mandatory", which made the field
        useless downstream: a nice-to-have and a hard requirement looked the same.
        """
        found: list[dict[str, str]] = []
        for section in segment(description):
            if section.kind == "noise":
                continue
            for clause in clauses(section.body):
                if not REQUIREMENT_LINE.search(clause) and section.kind not in {"mandatory", "optional"}:
                    continue
                if OPTIONAL_MARKER.search(clause) or section.kind == "optional":
                    weight = "preferred"
                elif MANDATORY_MARKER.search(clause) or section.kind == "mandatory":
                    weight = "mandatory"
                else:
                    weight = "unclear"
                found.append({"requirement": clause[:300], "importance": weight, "evidence": clause[:180]})
        return found

    def extract(self, description: str) -> dict[str, Any]:
        text = description.casefold()
        weighted: list[tuple[str, str, int, str]] = []
        for category, label, words in self.patterns:
            hits = [word for word in words if word in text]
            if hits:
                weighted.append((category, label, 15 + 5 * len(hits), ", ".join(hits[:3])))
        if not weighted:
            weighted = [("other", "Unclear work shape", 100, "Description lacks concrete activity signals")]
        total = sum(item[2] for item in weighted)
        shares = [int(item[2] * 100 / total) for item in weighted]
        for index in range(100 - sum(shares)):
            shares[index % len(shares)] += 1
        first_line = next((line.strip() for line in description.splitlines() if line.strip()), "Untitled job")
        requirements = self._requirements(description)
        return {
            "role_title": first_line[:160], "company": "", "summary": first_line[:300],
            "activities": [
                {"category": item[0], "description": item[1], "estimated_share": shares[i], "evidence": item[3]}
                for i, item in enumerate(weighted)
            ],
            "requirements": requirements[:20],
            "work_conditions": {"schedule": "", "hours": "", "timezone": "", "remote_scope": ""},
            "scope_risks": [], "unknowns": ["Heuristic extraction cannot infer unstated work allocation."],
            "confidence": "low", "extractor": "heuristic",
        }


class ModelExtractor:
    """Semantic extraction through whichever provider is configured."""

    def __init__(self, provider: providers.Provider):
        self.provider = provider
        self.prompt = (ROOT / "prompts" / "extract_job.md").read_text(encoding="utf-8")

    def extract(self, description: str) -> dict[str, Any]:
        data, usage = self.provider.complete_json(self.prompt, description, JOB_PROFILE_SCHEMA, "job_profile")
        profile = validate_profile(data)
        profile["extractor"] = self.provider.name
        profile["model"] = self.provider.model
        profile["usage"] = usage
        return profile


class FallbackExtractor:
    def __init__(self, primary: JobProfileExtractor | None, fallback: JobProfileExtractor):
        self.primary = primary
        self.fallback = fallback

    def extract(self, description: str) -> dict[str, Any]:
        if self.primary:
            try:
                return self.primary.extract(description)
            except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as error:
                profile = self.fallback.extract(description)
                profile["fallback_reason"] = f"{type(error).__name__}: {error}"
                return profile
        return self.fallback.extract(description)


def get_default_extractor() -> JobProfileExtractor:
    provider = providers.build(timeout=45)
    return FallbackExtractor(ModelExtractor(provider) if provider else None, HeuristicExtractor())
