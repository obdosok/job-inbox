import os
import pathlib
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from src import advise, candidate, funnel, providers
from src.advise import validate_assessment
from src.adapters import FileAdapter, GreenhouseAdapter, ManualPasteAdapter, NoFluffJobsAdapter
from src.db import JobDatabase
from src.evaluate import evaluate
from src.extraction import CATEGORIES, HeuristicExtractor, validate_profile
from src.models import Job
from src.parse import importance, us_only_requirement, weight_of
from src.score import actual_work_shape, hard_blockers, rate
from src.service import assess_job, ingest_jobs


_SAVED_KEYS: dict[str, str] = {}


def setUpModule():
    """No test may reach a provider.

    A key in the environment or in `.env` used to make the ingestion tests call
    the live API: real latency, real spend, and results that depend on someone's
    billing status. Tests that exercise a provider patch a fake key in for
    themselves; everything else runs offline.
    """
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if name in os.environ:
            _SAVED_KEYS[name] = os.environ.pop(name)


def tearDownModule():
    os.environ.update(_SAVED_KEYS)


class NetworkIsolationTests(unittest.TestCase):
    def test_the_suite_cannot_reach_a_provider(self):
        self.assertIsNone(providers.build(), "a provider key leaked into the test environment")
        self.assertIsNone(advise.get_default_advisor())


class ExtractionTests(unittest.TestCase):
    def test_heuristic_profile_is_valid_and_totals_100(self):
        profile = HeuristicExtractor().extract("Product owner building React UI with API integration and extensive QA testing")
        validate_profile(profile)
        self.assertEqual(100, sum(item["estimated_share"] for item in profile["activities"]))
        self.assertTrue(all(item["category"] in CATEGORIES for item in profile["activities"]))

    def test_invalid_share_total_is_rejected(self):
        profile = HeuristicExtractor().extract("React frontend")
        profile["activities"][0]["estimated_share"] = 40
        with self.assertRaisesRegex(ValueError, "total 100"):
            validate_profile(profile)


class ImportanceTests(unittest.TestCase):
    """A mention's weight must come from how the posting words it."""

    def test_nice_to_have_is_not_a_requirement(self):
        text = "Requirements\n\n* Expert React\n\nReact Native\n\nExperience with React Native / Expo is a strong plus."
        self.assertEqual("optional", importance(text, "react native").kind)
        self.assertEqual("mandatory", importance(text, "react").kind)

    def test_negated_terms_do_not_count(self):
        for text in ("We do NOT use Vue.", "No TypeScript on this project.", "No Shopify experience required."):
            term = next(item for item in ("vue", "typescript", "shopify") if item in text.casefold())
            with self.subTest(text=text):
                self.assertEqual("negated", importance(text, term).kind)
                self.assertEqual(0.0, weight_of(text, term))

    def test_absent_term_has_no_weight(self):
        self.assertIsNone(importance("A plain backend role.", "shopify"))
        self.assertEqual(0.0, weight_of("A plain backend role.", "shopify"))

    def test_heuristic_requirements_are_not_all_mandatory(self):
        text = "Requirements\n\n* 5+ years of React\n\nNice to have\n\n* Shopify experience is a plus"
        importances = {item["importance"] for item in HeuristicExtractor().extract(text)["requirements"]}
        self.assertIn("mandatory", importances)
        self.assertIn("preferred", importances)


