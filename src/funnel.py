"""Who gets a paid assessment, and who does not.

A board hands over hundreds of postings a day and most are irrelevant — the
first result from a remote aggregator was "Business Development Manager". At
roughly ten cents an assessment, sending the whole feed to the advisor costs
tens of dollars a day for an answer the title already gave away.

So the funnel narrows in three stages, cheapest first:

    fetched  -> title filter (free, no parsing)
             -> deterministic score (free, `src.score`)
             -> advisor (paid, capped)

The deterministic layer is not a fallback here. It is the gatekeeper that
decides what is worth paying for.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any


# Titles worth reading at all. Deliberately generous: this stage only removes
# what is obviously not software engineering, and the scorer does the judging.
DEFAULT_INCLUDE = (
    r"front[- ]?end|full[- ]?stack|back[- ]?end|software|web|developer|engineer|"
    r"react|vue|typescript|javascript|node|python|architect|tech lead"
)
# Removed outright: different professions that share vocabulary with ours.
DEFAULT_EXCLUDE = (
    r"\b(sales|account (?:executive|manager)|recruit\w*|marketing|business development|"
    r"customer success|support (?:agent|specialist)|hr\b|people partner|accountant|"
    r"controller|buchhalt\w*|designer|copywriter|content|intern|internship|praktyk\w*|"
    # Any flavour of manager: the operating manual names people management a weak
    # fit, and "Engineering Manager" was outscoring every hands-on role.
    r"student|junior|trainee|managers?|director|vp of|head of)\b"
)


def duplicate_key(company: str, title: str) -> str:
    """Identity of a posting across its copies.

    Boards repost one job once per region: a single remote Inwedo opening
    appeared eleven times, identical but for a voivodeship in the id. Left
    alone, a five-assessment budget buys five copies of the same job.
    """
    return "|".join(re.sub(r"\s+", " ", part or "").strip().casefold() for part in (company, title))


@dataclass
class FunnelReport:
    fetched: int = 0
    title_rejected: int = 0
    duplicates: int = 0
    scored: int = 0
    assessed: int = 0
    skipped_by_gate: int = 0
    rejected_titles: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"fetched {self.fetched} | filtered out by title {self.title_rejected} | "
                f"duplicates {self.duplicates} | scored {self.scored} | "
                f"assessed {self.assessed} | below the gate {self.skipped_by_gate}")


@dataclass
class FunnelPolicy:
    include: re.Pattern
    exclude: re.Pattern
    advise_max: int
    advise_min_score: float

    @classmethod
    def from_env(cls, overrides: dict[str, Any] | None = None) -> "FunnelPolicy":
        overrides = overrides or {}
        # A caller may cap spend below the configured ceiling but never above it,
        # so a request body cannot turn one click into an unbounded bill.
        ceiling = max(0, int(os.environ.get("JOB_INBOX_ADVISE_MAX", "5")))
        requested = overrides.get("advise_max")
        return cls(
            include=re.compile(os.environ.get("JOB_INBOX_TITLE_INCLUDE", DEFAULT_INCLUDE), re.I),
            exclude=re.compile(os.environ.get("JOB_INBOX_TITLE_EXCLUDE", DEFAULT_EXCLUDE), re.I),
            advise_max=min(ceiling, max(0, int(requested))) if requested is not None else ceiling,
            advise_min_score=float(os.environ.get("JOB_INBOX_ADVISE_MIN_SCORE", "4.5")),
        )

    def wants_title(self, title: str) -> bool:
        title = title or ""
        if self.exclude.search(title):
            return False
        return bool(self.include.search(title))


def shortlist(scored: list[tuple[Any, float]], policy: FunnelPolicy) -> tuple[list[Any], int]:
    """The items worth paying to assess, best deterministic score first.

    Returns the shortlist and how many cleared the title filter but not the gate,
    so a run can report what it chose not to spend on.
    """
    eligible = [item for item, score in scored if score >= policy.advise_min_score]
    ranked = [item for item, _ in sorted(scored, key=lambda pair: pair[1], reverse=True) if item in eligible]
    chosen = ranked[: policy.advise_max]
    return chosen, len(scored) - len(chosen)
