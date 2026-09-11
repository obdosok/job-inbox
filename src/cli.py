from __future__ import annotations

import argparse
import os
import json
import sys
from pathlib import Path

from . import advise, candidate, providers
from .evaluate import evaluate
from .db import JobDatabase
from .ingest import load_job
from .models import Evaluation
from .render import decision_card
from .service import ingest_jobs


ROOT = Path(__file__).resolve().parents[1]
EVALUATED = ROOT / "jobs" / "evaluated"
OUTCOMES = ROOT / "outcomes" / "applications.jsonl"


def save_evaluation(evaluation: Evaluation) -> Path:
    EVALUATED.mkdir(parents=True, exist_ok=True)
    destination = EVALUATED / f"{evaluation.job_id}.json"
    destination.write_text(json.dumps(evaluation.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


def cmd_evaluate(args: argparse.Namespace) -> int:
    stdin_text = None if args.path else sys.stdin.read()
    job = load_job(args.path, stdin_text, args.track, args.meta)
    result = evaluate(job, advise.assess(job.text))
    save_evaluation(result)
    database = JobDatabase()
    try:
        source_ref = database.get_job(result.job_id)["source_ref"]
    except KeyError:
        source_ref = f"cli:{result.job_id}"
    database.upsert_job({
        "id": result.job_id,
        "title": job.profile.get("role_title") or next((line.strip() for line in job.text.splitlines() if line.strip()), result.job_id),
        "company": job.profile.get("company", ""), "source": "cli", "source_ref": source_ref,
        "source_url": job.metadata.get("source_url", ""), "raw_text": job.text, "track": job.track,
        "metadata": job.metadata, "profile": job.profile, "evaluation": result.to_dict(),
        "decision": result.decision, "overall_score": result.overall_score,
    })
    print(decision_card(result))
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    rows = []
    if EVALUATED.exists():
        for path in EVALUATED.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("track") == args.track:
                rows.append(data)
    rows.sort(key=lambda item: (item["decision"] == "APPLY", item["overall_score"]), reverse=True)
    if not rows:
        print(f"No evaluated Track {args.track} jobs.")
        return 0
    for index, row in enumerate(rows[:args.limit], 1):
        print(f"{index:>2}. {row['job_id']:<32} {row['decision']:<5} {row['overall_score']}/10 ({row['confidence']})")
    return 0


def cmd_outcome_add(args: argparse.Namespace) -> int:
    OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
    record = {"job_id": args.job_id, "stage": args.stage, "date": args.date, "notes": args.notes, "rate": args.rate, "compensation": args.compensation, "actual_job_fit": args.actual_job_fit}
    database = JobDatabase()
    database.bootstrap_existing()
    database.add_outcome({"job_id": args.job_id, "stage": args.stage, "date": args.date, "notes": args.notes, "rate": args.rate, "compensation": args.compensation, "actual_job_fit": args.actual_job_fit})
    with OUTCOMES.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Recorded {args.stage} outcome for {args.job_id}.")
    return 0


def cmd_regression(_: argparse.Namespace) -> int:
    # The seeded cases pin deterministic decisions. With a key in `.env` the
    # default extractor would call the model for every case: real spend, and
    # expectations that drift with the model's reading.
    from .extraction import HeuristicExtractor

    cases = json.loads((ROOT / "regression" / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    failures = 0
    for case in cases:
        raw = ROOT / case["job_file"]
        meta = ROOT / case["meta_file"]
        result = evaluate(load_job(str(raw), None, case["track"], str(meta), extractor=HeuristicExtractor()))
        save_evaluation(result)
        ok = result.decision == case["expected"]
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'} {case['id']}: expected {case['expected']}, got {result.decision} ({result.overall_score}/10)")
    return 1 if failures else 0


def cmd_profile_refresh(_: argparse.Namespace) -> int:
    try:
        destination = candidate.refresh_from_pdfs()
    except ImportError:
        print("Refreshing the profile needs pypdf: python -m pip install pypdf")
        return 1
    print(f"Wrote {destination} ({len(candidate.load())} characters) from the PDFs in the project root.")
    return 0


def cmd_profile_show(_: argparse.Namespace) -> int:
    text = candidate.load()
    if not text:
        print("No cached candidate profile. Run: python -m src.cli profile refresh")
        return 1
    provider = providers.configured_provider() or "none (no API key)"
    model = os.environ.get("JOB_INBOX_LLM_MODEL") or providers.default_model()
    print(f"Profile: {len(text)} characters")
    print(f"Provider: {provider} | model: {model}")
    print(f"Advisor: {advise.mode()}{'' if advise.is_active() else ' (inactive)'}")
    if providers.ENV_FILE.is_file():
        print(f"Key file: {providers.ENV_FILE}")
    elif not providers.has_key():
        print(f"No key. Copy .env.example to {providers.ENV_FILE.name} and put your key in it.")
    return 0


def _report_ingestion(jobs: list, report) -> int:
    print(report.summary())
    if report.rejected_titles:
        print("  filtered out, e.g.: " + "; ".join(report.rejected_titles[:5]))
    for row in sorted(jobs, key=lambda item: item["overall_score"], reverse=True)[:12]:
        evaluation = row["evaluation"]
        mark = "*" if evaluation.get("assessment_mode", "off") != "off" else " "
        verdict = (evaluation.get("match") or {}).get("verdict", "")
        print(f"  {mark} {evaluation['decision']:<6}{evaluation['overall_score']:<5} {verdict:<9} {row['title'][:52]}")
    if report.assessed:
        print(f"  (* = assessed by the advisor; the rest were scored deterministically)")
    return 0


def cmd_ingest_greenhouse(args: argparse.Namespace) -> int:
    jobs, report = ingest_jobs(JobDatabase(), "greenhouse", {"board": args.board, "limit": args.limit}, args.track)
    return _report_ingestion(jobs, report)


def cmd_ingest_watchlist(args: argparse.Namespace) -> int:
    """Poll every board on the watch list, one after another.

    A board that has gone away is reported and skipped: this runs unattended, and
    one dead company must not cost the other eight their run.
    """
    boards = json.loads((ROOT / "sources" / "watchlist.yaml").read_text(encoding="utf-8"))["boards"]
    database = JobDatabase()
    everything, failures = [], []
    for entry in boards:
        label = entry.get("label", entry["board"])
        try:
            jobs, report = ingest_jobs(database, entry["adapter"],
                                       {"board": entry["board"], "limit": args.limit}, entry.get("track", args.track))
        except Exception as error:
            failures.append(f"{label}: {type(error).__name__}: {str(error)[:70]}")
            continue
        everything.extend(jobs)
        new = [row for row in jobs if row["evaluation"].get("assessment_mode", "off") == "off"]
        print(f"{label}")
        print(f"   {report.summary()}")
        for row in sorted(new, key=lambda item: item["overall_score"], reverse=True)[:3]:
            print(f"     {row['evaluation']['decision']:<6}{row['overall_score']:<5} {row['title'][:56]}")
    if failures:
        print("\nboards that did not answer:")
        for failure in failures:
            print(f"   {failure}")
    print(f"\n{len(everything)} openings across {len(boards) - len(failures)} boards. "
          f"Assess the ones worth reading from the inbox.")
    return 0


def cmd_ingest_nofluffjobs(args: argparse.Namespace) -> int:
    jobs, report = ingest_jobs(JobDatabase(), "nofluffjobs",
                               {"category": args.category, "region": args.region, "limit": args.limit,
                                "seniority": args.seniority}, args.track)
    return _report_ingestion(jobs, report)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Local deterministic job evaluator")
    commands = root.add_subparsers(dest="command", required=True)
    evaluate_parser = commands.add_parser("evaluate", help="Evaluate a job file or stdin")
    evaluate_parser.add_argument("path", nargs="?")
    evaluate_parser.add_argument("--track", choices=("A", "B"), required=True)
    evaluate_parser.add_argument("--meta", help="Optional JSON metadata file")
    evaluate_parser.set_defaults(func=cmd_evaluate)
    rank = commands.add_parser("rank", help="List top persisted evaluations")
    rank.add_argument("--track", choices=("A", "B"), required=True)
    rank.add_argument("--limit", type=int, default=10)
    rank.set_defaults(func=cmd_rank)
    outcome = commands.add_parser("outcome", help="Record application outcomes")
    outcome_commands = outcome.add_subparsers(dest="outcome_command", required=True)
    add = outcome_commands.add_parser("add")
    add.add_argument("--job-id", required=True)
    add.add_argument("--stage", required=True, choices=("applied", "replied", "interview", "recruiter-screen", "technical-interview", "rejected", "offer", "accepted"))
    add.add_argument("--date", required=True)
    add.add_argument("--notes", default="")
    add.add_argument("--rate", type=float)
    add.add_argument("--compensation")
    add.add_argument("--actual-job-fit", type=float)
    add.set_defaults(func=cmd_outcome_add)
    regression = commands.add_parser("regression", help="Run seeded decision cases")
    regression.set_defaults(func=cmd_regression)
    profile = commands.add_parser("profile", help="Manage the cached candidate profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("refresh", help="Re-extract the profile from the PDFs in the project root").set_defaults(func=cmd_profile_refresh)
    profile_commands.add_parser("show", help="Report the cached profile and advisor mode").set_defaults(func=cmd_profile_show)
    ingest = commands.add_parser("ingest", help="Import jobs through a safe source adapter")
    ingest_commands = ingest.add_subparsers(dest="ingest_command", required=True)
    greenhouse = ingest_commands.add_parser("greenhouse", help="Read a public Greenhouse board")
    watchlist = ingest_commands.add_parser("watchlist", help="Poll every board in sources/watchlist.yaml")
    watchlist.add_argument("--track", choices=("A", "B"), default="A")
    watchlist.add_argument("--limit", type=int, default=60)
    watchlist.set_defaults(func=cmd_ingest_watchlist)
    ashby = ingest_commands.add_parser("ashby", help="Read a public Ashby job board")
    ashby.add_argument("--board", required=True, help="Ashby board token, e.g. oviva")
    ashby.add_argument("--track", choices=("A", "B"), default="A")
    ashby.add_argument("--limit", type=int, default=60)
    ashby.set_defaults(func=lambda args: _report_ingestion(*ingest_jobs(
        JobDatabase(), "ashby", {"board": args.board, "limit": args.limit}, args.track)))
    nofluff = ingest_commands.add_parser("nofluffjobs", help="Read the No Fluff Jobs board")
    nofluff.add_argument("--category", default="frontend", help="Board category, e.g. frontend, fullstack, backend")
    nofluff.add_argument("--region", default="pl")
    nofluff.add_argument("--track", choices=("A", "B"), default="A")
    nofluff.add_argument("--limit", type=int, default=40)
    nofluff.add_argument("--seniority", nargs="*", default=["senior", "expert"],
                         help="Grades to ask the board for; pass none to take every grade")
    nofluff.set_defaults(func=cmd_ingest_nofluffjobs)
    greenhouse.add_argument("--board", required=True, help="Public Greenhouse board token")
    greenhouse.add_argument("--track", choices=("A", "B"), default="A")
    greenhouse.add_argument("--limit", type=int, default=50)
    greenhouse.set_defaults(func=cmd_ingest_greenhouse)
    return root


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