class USEligibilityTests(unittest.TestCase):
    """A pay band quoted for one country is not a rule about who may apply."""

    def test_salary_footnote_is_not_an_eligibility_rule(self):
        posting = ("Remote-Global\n\nStaff Engineer\n\nCompensation\n\nThe base salary range for this role's "
                   "listed level is currently for residents of the United States only.\n\n"
                   "Country Hiring Guidelines\n\nGitLab hires new team members in countries around the world.")
        self.assertEqual("", us_only_requirement(posting))
        self.assertEqual([], hard_blockers(Job(job_id="x", text=posting, track="A")))

    def test_a_residency_requirement_is_caught_and_quoted(self):
        posting = "About the role\n\nYou will report to the Head of Security and be located remotely within the United States."
        found = us_only_requirement(posting)
        self.assertIn("located remotely within the United States", found)
        self.assertIn(found[:40], hard_blockers(Job(job_id="x", text=posting, track="A"))[0])

    def test_wordings_that_do_and_do_not_restrict(self):
        blocking = ["US only. Senior React role.",
                    "You must be authorized to work in the US.",
                    "Candidates must be based in the United States.",
                    "This position is open to US residents only."]
        harmless = ["Our customers are mostly in the United States.",
                    "Overlap with US business hours is required.",
                    "We are a US company hiring globally.",
                    "The US salary range is $200,000 - $300,000."]
        for text in blocking:
            with self.subTest(text=text):
                self.assertNotEqual("", us_only_requirement(text))
        for text in harmless:
            with self.subTest(text=text):
                self.assertEqual("", us_only_requirement(text))

    def test_a_board_location_field_settles_it_without_prose(self):
        """Adapters render the board's own location line into the document."""
        restricted = ["Working conditions\n\n* Location: Remote, USA",
                      "Working conditions\n\n* Location: United States"]
        open_to_him = ["Working conditions\n\n* Location: Berlin, Remote - Germany",
                       "Working conditions\n\n* Location: Remote - Global",
                       "Working conditions\n\n* Location: Warsaw",
                       "Working conditions\n\n* Location: New York, London, Remote - Europe"]
        for text in restricted:
            with self.subTest(text=text):
                self.assertIn("Location", us_only_requirement(text))
        for text in open_to_him:
            with self.subTest(text=text):
                self.assertEqual("", us_only_requirement(text))

    def test_declared_eligibility_clears_the_blocker(self):
        posting = "Candidates must be based in the United States."
        self.assertEqual([], hard_blockers(Job(job_id="x", text=posting, track="A", metadata={"us_eligible": True})))


class ScoringRobustnessTests(unittest.TestCase):
    def test_unusable_metadata_never_raises(self):
        text = "Senior React TypeScript engineer building a data-heavy reporting application. " * 4
        for meta in ({"rate_min": 80, "rate_max": None}, {"proposals": None}, {"interviewing": None},
                     {"connect_cost": None}, {"duration_weeks": "ten"}, {"rate_min": "80", "rate_max": "160"}):
            with self.subTest(meta=meta):
                self.assertIn(evaluate(Job(job_id="x", text=text, track="B", metadata=meta)).decision,
                              {"APPLY", "MAYBE", "SKIP"})

    def test_single_ended_rate_is_not_dropped(self):
        self.assertEqual((200.0, 200.0), rate({"rate_max": 200}))
        self.assertEqual((80.0, 160.0), rate({"rate_min": 80, "rate_max": 160}))

    def test_thin_posting_never_reaches_apply(self):
        result = evaluate(Job(job_id="x", text="typescript vue react api migration performance reporting", track="B"))
        self.assertEqual("low", result.confidence)
        self.assertNotEqual("APPLY", result.decision)

    def test_work_shape_reads_responsibilities_not_the_skills_list(self):
        text = pathlib.Path("jobs/raw/structured-senior-frontend.txt").read_text(encoding="utf-8")
        shape = " ".join(actual_work_shape(text))
        self.assertNotIn("app-store", shape)
        self.assertNotIn("mobile", shape)


