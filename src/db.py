from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "job_inbox.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobDatabase:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("JOB_INBOX_DB", DEFAULT_DB))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL UNIQUE,
                    source_url TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL,
                    track TEXT NOT NULL CHECK (track IN ('A', 'B')),
                    metadata_json TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    evaluation_json TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('APPLY', 'MAYBE', 'SKIP')),
                    overall_score REAL NOT NULL,
                    workflow_status TEXT NOT NULL DEFAULT 'new',
                    discovered_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    outcome_date TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    rate REAL,
                    compensation TEXT,
                    actual_job_fit REAL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_decision_discovered
                    ON jobs(decision, discovered_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_workflow_status
                    ON jobs(workflow_status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_outcomes_job_date
                    ON outcomes(job_id, outcome_date DESC);
                """
            )
            db.execute("PRAGMA optimize")

    def upsert_job(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        discovered = record.get("discovered_at") or now
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO jobs (
                    id, title, company, source, source_ref, source_url, raw_text, track,
                    metadata_json, profile_json, evaluation_json, decision, overall_score,
                    workflow_status, discovered_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
                ON CONFLICT(source_ref) DO UPDATE SET
                    title=excluded.title, company=excluded.company, source_url=excluded.source_url,
                    raw_text=excluded.raw_text, track=excluded.track, metadata_json=excluded.metadata_json,
                    profile_json=excluded.profile_json, evaluation_json=excluded.evaluation_json,
                    decision=excluded.decision, overall_score=excluded.overall_score, updated_at=excluded.updated_at
                """,
                (
                    record["id"], record["title"], record.get("company", ""), record["source"],
                    record["source_ref"], record.get("source_url", ""), record["raw_text"], record["track"],
                    json.dumps(record["metadata"], ensure_ascii=False), json.dumps(record["profile"], ensure_ascii=False),
                    json.dumps(record["evaluation"], ensure_ascii=False), record["decision"], record["overall_score"],
                    discovered, now, now,
                ),
            )
        return self.get_job_by_source_ref(record["source_ref"])

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["profile"] = json.loads(result.pop("profile_json"))
        result["evaluation"] = json.loads(result.pop("evaluation_json"))
        return result

    def get_job_by_source_ref(self, source_ref: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE source_ref = ?", (source_ref,)).fetchone()
        if not row:
            raise KeyError(source_ref)
        return self._decode(row)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return self._decode(row)

    def list_jobs(self, inbox_filter: str = "today") -> list[dict[str, Any]]:
        where = {
            "today": "date(discovered_at, 'localtime') = date('now', 'localtime')",
            "apply": "decision = 'APPLY'",
            "maybe": "decision = 'MAYBE'",
            "skip": "decision = 'SKIP'",
            "all": "1 = 1",
        }.get(inbox_filter)
        if where is None:
            raise ValueError("Unknown inbox filter")
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM jobs WHERE {where} ORDER BY CASE decision WHEN 'APPLY' THEN 0 WHEN 'MAYBE' THEN 1 ELSE 2 END, overall_score DESC, discovered_at DESC LIMIT 250"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS found,
                       SUM(CASE WHEN decision = 'SKIP' THEN 1 ELSE 0 END) AS skipped,
                       SUM(CASE WHEN decision IN ('APPLY', 'MAYBE') THEN 1 ELSE 0 END) AS review
                FROM jobs
                WHERE date(discovered_at, 'localtime') = date('now', 'localtime')
                """
            ).fetchone()
            top = db.execute(
                """
                SELECT id, title, company, decision, overall_score
                FROM jobs WHERE decision IN ('APPLY', 'MAYBE')
                ORDER BY CASE decision WHEN 'APPLY' THEN 0 ELSE 1 END, overall_score DESC, discovered_at DESC LIMIT 3
                """
            ).fetchall()
        return {"jobs_found": row["found"] or 0, "auto_skipped": row["skipped"] or 0, "worth_reviewing": row["review"] or 0, "top_recommendations": [dict(item) for item in top]}

    def prepare_application(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        evaluation = job["evaluation"]
        package = {
            "job_id": job_id,
            "title": job["title"],
            "positioning": evaluation.get("cv_positioning"),
            "proposal_angle": evaluation.get("proposal_positioning_angle", []),
            "gaps_to_address": evaluation.get("gaps", []),
            "mitigations": evaluation.get("mitigations", []),
            "clarifying_questions": evaluation.get("clarifying_questions", []),
            "draft_message": evaluation.get("draft_message", ""),
            "checklist": [
                "Verify eligibility, rate, hours and timezone.",
                "Select two evidence stories that match the actual work.",
                "Address the main gap honestly in one sentence.",
                "Draft the application; review it manually before sending anywhere.",
            ],
            "submission": "Not submitted. This app never sends applications.",
        }
        with self.connect() as db:
            db.execute("UPDATE jobs SET workflow_status = 'prepared', updated_at = ? WHERE id = ?", (utc_now(), job_id))
        return package

    def add_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed_stages = {"applied", "replied", "interview", "recruiter-screen", "technical-interview", "rejected", "offer", "accepted"}
        if payload.get("stage") not in allowed_stages:
            raise ValueError("Unknown outcome stage")
        try:
            datetime.fromisoformat(str(payload.get("date", "")))
        except ValueError as error:
            raise ValueError("Outcome date must use YYYY-MM-DD") from error
        self.get_job(payload["job_id"])
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO outcomes (job_id, stage, outcome_date, notes, rate, compensation, actual_job_fit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (payload["job_id"], payload["stage"], payload["date"], payload.get("notes", ""), payload.get("rate"), payload.get("compensation"), payload.get("actual_job_fit"), utc_now()),
            )
            db.execute("UPDATE jobs SET workflow_status = ?, updated_at = ? WHERE id = ?", (payload["stage"], utc_now(), payload["job_id"]))
        return {"id": cursor.lastrowid, **payload}

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute("SELECT lower(decision) AS name, COUNT(*) AS count FROM jobs GROUP BY decision").fetchall()
            today = db.execute("SELECT COUNT(*) FROM jobs WHERE date(discovered_at, 'localtime') = date('now', 'localtime')").fetchone()[0]
        result = {"today": today, "apply": 0, "maybe": 0, "skip": 0}
        result.update({row["name"]: row["count"] for row in rows})
        return result

    def bootstrap_existing(self) -> int:
        with self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
                return 0
        evaluated_dir = ROOT / "jobs" / "evaluated"
        imported = 0
        if not evaluated_dir.exists():
            return imported
        from .extraction import HeuristicExtractor

        extractor = HeuristicExtractor()
        for path in evaluated_dir.glob("*.json"):
            try:
                evaluation = json.loads(path.read_text(encoding="utf-8"))
                job_id = str(evaluation["job_id"])
                raw_path = ROOT / "jobs" / "raw" / f"{job_id}.txt"
                if not raw_path.exists():
                    continue
                raw_text = raw_path.read_text(encoding="utf-8")
                first_line = next((line.strip() for line in raw_text.splitlines() if line.strip()), job_id)
                profile = extractor.extract(raw_text)
                self.upsert_job({
                    "id": job_id, "title": first_line[:200], "company": "", "source": "existing-cli",
                    "source_ref": f"existing:{job_id}", "source_url": "", "raw_text": raw_text,
                    "track": evaluation["track"], "metadata": evaluation.get("metadata", {}), "profile": profile,
                    "evaluation": evaluation, "decision": evaluation["decision"],
                    "overall_score": evaluation["overall_score"], "discovered_at": evaluation.get("created_at"),
                })
                imported += 1
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        outcomes_path = ROOT / "outcomes" / "applications.jsonl"
        if outcomes_path.exists():
            for line in outcomes_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    self.add_outcome({
                        "job_id": record["job_id"], "stage": record["stage"], "date": record["date"],
                        "notes": record.get("notes", ""), "rate": record.get("rate"),
                        "compensation": record.get("compensation"), "actual_job_fit": record.get("actual_job_fit"),
                    })
                except (KeyError, ValueError, json.JSONDecodeError):
                    continue
        return imported
