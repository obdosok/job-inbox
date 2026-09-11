# Job Inbox

A local single-user web inbox and CLI that turn job descriptions into inspectable `APPLY`, `MAYBE`, or `SKIP` decisions. It does not scrape LinkedIn, automate Upwork, or submit applications.

> **Portfolio copy.** The candidate in `candidate/profile.example.md` is fictional. The real profile, job history, calibration data and API keys are not in this repository, and the real profile file is gitignored so it cannot be committed by accident.

## What this demonstrates

- **Model advises, code decides.** An LLM scores six dimensions of a two-sided match - does the candidate fit the job, and does the employer actually get the right person - and must quote both the posting and the profile for every finding. Code re-validates every field, applies the thresholds, and deterministic hard blockers override the model in every mode. When model and rules disagree, the disagreement is recorded and shown rather than silently resolved.
- **Grounding enforced in code, not requested in the prompt.** A finding without both quotes fails validation and the whole response is discarded. On a 10-posting calibration run, 232 citations were checked against their sources: none fabricated.
- **Cost as a design constraint.** Board feeds are fetched, de-duplicated and scored for free; the model runs only on an explicit click. A hard per-run ceiling can be lowered by a request but never raised, and the static profile prefix is prompt-cached for an hour.
- **Two providers, one contract.** Anthropic and OpenAI behind a single schema-constrained call, in raw `urllib`, so the project runs on a stock Python with no dependencies.
- **A test suite that cannot spend money.** Provider keys are stripped for the whole run and a test fails if one leaks back in.
- **Built AI-native.** Developed with Claude Code, with every step verified against live behaviour rather than trusted because the generated code looked right.


## Start the web app

From the repository root, in PowerShell:

```powershell
$py = 'python'
& $py -m src.web
```

Open `http://127.0.0.1:8765`. The inbox has Today / Apply / Maybe / Skip filters, a daily summary, manual paste and text-file ingestion, board feeds, full decision cards, application preparation and outcome recording.

Two ways in, and they cost differently:

- **Paste** or **File** — one job you chose deliberately. It is assessed immediately, model and all.
- **Feed** — a board. Fetched, de-duplicated and scored for free; **no model call is made**. Each card then carries an **Assess this one** button, so you decide job by job what is worth paying to read.

SQLite data is stored locally at `data/job_inbox.sqlite3`. Override it with `JOB_INBOX_DB` when needed.

## What it uses

- `candidate/profile.example.md`: a fictional candidate so the demo runs out of the box. `profile refresh` writes your real `candidate/profile.md` from PDFs, and that file is gitignored.
- `src/parse.py`: document structure — requirement importance, negation, responsibility sections.
- `src/score.py`: deterministic hard blockers, activity decomposition and dimension scoring.
- `src/advise.py`, `src/providers.py`: the semantic advisor and the two provider wire formats.
- `src/evaluate.py`: Track A/B weights, decision thresholds, compensation and positioning.
- `jobs/raw/`: manually saved job descriptions and optional JSON metadata.
- `jobs/evaluated/`: persisted structured evaluations.
- `outcomes/applications.jsonl`: append-only application outcomes for future calibration.
- `src/db.py`: SQLite schema and persistence.
- `src/extraction.py`: structured LLM extraction, validation and offline fallback.
- `src/adapters.py`: common ingestion interface and the manual, file, Greenhouse, Ashby and No Fluff Jobs adapters.
- `sources/watchlist.yaml`: health and diabetes employers polled by `ingest watchlist`.
- `src/funnel.py`: the title filter, de-duplication and spend cap.
- `web/`: the dependency-free single-page inbox.

The `.yaml` files intentionally use JSON syntax, which is valid YAML. This keeps v0 dependency-free and makes it work with a stock Python 3.10+ installation.

## Use

From this directory:

```powershell
python -m src.cli evaluate jobs/raw/senior-react-typescript.txt --track B --meta jobs/raw/senior-react-typescript.meta.json
Get-Content job.txt | python -m src.cli evaluate --track A
python -m src.cli rank --track B --limit 10
python -m src.cli outcome add --job-id job1 --stage technical-interview --date 2026-09-15 --notes "Good product discussion"
python -m src.cli regression
python -m unittest discover -s tests -v
```

Every evaluation is printed as a decision card and saved to `jobs/evaluated/<job-id>.json`. Stdin jobs receive a stable content-derived ID.

## Optional metadata

Metadata is JSON and overrides values inferred from text. Useful fields include:

