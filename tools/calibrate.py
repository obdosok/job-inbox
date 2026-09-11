"""Run the corpus through the rules alone and through the advisor, and report
where they disagree. Needs ANTHROPIC_API_KEY or OPENAI_API_KEY.

  python tools/calibrate.py <corpus_dir> <out_dir> [track]
"""
import json
import os
import pathlib
import sys
import time

os.environ.setdefault("JOB_INBOX_ADVISOR", "findings")

from src import advise
from src.evaluate import evaluate
from src.extraction import HeuristicExtractor
from src.ingest import infer_metadata
from src.models import Job

corpus = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
track = sys.argv[3] if len(sys.argv) > 3 else "B"
out.mkdir(parents=True, exist_ok=True)

advisor = advise.get_default_advisor()
if advisor is None:
    print("No advisor available. Set ANTHROPIC_API_KEY (or OPENAI_API_KEY), then run "
          "`python -m src.cli profile refresh`. Check with `python -m src.cli profile show`.")
    raise SystemExit(1)

extractor = HeuristicExtractor()
rows, totals = [], {"in": 0, "out": 0, "cached": 0, "seconds": 0.0, "failed": 0}

for path in sorted(corpus.glob("*.txt")):
    text = path.read_text(encoding="utf-8")
    job = Job(job_id=path.stem, text=text, track=track,
              metadata=infer_metadata(text), profile=extractor.extract(text))
    rules = evaluate(job)

    started = time.monotonic()
    assessment = advise.assess(text, advisor)
    elapsed = time.monotonic() - started
    totals["seconds"] += elapsed

    if assessment is None or "advisor_error" in assessment:
        totals["failed"] += 1
        print(f"  FAIL {path.stem}: {(assessment or {}).get('advisor_error')}")
        rows.append({"job": path.stem, "error": (assessment or {}).get("advisor_error")})
        continue

    advised = evaluate(job, assessment)
    usage = assessment.get("usage") or {}
    totals["in"] += usage.get("input_tokens") or 0
    totals["out"] += usage.get("output_tokens") or 0
    totals["cached"] += usage.get("cached_input_tokens") or 0

    (out / f"{path.stem}.json").write_text(
        json.dumps({"assessment": assessment, "advised": advised.to_dict(), "rules_only": rules.to_dict()},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    rows.append({
        "job": path.stem,
        "rules": f"{rules.decision} {rules.overall_score}",
        "advised": f"{advised.decision} {advised.overall_score}",
        "model": f"{assessment['model_decision']} {assessment['model_score']}",
        "agree": advised.decision == rules.decision,
        "findings": len(assessment["findings"]),
        "mitigations": len(assessment["mitigations"]),
        "draft": len(assessment["draft_message"]),
        "seconds": round(elapsed, 1),
    })
    print(f"  {path.stem[:46]:<48} rules={rows[-1]['rules']:<10} advisor={rows[-1]['advised']:<10} model={rows[-1]['model']:<10} {elapsed:.1f}s")

(out / "_summary.json").write_text(json.dumps({"rows": rows, "totals": totals}, indent=2), encoding="utf-8")
print(f"\ntokens in={totals['in']} (cached {totals['cached']}) out={totals['out']} | {totals['seconds']:.0f}s total | {totals['failed']} failed")
print(f"details: {out}")
