from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass
class IngestedJob:
    title: str
    description: str
    source: str
    source_ref: str
    company: str = ""
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    discovered_at: str | None = None


USER_AGENT = "JobInbox/0.1 (personal job search)"


class IngestionAdapter(Protocol):
    name: str
    def ingest(self, payload: dict[str, Any]) -> list[IngestedJob]: ...


class ManualPasteAdapter:
    name = "manual"

    def ingest(self, payload: dict[str, Any]) -> list[IngestedJob]:
        description = str(payload.get("description", "")).strip()
        if not description:
            raise ValueError("Job description is required")
        title = str(payload.get("title", "")).strip() or next((line.strip() for line in description.splitlines() if line.strip()), "Untitled job")
        source_ref = str(payload.get("source_ref", "")).strip() or f"manual:{payload.get('content_hash', '')}"
        return [IngestedJob(title[:200], description, self.name, source_ref, str(payload.get("company", "")).strip(), str(payload.get("source_url", "")).strip(), dict(payload.get("metadata") or {}))]


class FileAdapter(ManualPasteAdapter):
    name = "file"

    def ingest(self, payload: dict[str, Any]) -> list[IngestedJob]:
        filename = str(payload.get("filename", "job.txt"))
        payload = dict(payload)
        payload["source_ref"] = payload.get("source_ref") or f"file:{filename}:{payload.get('content_hash', '')}"
        return super().ingest(payload)