```json
{
  "rate_min": 80,
  "rate_max": 160,
  "rate_period": "hour",
  "currency": "USD",
  "freshness_minutes": 30,
  "proposals": 50,
  "interviewing": 0,
  "connect_cost": 12,
  "remote": true,
  "eu_compatible": true,
  "timezone_compatible": true,
  "annual_max": 110000
}
```

Missing metadata lowers confidence rather than being silently treated as favorable. For `MAYBE`, inspect the gaps/risks and obtain the missing scope or market facts.

## Matching against the candidate

The candidate side comes from two PDFs you place in the project root, `cv.pdf` and `operating-manual.pdf`, not from hand-written rules. Extract them once:

```powershell
& $py -m pip install pypdf     # only needed to refresh
& $py -m src.cli profile refresh
```

That writes `candidate/profile.md`, which the advisor reads as prose. Normal runs need no dependencies.

`src/parse.py` reads the posting the same way a person skims it: it segments the document, tells a hard requirement from a nice-to-have, and ignores a negated mention. That is what a hard blocker, a penalty and the work-shape decomposition are computed from, and it is the whole story when no API key is set.

## The advisor

Word matching cannot weigh a proportion — one testing bullet among twenty skills is not a testing-heavy role — and cannot read 40k characters of operating-manual prose. The advisor can. Set `JOB_INBOX_ADVISOR`:

| Mode | Who decides | Use it when |
| --- | --- | --- |
| `findings` (default with a key) | The model scores each dimension; the weights and thresholds in `src/evaluate.py` decide | You want a decision you can audit and regression-test |
| `verdict` | The model's own decision and score are taken as-is | You want its judgement straight |
| `off` | Deterministic only | No key, or you want the rules alone |

Every finding must quote both the posting and the profile, so each claim is checkable by eye. Both modes also produce mitigations for the concerns, questions worth asking, and a draft application message. Deterministic hard blockers override every mode: a rate below the configured floor is arithmetic, not judgement.

The model's opinion is recorded even when the rules decide, so a disagreement is visible instead of silent. If the advisor is unavailable the job is still scored, and the reason is shown on the card.

Nothing is ever sent anywhere. The draft is yours to edit and send by hand.

## Feeds and the funnel

```powershell
& $py -m src.cli ingest watchlist --track A            # every board in sources/watchlist.yaml
& $py -m src.cli ingest nofluffjobs --category frontend --track A --limit 60
& $py -m src.cli ingest ashby --board oviva --track A
& $py -m src.cli ingest greenhouse --board BOARD_TOKEN --track A --limit 25
```

`sources/watchlist.yaml` is the standing list of health and diabetes employers with readable boards, each with a note on why it is there. Polling it is how a rare opening at a small company gets noticed the day it appears; a board that goes away is reported and skipped rather than ending the run.

Ashby publishes `workplaceType` and `isRemote` as fields, and Greenhouse publishes the location. Both are rendered into the document rather than left in metadata, so "Location: Remote, USA" reads as the restriction it is and "Berlin, Remote - Germany" does not.

No Fluff Jobs publishes structured postings, so the adapter renders `musts` and `nices` back into ordinary `Requirements` / `Nice to have` sections and lets `src/parse.py` read them through the same path as a pasted job. Salary arrives machine-readable and is normalised (a daily PLN band becomes a monthly one). The board filters by grade server-side — the default asks for `senior` and `expert`, which is politer than downloading juniors to reject them.

A feed is a firehose, and an assessment costs real money, so `src/funnel.py` narrows in stages, cheapest first:

```
fetched  -> title filter (free; applied before the per-posting detail request)
         -> de-duplicate (free; one opening is reposted once per region)
         -> deterministic score (free)
         -> advisor (paid, capped)
```

On a real run: 60 fetched, 50 of them regional reposts of the same jobs, 10 unique, 3 assessed. Without the middle two stages the budget would have bought five copies of one Inwedo posting.

| Variable | Effect |
| --- | --- |
| `JOB_INBOX_ADVISE_MAX` | Assessments per run. Default 5 — a hard ceiling on spend |
| `JOB_INBOX_ADVISE_MIN_SCORE` | Deterministic score a job must reach to be worth assessing. Default 4.5 |
| `JOB_INBOX_TITLE_INCLUDE` / `_EXCLUDE` | Override the title filter |

