"""Structure-aware reading of a job posting.

The scorer used to ask ``term in text``. That cannot tell a hard requirement
from a nice-to-have, cannot see a negation, and gets louder the longer the
posting is. This module resolves a term to a single importance weight by
looking at *where* and *how* it is mentioned.

Nothing here is candidate-specific and nothing calls a model. The same
weights feed the offline extractor and the scorer, so the deterministic and
LLM paths agree on what "mandatory" means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


MANDATORY = 1.0
STATED = 0.6
OPTIONAL = 0.15
NEGATED = 0.0

OPTIONAL_MARKER = re.compile(
    r"\b(nice[- ]to[- ]have|(?:a|strong|big|huge) plus|bonus|preferred|desirable|"
    r"would be (?:great|nice|a plus)|familiarity with|ideally|advantage|helpful|"
    r"not required|optional|willing to learn)\b",
    re.I,
)
MANDATORY_MARKER = re.compile(
    r"\b(must have|must be|must know|we require|required|requirements?|expert-level|"
    r"mandatory|essential|proven track record|\d+\+?\s*years)\b",
    re.I,
)
NEGATION = re.compile(r"\b(no|not|never|without|don'?t|doesn'?t|won'?t|isn'?t|aren'?t|nor)\b", re.I)

_MANDATORY_HEADER = re.compile(r"^(requirements?|required skills?|must[- ]haves?|qualifications|who you are|you (?:must|should) have)\b", re.I)
_OPTIONAL_HEADER = re.compile(r"^(nice[- ]to[- ]haves?|bonus(?: points)?|preferred|pluses|good to have|optional|extra credit)\b", re.I)
_DUTIES_HEADER = re.compile(r"^(what you'?ll do|what you will do|responsibilities|the role|your role|day[- ]to[- ]day|about the (?:role|job|position)|project expectations)\b", re.I)
_NOISE_HEADER = re.compile(r"^(about (?:us|the company|the team)|benefits?|perks?|why (?:join|work)|compensation|salary|(?:how )?to apply|equal opportunity|our values|please (?:include|also))\b", re.I)

_SECTION_WEIGHT = {"mandatory": MANDATORY, "duties": STATED, "optional": OPTIONAL, "skills": STATED, "intro": STATED, "noise": NEGATED}

_STOPWORDS = {"and", "or", "the", "a", "an", "of", "to", "with", "for", "in", "on", "&"}
_CLAUSE_SPLIT = re.compile(r"(?<=[.;!?])\s+|\s+(?:but|however|although|though)\s+", re.I)
_BULLET = re.compile(r"^\s*(?:[*\-•–]|\d+[.)])\s+")


@dataclass(frozen=True)
class Section:
    header: str
    kind: str
    body: str


@dataclass(frozen=True)
class Mention:
    term: str
    weight: float
    kind: str
    evidence: str


def _title_like(line: str) -> bool:
    """True for 'UI & Design' and 'Testing & DevOps', false for a sentence.

    ``str.istitle()`` is useless here because it rejects any acronym.
    """
    words = re.findall(r"[A-Za-z][A-Za-z/&'-]*", line)
    significant = [word for word in words if word.lower() not in _STOPWORDS]
    if not significant:
        return False
    return sum(word[0].isupper() for word in significant) >= len(significant) * 0.7


def _known_header(line: str) -> bool:
    """A line that names a section we recognise, whatever its capitalisation.

    Title case alone is not enough: "Nice to have" is a header on half the job
    boards in existence and only its first word is capitalised, so relying on
    case merged it into the requirements above it and turned every nice-to-have
    into a hard requirement.
    """
    plain = line.replace("’", "'").replace("‘", "'")
    return any(pattern.match(plain) for pattern in (_OPTIONAL_HEADER, _MANDATORY_HEADER, _DUTIES_HEADER, _NOISE_HEADER))


def _is_header(line: str, following: str) -> bool:
    stripped = line.strip()
    if not stripped or _BULLET.match(line) or len(stripped) > 60:
        return False
    if re.match(r"^#{1,4}\s", stripped):
        return True
    if stripped.endswith(":"):
        return True
    if _known_header(stripped.rstrip(":")):
        return True
    if stripped.endswith((".", ",", "!", "?")):
        return False
    return _title_like(stripped) and (not following or bool(_BULLET.match(following)) or _title_like(following) is False)


def _classify(header: str) -> str:
    header = header.replace("’", "'").replace("‘", "'")
    if _OPTIONAL_HEADER.match(header):
        return "optional"
    if _MANDATORY_HEADER.match(header):
        return "mandatory"
    if _DUTIES_HEADER.match(header):
        return "duties"
    if _NOISE_HEADER.match(header):
        return "noise"
    return "skills"


def segment(text: str) -> list[Section]:
    """Split a posting into labelled sections. Unstructured text yields one."""
    lines = text.splitlines()
    sections: list[Section] = []
    header, kind, body = "", "intro", []
    for index, line in enumerate(lines):
        following = next((later.strip() for later in lines[index + 1:] if later.strip()), "")
        if _is_header(line, following):
            if any(item.strip() for item in body) or header:
                sections.append(Section(header, kind, "\n".join(body).strip()))
            header = line.strip().lstrip("# ").rstrip(":")
            kind = _classify(header)
            body = []
        else:
            body.append(line)
    sections.append(Section(header, kind, "\n".join(body).strip()))
    return [item for item in sections if item.body or item.header]


def clauses(block: str):
    for line in block.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line:
            continue
        for clause in _CLAUSE_SPLIT.split(line):
            if clause.strip():
                yield clause.strip()


def _clause_weight(clause: str, term: str) -> tuple[float, str]:
    lowered = clause.casefold()
    head = clause[: lowered.index(term)]
    if NEGATION.search(head[-45:]):
        return NEGATED, "negated"
    if OPTIONAL_MARKER.search(clause):
        return OPTIONAL, "optional"
    if MANDATORY_MARKER.search(clause):
        return MANDATORY, "mandatory"
    return STATED, "stated"


def mentions(text: str, term: str) -> list[Mention]:
    """Every mention of ``term``, each weighted from its own clause and section.

    A bare heading is a label, not a claim, so it never votes on its own; it
    lends its section's body instead. That distinction is what keeps a
    'React Native' heading from outranking the 'is a strong plus' line below it.
    """
    term = term.casefold()
    found: list[Mention] = []
    for section in segment(text):
        in_header = term in section.header.casefold()
        in_body = term in section.body.casefold()
        if in_body:
            for clause in clauses(section.body):
                if term not in clause.casefold():
                    continue
                weight, kind = _clause_weight(clause, term)
                if section.kind == "optional" and kind == "stated":
                    weight, kind = OPTIONAL, "optional"
                elif section.kind == "mandatory" and kind == "stated":
                    weight, kind = MANDATORY, "mandatory"
                elif section.kind == "noise":
                    continue
                found.append(Mention(term, weight, kind, clause[:200]))
        elif in_header and section.kind != "noise":
            weight = _SECTION_WEIGHT[section.kind]
            body_clause = next(iter(clauses(section.body)), section.header)
            for clause in clauses(section.body):
                if OPTIONAL_MARKER.search(clause):
                    weight, body_clause = OPTIONAL, clause
                    break
                if MANDATORY_MARKER.search(clause):
                    weight, body_clause = MANDATORY, clause
                    break
            if weight > NEGATED:
                kind = "optional" if weight == OPTIONAL else "mandatory" if weight == MANDATORY else "stated"
                found.append(Mention(term, weight, kind, body_clause[:200]))
    return found


def importance(text: str, term: str) -> Mention | None:
    """One weight for ``term`` across the whole posting.

    An explicit marker outranks a bare mention, because saying "a strong plus"
    is a deliberate statement while listing a word is not. A term that is only
    ever negated scores zero.
    """
    found = mentions(text, term)
    if not found:
        return None
    if all(item.kind == "negated" for item in found):
        return found[0]
    voting = [item for item in found if item.kind != "negated"]
    for kind in ("mandatory", "optional"):
        explicit = [item for item in voting if item.kind == kind]
        if explicit:
            return explicit[0]
    return voting[0]


_US = re.compile(r"\b(?:u\.?s\.?a?|united states)\b", re.I)
# Verbs that make a sentence about where a person may live and work.
_ELIGIBILITY = re.compile(
    r"\b(?:must be|located|locate|based|residing|reside|residents?|residency|"
    r"work(?:ing)? from|eligible to work|authoriz\w+ to work|legally able to work|"
    r"citizens?|work authorization)\b", re.I)
# A pay band quoted for one country says nothing about who may apply.
_COMPENSATION = re.compile(r"\b(?:salary|salaries|pay|compensation|bonus|equity|benefits?|401k|wage)\b", re.I)
# The terse form, which carries no verb: "US only", "United States only".
_US_ONLY = re.compile(r"\b(?:u\.?s\.?a?|united states)[- ]?only\b", re.I)


# A board's own location field, which adapters render into the document.
_LOCATION_LINE = re.compile(r"^\*?\s*location\s*:\s*(.+)$", re.I)
# Anything here means the role is not confined to the United States.
_ELSEWHERE = re.compile(r"\b(poland|polska|europe|european|eu|emea|worldwide|global|anywhere|"
                        r"germany|france|spain|netherlands|portugal|uk|united kingdom)\b", re.I)


def us_only_requirement(text: str) -> str:
    """The clause restricting this role to the United States, or ``""``.

    A substring search over the whole posting cannot tell an eligibility rule
    from a salary footnote. GitLab's "the base salary range ... is currently for
    residents of the United States only" sits two lines under "Remote-Global",
    and reading it as a hard blocker force-skips a globally remote role; Vercel's
    "be located remotely within the United States" is a real restriction that
    matches none of the obvious phrasings. So look at one clause at a time and
    ignore the ones that are talking about money.
    """
    # The board's own location field settles it without reading any prose:
    # "Remote, USA" is a restriction, "Berlin, Remote - Germany" is not.
    for line in text.splitlines():
        found = _LOCATION_LINE.match(line.strip())
        if found and _US.search(found.group(1)) and not _ELSEWHERE.search(found.group(1)):
            return line.strip(" *")[:200]
    for section in segment(text):
        if section.kind == "noise":
            continue
        for clause in clauses(f"{section.header}\n{section.body}"):
            if not _US.search(clause) or _COMPENSATION.search(clause):
                continue
            if _US_ONLY.search(clause) or _ELIGIBILITY.search(clause):
                return clause[:200]
    return ""


def weight_of(text: str, *terms: str) -> float:
    """Strongest importance among ``terms``. 0.0 when absent or negated."""
    found = [importance(text, term) for term in terms]
    return max((item.weight for item in found if item), default=0.0)


def evidence_for(text: str, *terms: str) -> str:
    found = [importance(text, term) for term in terms]
    ranked = sorted((item for item in found if item), key=lambda item: item.weight, reverse=True)
    return ranked[0].evidence if ranked else ""