class GreenhouseAdapter:
    name = "greenhouse"
    endpoint = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

    @staticmethod
    def _plain_text(value: str) -> str:
        value = html.unescape(value)
        value = re.sub(r"<\s*br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"</(?:p|li|div|h\d)>", "\n", value, flags=re.I)
        return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()

    def ingest(self, payload: dict[str, Any]) -> list[IngestedJob]:
        board = str(payload.get("board", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", board):
            raise ValueError("Greenhouse board token may contain only letters, numbers, underscores and hyphens")
        url = self.endpoint.format(board=urllib.parse.quote(board))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        jobs: list[IngestedJob] = []
        limit = max(1, min(int(payload.get("limit", 50)), 100))
        for item in data.get("jobs", [])[:limit]:
            updated = item.get("updated_at")
            metadata = {
                "location": (item.get("location") or {}).get("name", ""),
                "greenhouse_board": board,
                "updated_at": updated,
                "remote": "remote" in ((item.get("location") or {}).get("name", "")).casefold(),
            }
            if updated:
                try:
                    then = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    metadata["freshness_minutes"] = max(0, int((datetime.now(timezone.utc) - then.astimezone(timezone.utc)).total_seconds() / 60))
                except ValueError:
                    pass
            title = item.get("title") or "Untitled job"
            body = self._plain_text(item.get("content") or "")
            # Location lives in a field, not the prose. Render it in so the
            # parser and the advisor both see "Remote, USA" for what it is.
            if metadata["location"]:
                body = f"{title}\n{board}\n\nWorking conditions\n\n* Location: {metadata['location']}\n\n{body}"
            jobs.append(IngestedJob(
                title=title, description=body, source=self.name,
                source_ref=f"greenhouse:{board}:{item['id']}", company=board,
                source_url=item.get("absolute_url") or "", metadata=metadata,
            ))
        return jobs


class AshbyAdapter:
    """Ashby's public job board, common among health and European startups.

    Unlike Greenhouse it publishes plain-text descriptions and, more usefully,
    structured `workplaceType` and `isRemote` fields. A Berlin role marked
    Hybrid is a relocation requirement no matter how the prose is worded, so
    those facts are rendered into the document where the parser and the advisor
    both read them rather than left in metadata only.
    """

    name = "ashby"
    endpoint = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"

    @staticmethod
    def _conditions(job: dict[str, Any]) -> list[str]:
        places = [job.get("location")] + list(job.get("secondaryLocations") or [])
        named = [str(place.get("location") if isinstance(place, dict) else place).strip()
                 for place in places if place]
        lines = []
        if named:
            lines.append(f"* Location: {', '.join(dict.fromkeys(named))}")
        for label, key in (("Working mode", "workplaceType"), ("Employment", "employmentType"),
                           ("Team", "team"), ("Department", "department")):
            value = job.get(key)
            if value:
                lines.append(f"* {label}: {value}")
        if job.get("isRemote") is not None:
            lines.append(f"* Remote: {'yes' if job['isRemote'] else 'no'}")
        return lines

    def ingest(self, payload: dict[str, Any]) -> list[IngestedJob]:
        board = str(payload.get("board", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", board):
            raise ValueError("Ashby board token may contain only letters, numbers, dots, underscores and hyphens")
        accept = payload.get("accept_title")
        limit = max(1, min(int(payload.get("limit", 50)), 200))
        request = urllib.request.Request(self.endpoint.format(board=urllib.parse.quote(board)),
                                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        jobs: list[IngestedJob] = []
        for item in data.get("jobs", []):
            if len(jobs) >= limit:
                break
            title = str(item.get("title") or "Untitled job")
            if callable(accept) and not accept(title):
                continue
            body = str(item.get("descriptionPlain") or "").strip()
            if not body:
                body = GreenhouseAdapter._plain_text(html.unescape(str(item.get("descriptionHtml") or "")))
            conditions = self._conditions(item)
            document = "\n".join([title, board, "", *( ["Working conditions", ""] + conditions + [""] if conditions else []), body])
            location = str((item.get("location") or {}).get("location") if isinstance(item.get("location"), dict)
                           else item.get("location") or "")
            metadata: dict[str, Any] = {
                "location": location, "ashby_board": board,
                "workplace_type": item.get("workplaceType"), "remote": bool(item.get("isRemote")),
            }
            published = item.get("publishedAt")
            if published:
                try:
                    then = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                    metadata["freshness_minutes"] = max(0, int((datetime.now(timezone.utc) - then.astimezone(timezone.utc)).total_seconds() / 60))
                except ValueError:
                    pass
            jobs.append(IngestedJob(
                title=title, description=document, source=self.name,
                source_ref=f"ashby:{board}:{item.get('id') or title}", company=board,
                source_url=str(item.get("jobUrl") or item.get("applyUrl") or ""), metadata=metadata,
            ))
        return jobs


class NoFluffJobsAdapter:
    """The Polish board, which publishes structured postings rather than prose.

    Requirements arrive already split into `musts` and `nices`, and salary is
    machine-readable. Both are rendered back into a document with ordinary
    section headers, so `src.parse` reads them through the same path as a pasted
    posting instead of needing a second, board-specific code path.
    """

    name = "nofluffjobs"
    search_url = "https://nofluffjobs.com/api/search/posting?pageTo=1&pageSize={size}&salaryCurrency=PLN&salaryPeriod=month&region={region}"
    detail_url = "https://nofluffjobs.com/api/posting/{posting}"
    working_days = 21

    @staticmethod
    def _values(items: Any) -> list[str]:
        return [str(item.get("value", "")).strip() for item in (items or []) if str(item.get("value", "")).strip()]

    @classmethod
    def _salary(cls, essentials: dict[str, Any]) -> dict[str, Any]:
        """Normalise the published band into metadata the scorer understands."""
        original = (essentials or {}).get("originalSalary") or {}
        currency = original.get("currency") or ""
        meta: dict[str, Any] = {"currency": currency} if currency else {}
        for shape in (original.get("types") or {}).values():
            band = shape.get("range") or []
            if len(band) != 2:
                continue
            low, high = float(band[0]), float(band[1])
            period = str(shape.get("period", "")).casefold()
            if period == "hour":
                meta.update(rate_min=low, rate_max=high, rate_period="hour")
            else:
                factor = cls.working_days if period == "day" else 1
                meta.update(monthly_min=low * factor, monthly_max=high * factor)
            break
        return meta

    @classmethod
    def _document(cls, detail: dict[str, Any]) -> str:
        requirements = detail.get("requirements") or {}
        specs = detail.get("specs") or {}
        lines = [str(detail.get("title") or "Untitled job"), str((detail.get("company") or {}).get("name", ""))]
        essentials = detail.get("essentials") or {}
        if isinstance(essentials, dict) and essentials.get("description"):
            lines += ["", "About the role", "", str(essentials["description"])]
        for header, items in (("Requirements", requirements.get("musts")), ("Nice to have", requirements.get("nices"))):
            values = cls._values(items)
            if values:
                lines += ["", header, ""] + [f"* {value}" for value in values]
        conditions = [f"* {key}: {value}" for key, value in specs.items() if isinstance(value, (str, int, bool)) and str(value).strip()]
        if conditions:
            lines += ["", "Working conditions", ""] + conditions
        for header, key in (("What you will do", "dailyTasks"), ("About the project", "projectDescription")):
            body = requirements.get(key) or detail.get(key)
            values = cls._values(body) if isinstance(body, list) else ([str(body)] if body else [])
            if values:
                lines += ["", header, ""] + [f"* {value}" for value in values]
        return "\n".join(lines)

    def ingest(self, payload: dict[str, Any]) -> list[IngestedJob]:
        category = str(payload.get("category", "frontend")).strip()
        if not re.fullmatch(r"[a-z0-9-]{1,40}", category):
            raise ValueError("Category may contain only lowercase letters, numbers and hyphens")
        region = str(payload.get("region", "pl")).strip()
        if not re.fullmatch(r"[a-z]{2,10}", region):
            raise ValueError("Region may contain only lowercase letters")
        size = max(1, min(int(payload.get("limit", 50)), 200))
        # The board can filter by grade, and asking it to is both politer and far
        # more effective than downloading juniors to reject them: the unfiltered
        # feed front-loads them, so 40 fetched yielded 5 usable postings.
        seniority = [str(level).strip().lower() for level in payload.get("seniority", ("senior", "expert"))
                     if re.fullmatch(r"[a-z]{3,12}", str(level).strip().lower())]
        criteria: dict[str, Any] = {"category": [category]}
        if seniority:
            criteria["seniority"] = seniority
        request = urllib.request.Request(
            self.search_url.format(size=size, region=region),
            data=json.dumps({"criteriaSearch": criteria}).encode("utf-8"), method="POST",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            postings = json.loads(response.read().decode("utf-8")).get("postings", [])

        # Each posting needs its own detail request, so drop the ones we would
        # discard anyway before spending a round trip on them. The search result
        # already carries the title, which is all the title filter reads.
        accept = payload.get("accept_title")
        jobs: list[IngestedJob] = []
        for posting in postings[:size]:
            posting_id = str(posting.get("id") or "")
            if not posting_id:
                continue
            if callable(accept) and not accept(str(posting.get("title") or "")):
                continue
            try:
                detail = self._detail(posting_id)
            except (OSError, ValueError, json.JSONDecodeError):
                # One unavailable posting must not lose the rest of the batch.
                continue
            essentials = detail.get("essentials") or {}
            metadata = {"nofluffjobs_category": category, "location": ", ".join(
                str(place.get("city", "")) for place in ((detail.get("location") or {}).get("places") or []) if place.get("city"))}
            metadata.update(self._salary(essentials if isinstance(essentials, dict) else {}))
            jobs.append(IngestedJob(
                title=str(detail.get("title") or posting.get("title") or "Untitled job"),
                description=self._document(detail), source=self.name,
                source_ref=f"nofluffjobs:{posting_id}",
                company=str((detail.get("company") or {}).get("name", "")),
                source_url=f"https://nofluffjobs.com/job/{posting_id}", metadata=metadata,
            ))
            time.sleep(0.35)  # the endpoint is the site's own; do not hammer it
        return jobs

    def _detail(self, posting_id: str) -> dict[str, Any]:
        # Posting ids carry the city, so they contain non-ASCII: "...-Kraków".
        url = self.detail_url.format(posting=urllib.parse.quote(posting_id, safe=""))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))


ADAPTERS: dict[str, IngestionAdapter] = {
    "manual": ManualPasteAdapter(), "file": FileAdapter(), "greenhouse": GreenhouseAdapter(),
    "nofluffjobs": NoFluffJobsAdapter(), "ashby": AshbyAdapter(),
}