def assessment(**overrides):
    data = {
        "role_summary": "Senior React product engineer.",
        "match": {"verdict": "strong",
                  "for_candidate": "The work is application engineering he has done for years.",
                  "for_employer": "They get proven production judgment rather than someone learning the domain.",
                  "decisive_factor": "The stack match is direct and the mode of work is autonomous."},
        "working_style": {"how_this_role_runs": "Autonomous, product-facing, few meetings.",
                          "suits_him": ["end-to-end ownership"], "drains_him": ["exhaustive manual QA"]},
        "not_my_strengths": ["Pixel-perfect visual polish"],
        "dimension_scores": {"capability_fit": 9, "working_style_fit": 9, "conditions_fit": 8,
                             "employer_benefit": 8, "win_probability": 8, "career_capital": 7},
        "findings": [{"dimension": "capability_fit", "verdict": "strength", "claim": "React and TypeScript are the core ask.",
                      "job_evidence": "Expert-level React and TypeScript", "profile_evidence": "production React, Next.js and TypeScript"}],
        "mitigations": [{"concern": "React tenure is recent", "how_to_address": "Lead with 18 years of application engineering."}],
        "clarifying_questions": ["What does the first milestone look like?"],
        "model_decision": "APPLY", "model_score": 8, "model_reasoning": "Strong match.",
        "draft_message": "I build data-heavy React applications.", "confidence": "high",
    }
    data.update(overrides)
    return data


class AdvisorTests(unittest.TestCase):
    text = "Senior React TypeScript engineer building data-heavy reporting applications. " * 5

    def evaluate_in(self, mode, **overrides):
        with patch.dict("os.environ", {"JOB_INBOX_ADVISOR": mode}, clear=False):
            return evaluate(Job(job_id="x", text=self.text, track="B"), assessment(**overrides))

    def test_findings_mode_runs_model_scores_through_the_code_thresholds(self):
        result = self.evaluate_in("findings", model_decision="SKIP", model_score=2)
        self.assertEqual("findings", result.assessment_mode)
        self.assertEqual("APPLY", result.decision)
        self.assertFalse(result.model_opinion["agrees"])
        self.assertTrue(result.deterministic_scores, "the rules-only scores stay available for comparison")

    def test_a_poor_pairing_skips_however_well_it_scores(self):
        """The point of the redesign: a role can look good and still be wrong.

        High marks on every dimension must not outvote "the employer would be
        better served by someone else" or "he could not sustain this".
        """
        result = self.evaluate_in("findings", match={
            "verdict": "poor",
            "for_candidate": "He could do it, but the meeting load would grind him down.",
            "for_employer": "They need a Shopify specialist and would be paying him to learn.",
            "decisive_factor": "Wrong person for their actual problem."})
        self.assertEqual("SKIP", result.decision)
        self.assertGreater(result.overall_score, 7, "the indicator score stays high; only the verdict changed")

    def test_working_style_and_limits_reach_the_card(self):
        result = self.evaluate_in("findings")
        self.assertEqual("strong", result.match["verdict"])
        self.assertTrue(result.match["for_employer"])
        self.assertIn("exhaustive manual QA", result.working_style["drains_him"])
        self.assertTrue(result.not_my_strengths)

    def test_the_employer_side_may_not_be_left_blank(self):
        with self.assertRaisesRegex(ValueError, "employer's side"):
            validate_assessment(assessment(match={"verdict": "strong", "for_candidate": "Good.",
                                                  "for_employer": "   ", "decisive_factor": "x"}))

    def test_verdict_mode_takes_the_model_decision(self):
        result = self.evaluate_in("verdict", model_decision="SKIP", model_score=2)
        self.assertEqual("verdict", result.assessment_mode)
        self.assertEqual("SKIP", result.decision)
        self.assertEqual(2.0, result.overall_score)

    def test_hard_blockers_override_every_mode(self):
        blocked = "US only. " + self.text
        for mode in ("findings", "verdict"):
            with self.subTest(mode=mode), patch.dict("os.environ", {"JOB_INBOX_ADVISOR": mode}, clear=False):
                result = evaluate(Job(job_id="x", text=blocked, track="B"), assessment())
                self.assertEqual("SKIP", result.decision)
                self.assertTrue(result.hard_blockers)

    def test_advisor_failure_falls_back_and_says_so(self):
        class Broken:
            def assess(self, description):
                raise urllib.error.URLError("no route to host")

        result = evaluate(Job(job_id="x", text=self.text, track="B"), advise.assess(self.text, Broken()))
        self.assertEqual("off", result.assessment_mode)
        self.assertIn("URLError", result.assessment_note)
        self.assertIn(result.decision, {"APPLY", "MAYBE", "SKIP"})

    def test_carries_the_advice_the_scorer_never_had(self):
        result = self.evaluate_in("findings")
        self.assertTrue(result.draft_message)
        self.assertTrue(result.mitigations)
        self.assertTrue(result.clarifying_questions)

    def test_unevidenced_findings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "quote both"):
            validate_assessment(assessment(findings=[{"dimension": "working_style_fit", "verdict": "concern",
                                                      "claim": "Feels risky.", "job_evidence": "", "profile_evidence": ""}]))

    def test_missing_dimension_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "five dimensions"):
            validate_assessment(assessment(dimension_scores={"capability_fit": 5}))


