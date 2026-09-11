from __future__ import annotations

from .models import Evaluation


def _section(title: str, items: list[str]) -> list[str]:
    return [f"{title}:", *[f"- {item}" for item in items]]


def decision_card(e: Evaluation) -> str:
    lines = [
        f"DECISION: {e.decision}",
        f"OVERALL SCORE: {e.overall_score}/10",
        f"CONFIDENCE: {e.confidence}",
        "",
        "DIMENSIONS (indicators): " + " | ".join(
            f"{name.replace('_', ' ').title()} {value}/10" for name, value in e.scores.items()),
        "",
    ]
    if e.match:
        lines += [
            f"MATCH: {e.match['verdict']}",
            f"- for you: {e.match['for_candidate']}",
            f"- for them: {e.match['for_employer']}",
            f"- decisive: {e.match['decisive_factor']}",
            "",
        ]
    if e.working_style:
        lines += [f"HOW THIS ROLE RUNS: {e.working_style['how_this_role_runs']}"]
        lines += _section("SUITS YOU", e.working_style["suits_him"])
        lines += _section("DRAINS YOU", e.working_style["drains_him"]) + [""]
    if e.not_my_strengths:
        lines += _section("NOT YOUR STRENGTHS HERE", e.not_my_strengths) + [""]
    lines += _section("WHY", e.why) + [""]
    lines += _section("STRONG MATCHES", e.strong_matches) + [""]
    lines += _section("GAPS", e.gaps) + [""]
    lines += _section("RISKS", e.risks) + [""]
    lines += _section("HARD BLOCKERS", e.hard_blockers or ["None detected."]) + [""]
    lines += _section("ACTUAL WORK SHAPE", e.actual_work_shape) + [""]
    lines += [f"TRACK: {e.track}", "", "SUGGESTED COMPENSATION:", f"- {e.suggested_compensation}", ""]
    lines += _section("PROPOSAL / POSITIONING ANGLE", e.proposal_positioning_angle) + [""]
    lines += [f"CV POSITIONING: {e.cv_positioning}", "", f"CAREER CAPITAL: {e.career_capital['level']}", f"- {e.career_capital['why']}", ""]
    if e.findings:
        lines += ["FINDINGS:"]
        for item in e.findings:
            lines += [
                f"- [{item['verdict']}] {item['dimension']}: {item['claim']}",
                f"    posting: \"{item['job_evidence']}\"",
                f"    profile: \"{item['profile_evidence']}\"",
            ]
        lines += [""]
    if e.mitigations:
        lines += _section("HOW TO ADDRESS THE CONCERNS", [f"{item['concern']} -> {item['how_to_address']}" for item in e.mitigations]) + [""]
    if e.clarifying_questions:
        lines += _section("WORTH ASKING", e.clarifying_questions) + [""]
    if e.model_opinion and not e.model_opinion.get("agrees", True):
        opinion = e.model_opinion
        lines += [f"SECOND OPINION: the advisor would say {opinion['decision']} ({opinion['score']}/10), the rules say {e.decision}.", f"- {opinion['reasoning']}", ""]
    if e.assessment_note:
        lines += [f"ADVISOR UNAVAILABLE: {e.assessment_note}", "- Scored deterministically instead.", ""]
    if e.draft_message:
        lines += ["DRAFT MESSAGE (review before sending anywhere):", "", e.draft_message, ""]
    lines += [f"NEXT ACTION: {e.next_action}"]
    return "\n".join(lines)