Feeds are always extracted offline, never through the model: a model extraction would run *before* the gate and bill every fetched posting in order to decide which ones are worth billing. Pasting a job in the web UI stays exempt from the whole funnel — that is a deliberate act, and it always earns an assessment.

## Calibrating the advisor

Prompt changes are only as good as what they do to real postings. `tools/calibrate.py` runs a folder of `.txt` postings through the rules alone and through the advisor, writes the full assessment for each, and reports where the two disagree along with token usage:

```powershell
& $py tools/calibrate.py jobs/raw tmp/calibration B
```

Read the disagreements first: they are where either the prompt or the thresholds are wrong.

## Structured LLM extraction

The app works without an API key using its deterministic heuristic extractor. To enable semantic extraction and the advisor, put a key in `.env` in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Copy `.env.example` to `.env` to start. `.env` is gitignored, and both the CLI and the web app read it. An exported environment variable always wins over the file, including an explicitly empty one — so `$env:ANTHROPIC_API_KEY=''` turns the advisor off for that shell without touching the file.

Either provider works; `OPENAI_API_KEY` uses the Responses API instead of the Messages API.

`src/providers.py` holds the only difference between them; everything downstream is shared. With both keys set, Anthropic is used unless `JOB_INBOX_PROVIDER` says otherwise.

| Variable | Effect |
| --- | --- |
| `JOB_INBOX_PROVIDER` | `anthropic` or `openai`. Defaults to whichever key is set |
| `JOB_INBOX_LLM_MODEL` | Model override. Defaults to `claude-opus-5` or `gpt-5-mini` |
| `JOB_INBOX_ADVISOR_MODEL` | A different model for the advisor than for extraction |
| `JOB_INBOX_EFFORT` | Anthropic only: `low`…`max`. Unset means the API default |
| `JOB_INBOX_MAX_TOKENS` | Anthropic only, default 16000 |
| `JOB_INBOX_CACHE_TTL` | Anthropic only, default `1h`. Set `5m` for the shorter, cheaper-to-write cache |

Run `& $py -m src.cli profile show` to see which provider and model are actually in force.

Two Anthropic-specific details are handled in `src/providers.py`: the candidate profile is sent as a `cache_control` system block with a one-hour TTL, so pasting a few jobs over an evening does not re-bill its 18k tokens each time (the default five-minute cache only survives a batch run), and JSON-schema keywords the Messages API rejects (`minimum`, `maxLength`, …) are stripped from the wire schema — `validate_profile` and `validate_assessment` re-check those bounds locally, so nothing is lost.

The LLM only extracts a schema-validated job profile: actual activity shares, requirements, work conditions, scope risks and unknowns. It does not decide whether to apply. Scores, Track A/B weights and hard blockers remain deterministic and inspectable. If the API is unavailable or its output fails validation, ingestion falls back to the offline extractor and records the fallback reason.

## Ingestion adapters

All sources implement the same adapter contract. Manual paste and file content stay local except when LLM extraction is enabled. The Greenhouse adapter accepts only a public board token and constructs a fixed `boards-api.greenhouse.io` URL, preventing arbitrary URL fetching.

Public Greenhouse boards can also be imported from the CLI:

```powershell
& $py -m src.cli ingest greenhouse --board BOARD_TOKEN --track A --limit 25
```

## Scoring model

Both tracks score Evidence Fit, Work Fit, Economics, Win Probability and Career Capital from 0-10. Track A gives more weight to stable role quality and future value. Track B gives more weight to existing evidence, freshness/read probability and speed-to-cash economics. The weights and thresholds are at the top of `src/evaluate.py`; signal rules are plain code in `src/score.py`.

Hard blockers override an attractive aggregate. Current explicit checks cover US-only eligibility, mandatory Shopify public-app evidence, mandatory mobile/app-store evidence, incompatible schedules/timezones and compensation below configured floors.

The work-shape section is deliberately activity-based. It estimates whether the role is really implementation, backend/data, audit/QA, mobile, design craft, operations or coordination instead of trusting labels such as "product-minded" or "ownership."

## Outcomes and future calibration

Outcome stages are `applied`, `replied`, `interview`, `recruiter-screen`, `technical-interview`, `rejected`, `offer`, and `accepted`. Optional rate, compensation and user-rated actual fit are preserved. This is enough to later compare response rate by role family, compensation, CV positioning, React depth and Track A/B without adding ML now.

The extraction prompt is isolated in `prompts/extract_job.md`. Candidate evidence remains bounded by `candidate/evidence_profile.yaml` and is never supplied by the extractor.