class ProviderTests(unittest.TestCase):
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 10},
                             "note": {"type": "string", "maxLength": 50}},
              "required": ["score", "note"]}

    def call_anthropic(self, response):
        seen = {}

        def fake_post(url, headers, payload, timeout):
            seen.update(url=url, headers=headers, payload=payload)
            return response

        with patch.object(providers, "_post", fake_post):
            provider = providers.AnthropicProvider("sk-ant-test", "claude-opus-5")
            return provider.complete_json("SYSTEM", "USER", self.schema, "job_assessment"), seen

    def test_request_shape_matches_the_messages_api(self):
        (data, usage), seen = self.call_anthropic({
            "content": [{"type": "text", "text": '{"score": 7, "note": "ok"}'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12000, "output_tokens": 800, "cache_read_input_tokens": 11000},
        })
        self.assertTrue(seen["url"].endswith("/messages"))
        self.assertEqual("sk-ant-test", seen["headers"]["x-api-key"])
        self.assertEqual(providers.ANTHROPIC_VERSION, seen["headers"]["anthropic-version"])
        self.assertIn("max_tokens", seen["payload"], "the Messages API requires max_tokens")
        self.assertEqual({"score": 7, "note": "ok"}, data)
        self.assertEqual(11000, usage["cached_input_tokens"])

    def test_static_prefix_carries_the_cache_breakpoint(self):
        _, seen = self.call_anthropic({"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"})
        system = seen["payload"]["system"]
        self.assertEqual([{"type": "text", "text": "SYSTEM",
                           "cache_control": {"type": "ephemeral", "ttl": "1h"}}], system)
        self.assertEqual("USER", seen["payload"]["messages"][0]["content"],
                         "the volatile posting must stay out of the cached prefix")

    def test_unsupported_schema_keywords_are_stripped(self):
        _, seen = self.call_anthropic({"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"})
        sent = seen["payload"]["output_config"]["format"]["schema"]
        self.assertNotIn("minimum", sent["properties"]["score"])
        self.assertNotIn("maxLength", sent["properties"]["note"])
        self.assertFalse(sent["additionalProperties"], "additionalProperties: false must survive")
        self.assertEqual(["score", "note"], sent["required"])
        self.assertIn("minimum", self.schema["properties"]["score"], "the original schema must not be mutated")

    def test_refusal_and_truncation_raise_rather_than_parse(self):
        with self.assertRaisesRegex(ValueError, "declined"):
            self.call_anthropic({"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}})
        with self.assertRaisesRegex(ValueError, "max_tokens"):
            self.call_anthropic({"content": [{"type": "text", "text": "{"}], "stop_reason": "max_tokens"})

    def test_provider_selection_follows_the_keys(self):
        cases = [
            ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": ""}, "anthropic", "claude-opus-5"),
            ({"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "o"}, "openai", "gpt-5-mini"),
            ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}, "anthropic", "claude-opus-5"),
            ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "JOB_INBOX_PROVIDER": "openai"}, "openai", "gpt-5-mini"),
            ({"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": ""}, "", "gpt-5-mini"),
        ]
        for env, expected, model in cases:
            full = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "JOB_INBOX_PROVIDER": "", "JOB_INBOX_LLM_MODEL": "", **env}
            with self.subTest(env=env), patch.dict("os.environ", full, clear=False):
                self.assertEqual(expected, providers.configured_provider())
                self.assertEqual(model, providers.default_model())
                self.assertEqual(bool(expected), providers.build() is not None)

    def test_env_file_fills_only_what_the_environment_left_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# a comment\nANTHROPIC_API_KEY='sk-ant-from-file'\n"
                            "OPENAI_API_KEY=sk-from-file\nJOB_INBOX_LLM_MODEL=\nbroken line\n", encoding="utf-8")
            # An exported key wins, and an explicitly emptied one stays empty:
            # "off" is a decision the file must not quietly reverse.
            with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                filled = providers.load_env_file(path)
                self.assertEqual(["ANTHROPIC_API_KEY"], filled)
                self.assertEqual("sk-ant-from-file", os.environ["ANTHROPIC_API_KEY"])
                self.assertEqual("", os.environ["OPENAI_API_KEY"])
                self.assertNotIn("JOB_INBOX_LLM_MODEL", filled, "an empty value is not a setting")
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_missing_env_file_is_not_an_error(self):
        self.assertEqual([], providers.load_env_file(Path("no-such-directory") / ".env"))

    def test_anthropic_key_alone_activates_the_advisor(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "", "JOB_INBOX_PROVIDER": "", "JOB_INBOX_ADVISOR": ""}
        with patch.dict("os.environ", env, clear=False), patch.object(candidate, "is_available", lambda: True):
            self.assertEqual("findings", advise.mode())
            self.assertTrue(advise.is_active())
            self.assertIsInstance(advise.get_default_advisor(), advise.ModelAdvisor)


class FunnelTests(unittest.TestCase):
    """The funnel decides what gets paid for, so its edges are worth pinning."""

    def setUp(self):
        self.policy = funnel.FunnelPolicy.from_env()

    def test_titles_that_pass_and_titles_that_do_not(self):
        wanted = ["Senior Frontend Engineer", "Full-Stack Developer (React)",
                  "Software Engineer, Platform", "Tech Lead — TypeScript"]
        unwanted = ["Business Development Manager", "Account Executive, Commercial",
                    "Controller/ Unterstützung Buchhaltung", "Visual Designer, Web",
                    "Junior Frontend Developer", "Technical Recruiter"]
        for title in wanted:
            with self.subTest(title=title):
                self.assertTrue(self.policy.wants_title(title))
        for title in unwanted:
            with self.subTest(title=title):
                self.assertFalse(self.policy.wants_title(title))

    def test_shortlist_caps_spend_and_takes_the_best(self):
        policy = funnel.FunnelPolicy(include=self.policy.include, exclude=self.policy.exclude,
                                     advise_max=2, advise_min_score=4.5)
        scored = [("low", 3.0), ("best", 8.1), ("mid", 5.0), ("good", 7.2)]
        chosen, skipped = funnel.shortlist(scored, policy)
        self.assertEqual(["best", "good"], chosen)
        self.assertEqual(2, skipped, "the gate and the cap both have to be reported")

    def test_nothing_clears_an_impossible_gate(self):
        policy = funnel.FunnelPolicy(include=self.policy.include, exclude=self.policy.exclude,
                                     advise_max=5, advise_min_score=9.9)
        chosen, skipped = funnel.shortlist([("a", 7.0), ("b", 8.0)], policy)
        self.assertEqual([], chosen)
        self.assertEqual(2, skipped)

    def test_regional_reposts_collapse_to_one_job(self):
        """One remote opening appeared 11 times, once per voivodeship."""
        key = funnel.duplicate_key("Inwedo Sp. z o.o.", "Remote Senior React Developer")
        self.assertEqual(key, funnel.duplicate_key("inwedo sp. z o.o.", "  Remote Senior   React Developer "))
        self.assertNotEqual(key, funnel.duplicate_key("Other Co", "Remote Senior React Developer"))
        self.assertNotEqual(key, funnel.duplicate_key("Inwedo Sp. z o.o.", "Senior Angular Developer"))

    def test_a_pasted_job_is_never_filtered_by_its_title(self):
        with tempfile.TemporaryDirectory() as directory:
            db = JobDatabase(Path(directory) / "test.sqlite3")
            jobs, report = ingest_jobs(db, "manual", {
                "title": "Business Development Manager",
                "description": "Senior Vue TypeScript engineer improving a data-heavy reporting application."}, "A")
            self.assertEqual(1, len(jobs), "pasting a job is deliberate; the funnel must not second-guess it")
            self.assertEqual(0, report.title_rejected)


class NoFluffJobsTests(unittest.TestCase):
    detail = {
        "title": "Frontend Engineer (React)", "company": {"name": "Mindbox"},
        "essentials": {"originalSalary": {"currency": "PLN", "types": {"b2b": {"period": "Day", "range": [1450.0, 1650.0]}}}},
        "requirements": {"musts": [{"value": "React"}, {"value": "TypeScript"}],
                         "nices": [{"value": "GraphQL"}]},
        "specs": {"remote": True, "dailyStandup": "yes"},
        "location": {"places": [{"city": "Kraków"}]},
    }

    def test_musts_and_nices_become_sections_the_parser_understands(self):
        document = NoFluffJobsAdapter._document(self.detail)
        self.assertEqual("mandatory", importance(document, "react").kind)
        self.assertEqual("optional", importance(document, "graphql").kind,
                         "a 'nice to have' must not be read as a requirement")

    def test_daily_rate_is_converted_to_a_monthly_band(self):
        meta = NoFluffJobsAdapter._salary(self.detail["essentials"])
        self.assertEqual("PLN", meta["currency"])
        self.assertEqual(1450.0 * 21, meta["monthly_min"])
        self.assertEqual(1650.0 * 21, meta["monthly_max"])
        self.assertNotIn("rate_min", meta, "a daily PLN band is not an hourly USD rate")

    def test_hourly_bands_stay_hourly(self):
        meta = NoFluffJobsAdapter._salary({"originalSalary": {"currency": "PLN", "types": {"b2b": {"period": "Hour", "range": [90.0, 120.0]}}}})
        self.assertEqual((90.0, 120.0), (meta["rate_min"], meta["rate_max"]))

    def test_missing_or_undisclosed_salary_is_not_invented(self):
        self.assertEqual({}, NoFluffJobsAdapter._salary({}))
        self.assertEqual({"currency": "PLN"}, NoFluffJobsAdapter._salary({"originalSalary": {"currency": "PLN", "types": {}}}))

    def test_bad_category_or_region_is_rejected_before_any_request(self):
        for payload in ({"category": "../etc"}, {"category": "front end"}, {"region": "PL!"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                NoFluffJobsAdapter().ingest(payload)


class OnDemandAssessmentTests(unittest.TestCase):
    """Feeds arrive scored but unassessed; a click pays for one reading."""

    def ingest_one(self, db):
        jobs, _ = ingest_jobs(db, "manual", {
            "title": "Senior Frontend Engineer",
            "description": "Senior React TypeScript engineer building data-heavy reporting dashboards. " * 4}, "B")
        return jobs[0]

    def test_a_feed_row_carries_no_assessment_until_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            db = JobDatabase(Path(directory) / "test.sqlite3")
            row = self.ingest_one(db)
            self.assertEqual("off", row["evaluation"]["assessment_mode"])
            self.assertEqual({}, row["evaluation"]["match"])

    def test_assessing_on_demand_updates_the_stored_row(self):
        class Stub:
            def assess(self, description):
                return assessment()

        with tempfile.TemporaryDirectory() as directory, \
             patch.dict("os.environ", {"JOB_INBOX_ADVISOR": "findings"}, clear=False), \
             patch.object(advise, "get_default_advisor", lambda: Stub()):
            db = JobDatabase(Path(directory) / "test.sqlite3")
            row = self.ingest_one(db)
            updated = assess_job(db, row["id"])
            self.assertEqual("findings", updated["evaluation"]["assessment_mode"])
            self.assertEqual("strong", updated["evaluation"]["match"]["verdict"])
            self.assertTrue(updated["evaluation"]["draft_message"])
            self.assertEqual(updated["evaluation"]["decision"], db.get_job(row["id"])["decision"],
                             "the row on disk must agree with what was returned")

    def test_without_an_advisor_it_says_so_instead_of_pretending(self):
        with tempfile.TemporaryDirectory() as directory:
            db = JobDatabase(Path(directory) / "test.sqlite3")
            row = self.ingest_one(db)
            with self.assertRaisesRegex(ValueError, "No advisor available"):
                assess_job(db, row["id"])

    def test_an_advisor_failure_does_not_overwrite_a_good_row(self):
        class Broken:
            def assess(self, description):
                raise urllib.error.URLError("no route to host")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(advise, "get_default_advisor", lambda: Broken()):
            db = JobDatabase(Path(directory) / "test.sqlite3")
            row = self.ingest_one(db)
            with self.assertRaisesRegex(ValueError, "URLError"):
                assess_job(db, row["id"])
            self.assertEqual(row["evaluation"]["overall_score"], db.get_job(row["id"])["evaluation"]["overall_score"])

    def test_a_request_may_lower_the_spend_cap_but_not_raise_it(self):
        with patch.dict("os.environ", {"JOB_INBOX_ADVISE_MAX": "5"}, clear=False):
            self.assertEqual(0, funnel.FunnelPolicy.from_env({"advise_max": 0}).advise_max)
            self.assertEqual(2, funnel.FunnelPolicy.from_env({"advise_max": 2}).advise_max)
            self.assertEqual(5, funnel.FunnelPolicy.from_env({"advise_max": 99}).advise_max,
                             "a request body must not be able to raise the configured ceiling")
            self.assertEqual(5, funnel.FunnelPolicy.from_env({}).advise_max)


class AdapterTests(unittest.TestCase):
    def test_manual_and_file_adapters_share_contract(self):
        payload = {"description": "Senior Vue Engineer\nBuild reporting applications.", "content_hash": "abc"}
        manual = ManualPasteAdapter().ingest(payload)[0]
        file_job = FileAdapter().ingest({**payload, "filename": "job.txt"})[0]
        self.assertEqual("manual", manual.source)
        self.assertEqual("file", file_job.source)
        self.assertEqual(manual.description, file_job.description)

    def test_greenhouse_rejects_arbitrary_urls_and_bad_tokens(self):
        with self.assertRaises(ValueError):
            GreenhouseAdapter().ingest({"board": "https://example.com/private"})

    def test_greenhouse_decodes_then_strips_html(self):
        self.assertEqual("Who we are", GreenhouseAdapter._plain_text("&lt;h2&gt;Who we are&lt;/h2&gt;"))


class DatabaseTests(unittest.TestCase):
    def test_ingestion_persists_evaluation_and_preparation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            db = JobDatabase(Path(directory) / "test.sqlite3")
            jobs, _ = ingest_jobs(db, "manual", {"title": "Vue role", "description": "Senior Vue TypeScript engineer improving a data-heavy application."}, "A")
            self.assertEqual(1, len(jobs))
            stored = db.get_job(jobs[0]["id"])
            self.assertIn(stored["decision"], {"APPLY", "MAYBE", "SKIP"})
            package = db.prepare_application(stored["id"])
            self.assertIn("never sends applications", package["submission"])
            self.assertEqual("prepared", db.get_job(stored["id"])["workflow_status"])

    def test_summary_and_filters(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            db = JobDatabase(Path(directory) / "test.sqlite3")
            ingest_jobs(db, "manual", {"description": "Senior Vue Nuxt TypeScript role improving a mature application."}, "A")
            self.assertEqual(1, db.summary()["jobs_found"])
            self.assertEqual(1, len(db.list_jobs("today")))


if __name__ == "__main__":
    unittest.main()
