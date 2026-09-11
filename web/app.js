const state = { filter: "today", source: "manual" };
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function list(items, empty = "None detected.") {
  const values = items?.length ? items : [empty];
  return `<ul>${values.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function metadataChips(job) {
  const meta = job.metadata || {};
  const chips = [`Track ${job.track}`, job.source, job.workflow_status === "prepared" ? "Prepared" : "New"];
  if (meta.freshness_minutes !== undefined) {
    const minutes = Number(meta.freshness_minutes);
    chips.push(minutes < 60 ? `${minutes}m old` : minutes < 1440 ? `${Math.round(minutes / 60)}h old` : `${Math.round(minutes / 1440)}d old`);
  }
  if (meta.proposals !== undefined) chips.push(`${meta.proposals}${meta.proposals >= 50 ? "+" : ""} proposals`);
  if (meta.interviewing !== undefined) chips.push(`${meta.interviewing} interviewing`);
  if (meta.rate_min !== undefined) chips.push(`$${meta.rate_min}–${meta.rate_max ?? meta.rate_min}/h`);
  if (meta.location) chips.push(meta.location);
  return chips.map(item => `<span class="chip">${escapeHtml(item)}</span>`).join("");
}

function detailSection(title, content) {
  return `<section class="detail-section"><h4>${escapeHtml(title)}</h4>${content}</section>`;
}

// A stored source_url is attacker-controlled: it arrives from a pasted payload
// or from an imported board. Only http(s) may reach an href.
function safeUrl(value = "") {
  try {
    const url = new URL(String(value), window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function findingList(findings = []) {
  if (!findings.length) return "";
  return `<ul class="findings">${findings.map(item => `
    <li class="finding ${escapeHtml(item.verdict)}">
      <strong>${escapeHtml(item.claim)}</strong>
      <span class="finding-dimension">${escapeHtml(String(item.dimension).replace(/_/g, " "))}</span>
      <blockquote>posting: ${escapeHtml(item.job_evidence)}</blockquote>
      <blockquote>profile: ${escapeHtml(item.profile_evidence)}</blockquote>
    </li>`).join("")}</ul>`;
}

function mitigationList(mitigations = []) {
  if (!mitigations.length) return "";
  return `<ul class="mitigations">${mitigations.map(item =>
    `<li><strong>${escapeHtml(item.concern)}</strong><span>${escapeHtml(item.how_to_address)}</span></li>`).join("")}</ul>`;
}

function jobCard(job) {
  const e = job.evaluation;
  const scores = e.scores;
  const blockerContent = list(e.hard_blockers);
  return `<article class="job-card" data-decision="${e.decision}">
    <div class="card-top">
      <div class="card-title"><span class="decision ${e.decision}">${e.decision}</span><div><h3>${escapeHtml(job.title)}</h3><p class="company">${escapeHtml(job.company || "Company not specified")}</p></div></div>
      <div class="score"><strong>${e.overall_score}</strong><span>overall / 10</span></div>
    </div>
    ${e.match?.verdict ? `<div class="match ${escapeHtml(e.match.verdict)}">
      <p class="match-verdict">Match: <strong>${escapeHtml(e.match.verdict)}</strong></p>
      <p><span>For you</span> ${escapeHtml(e.match.for_candidate)}</p>
      <p><span>For them</span> ${escapeHtml(e.match.for_employer)}</p>
      ${e.match.decisive_factor ? `<p class="decisive"><span>Decisive</span> ${escapeHtml(e.match.decisive_factor)}</p>` : ""}
    </div>` : ""}
    <div class="metrics">
      ${Object.entries(scores).map(([name, value]) =>
        `<div class="metric"><span>${escapeHtml(name.replace(/_/g, " "))}</span><strong>${escapeHtml(String(value))}/10</strong></div>`).join("")}
    </div>
    <div class="chips">${metadataChips(job)}</div>
    <ul class="card-why">${e.why.slice(0, 4).map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
    <div class="card-actions">
      ${e.assessment_mode === "off" || !e.assessment_mode
        ? `<button class="assess-button" data-assess="${job.id}">Assess this one</button>`
        : ""}
      <button class="prepare-button" data-prepare="${job.id}">Prepare application</button>
      ${safeUrl(job.source_url) ? `<a class="secondary-button" href="${escapeHtml(safeUrl(job.source_url))}" target="_blank" rel="noreferrer">Source ↗</a>` : ""}
    </div>
    <details class="details">
      <summary>Full decision details</summary>
      <div class="detail-grid">
        ${detailSection("WHY", list(e.why))}
        ${e.working_style?.how_this_role_runs ? detailSection("HOW THIS ROLE RUNS", `
          <p>${escapeHtml(e.working_style.how_this_role_runs)}</p>
          ${e.working_style.suits_him?.length ? `<p class="style-label">Suits you</p>${list(e.working_style.suits_him)}` : ""}
          ${e.working_style.drains_him?.length ? `<p class="style-label">Drains you</p>${list(e.working_style.drains_him)}` : ""}`) : ""}
        ${e.not_my_strengths?.length ? detailSection("NOT YOUR STRENGTHS HERE", list(e.not_my_strengths)) : ""}
        ${e.findings?.length ? detailSection("EVIDENCE", findingList(e.findings)) : ""}
        ${e.mitigations?.length ? detailSection("HOW TO ADDRESS", mitigationList(e.mitigations)) : ""}
        ${e.clarifying_questions?.length ? detailSection("WORTH ASKING", list(e.clarifying_questions)) : ""}
        ${e.model_opinion && e.model_opinion.agrees === false
          ? detailSection("SECOND OPINION", `<p>The advisor would say <strong>${escapeHtml(e.model_opinion.decision)}</strong> (${escapeHtml(String(e.model_opinion.score))}/10); the rules say <strong>${escapeHtml(e.decision)}</strong>. ${escapeHtml(e.model_opinion.reasoning || "")}</p>`)
          : ""}
        ${e.assessment_note ? detailSection("ADVISOR UNAVAILABLE", `<p>${escapeHtml(e.assessment_note)} Scored deterministically instead.</p>`) : ""}
        ${detailSection("ACTUAL WORK", list(e.actual_work_shape))}
        ${detailSection("GAPS", list(e.gaps))}
        ${detailSection("RISKS", list(e.risks))}
        ${detailSection("HARD BLOCKERS", blockerContent)}
        ${detailSection("COMPENSATION / RATE", `<p>${escapeHtml(e.suggested_compensation)}</p>`)}
        ${detailSection("PROPOSAL ANGLE", list(e.proposal_positioning_angle))}
        ${detailSection("CAREER CAPITAL", `<p><strong>${escapeHtml(e.career_capital.level)}</strong> — ${escapeHtml(e.career_capital.why)}</p>`)}
      </div>
    </details>
  </article>`;
}

async function loadJobs() {
  const listElement = $("#job-list");
  listElement.innerHTML = `<div class="empty-state"><p>Loading…</p></div>`;
  try {
    const data = await api(`/api/jobs?filter=${state.filter}`);
    listElement.innerHTML = data.jobs.length ? data.jobs.map(jobCard).join("") : $("#empty-template").innerHTML;
    $$("#filters button").forEach(button => { $("span", button).textContent = data.counts[button.dataset.filter] ?? 0; });
    $$('[data-prepare]').forEach(button => button.addEventListener("click", () => prepare(button.dataset.prepare)));
    $$('[data-assess]').forEach(button => button.addEventListener("click", () => assess(button, button.dataset.assess)));
  } catch (error) {
    listElement.innerHTML = `<div class="notice error">${escapeHtml(error.message)}</div>`;
  }
}

async function loadSummary() {
  const data = await api("/api/summary");
  $("#jobs-found").textContent = data.jobs_found;
  $("#auto-skipped").textContent = data.auto_skipped;
  $("#worth-reviewing").textContent = data.worth_reviewing;
  $("#top-recommendations").innerHTML = data.top_recommendations.length
    ? data.top_recommendations.map(item => `<li>${escapeHtml(item.title)} <small>${item.decision} ${item.overall_score}</small></li>`).join("")
    : "<li>Nothing ranked yet</li>";
}

let noticeTimer;

function notice(message, isError = false, holdMs = 6500) {
  const element = $("#notice");
  element.textContent = message;
  element.className = `notice${isError ? " error" : ""}`;
  element.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { element.hidden = true; }, holdMs);
}

// The only place in the interface that spends money, and it takes one click and
// a minute or two. Say what is happening and count it out loud.
async function assess(button, jobId) {
  const started = Date.now();
  const tick = setInterval(() => {
    button.textContent = `Reading… ${Math.round((Date.now() - started) / 1000)}s`;
  }, 1000);
  button.disabled = true;
  button.textContent = "Reading…";
  notice("Reading this posting against your profile: match, evidence and a draft reply. A minute or two.", false, 240000);
  try {
    const job = await api(`/api/jobs/${jobId}/assess`, { method: "POST", body: "{}" });
    const verdict = job.evaluation?.match?.verdict;
    notice(`${job.evaluation.decision} ${job.evaluation.overall_score}/10${verdict ? ` · match ${verdict}` : ""}`);
    await Promise.all([loadJobs(), loadSummary().catch(() => {})]);
  } catch (error) {
    notice(error.message, true);
    clearInterval(tick);
    button.disabled = false;
    button.textContent = "Assess this one";
  }
  clearInterval(tick);
}

async function prepare(jobId) {
  try {
    const pack = await api(`/api/jobs/${jobId}/prepare`, { method: "POST", body: "{}" });
    state.preparingJobId = jobId;
    $("#prepare-title").textContent = pack.title;
    $("#prepare-content").innerHTML = `
      <p><strong>CV positioning:</strong> ${escapeHtml(pack.positioning || "not set")}</p>
      ${pack.draft_message ? `
        <h3>Draft message</h3>
        <textarea id="draft-message" class="draft" rows="10">${escapeHtml(pack.draft_message)}</textarea>
        <div class="draft-actions">
          <button type="button" id="copy-draft" class="secondary-button">Copy draft</button>
          <span class="field-help">Review and edit before sending. Nothing is sent from here.</span>
        </div>` : ""}
      ${pack.mitigations?.length ? `<h3>How to address the concerns</h3>${mitigationList(pack.mitigations)}` : ""}
      ${pack.clarifying_questions?.length ? `<h3>Worth asking</h3>${list(pack.clarifying_questions)}` : ""}
      <h3>Proposal angle</h3>${list(pack.proposal_angle)}
      <h3>Gaps to address</h3>${list(pack.gaps_to_address)}
      <h3>Checklist</h3><ol class="prepare-list">${pack.checklist.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ol>`;
    $("#copy-draft")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("#draft-message").value);
        notice("Draft copied to the clipboard.");
      } catch {
        $("#draft-message").select();
        notice("Select-all applied; copy with Ctrl+C.", true);
      }
    });
    $("#prepare-dialog").showModal();
    await loadJobs();
  } catch (error) { notice(error.message, true); }
}

async function submitIngestion(event) {
  event.preventDefault();
  const button = $("#ingest-submit");
  button.disabled = true;
  // The advisor reads the whole posting against the profile and drafts a
  // message, which takes a minute or two. A frozen label reads as a hung app,
  // so count the wait out loud.
  const started = Date.now();
  const tick = setInterval(() => {
    const seconds = Math.round((Date.now() - started) / 1000);
    button.textContent = seconds < 5 ? "Evaluating…" : `Evaluating… ${seconds}s`;
  }, 1000);
  button.textContent = "Evaluating…";
  notice(state.source === "manual" || state.source === "file"
    ? "Reading the posting against your profile and drafting a reply. This usually takes a minute or two."
    : "Fetching and scoring the board. No model calls, so this costs nothing — assess the ones worth reading afterwards.",
    false, 180000);
  try {
    let payload = { track: $("#track").value };
    if (state.source === "manual") {
      payload = { ...payload, title: $("#manual-title").value, company: $("#manual-company").value, description: $("#manual-description").value };
    } else if (state.source === "file") {
      const file = $("#job-file").files[0];
      if (!file) throw new Error("Choose a text file first.");
      payload = { ...payload, filename: file.name, description: await file.text() };
    } else if (state.source === "nofluffjobs") {
      // Feeds arrive scored but unassessed: fetching is free, reading is not.
      payload = {
        ...payload, advise_max: 0,
        category: $("#nfj-category").value,
        seniority: $("#nfj-seniority").value ? $("#nfj-seniority").value.split(",") : [],
        limit: Number($("#nfj-limit").value),
      };
    } else {
      payload = { ...payload, board: $("#greenhouse-board").value, limit: Number($("#greenhouse-limit").value) };
    }
    const result = await api(`/api/ingest/${state.source}`, { method: "POST", body: JSON.stringify(payload) });
    $("#ingest-dialog").close();
    notice(result.funnel && state.source !== "manual" && state.source !== "file"
      ? `${result.count} added. ${result.funnel}`
      : `${result.count} job${result.count === 1 ? "" : "s"} evaluated and added.`, false, 12000);
    await Promise.all([loadJobs(), loadSummary()]);
  } catch (error) { notice(error.message, true); }
  finally { clearInterval(tick); button.disabled = false; button.textContent = "Evaluate and add"; }
}

function bind() {
  $("#today-label").textContent = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "short", day: "numeric" }).format(new Date());
  $("#add-job-button").addEventListener("click", () => $("#ingest-dialog").showModal());
  $$(".ingest-close").forEach(button => button.addEventListener("click", () => $("#ingest-dialog").close()));
  $("#prepare-close").addEventListener("click", () => $("#prepare-dialog").close());
  $("#ingest-form").addEventListener("submit", submitIngestion);
  $("#outcome-date").valueAsDate = new Date();
  $("#outcome-form").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      await api("/api/outcomes", { method: "POST", body: JSON.stringify({
        job_id: state.preparingJobId, stage: $("#outcome-stage").value,
        date: $("#outcome-date").value, notes: $("#outcome-notes").value
      }) });
      $("#prepare-dialog").close();
      notice("Outcome saved for later calibration.");
      await Promise.all([loadJobs(), loadSummary()]);
    } catch (error) { notice(error.message, true); }
  });
  $$("#filters button").forEach(button => button.addEventListener("click", async () => {
    $$("#filters button").forEach(item => item.classList.remove("active")); button.classList.add("active"); state.filter = button.dataset.filter; await loadJobs();
  }));
  $$(".source-tabs button").forEach(button => button.addEventListener("click", () => {
    $$(".source-tabs button").forEach(item => item.classList.remove("active")); button.classList.add("active"); state.source = button.dataset.source;
    $$(".source-panel").forEach(panel => { panel.hidden = panel.dataset.panel !== state.source; });
  }));
}

async function start() {
  bind();
  const settings = await api("/api/settings").catch(() => ({ llm_enabled: false, advisor_mode: "off" }));
  // "Off" has three different causes and the reason is what the reader needs.
  const advisor = { findings: "Advisor · rules decide", verdict: "Advisor · model decides" }[settings.advisor_mode]
    || (!settings.llm_enabled ? "Deterministic · no API key"
      : !settings.profile_ready ? "Deterministic · no candidate profile"
      : "Deterministic · advisor switched off");
  $("#llm-status").textContent = settings.llm_enabled ? `${advisor} · ${settings.llm_model}` : advisor;
  await Promise.all([loadJobs(), loadSummary().catch(error => notice(error.message, true))]);
  registerWebMcp();
}

function registerWebMcp() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const report = error => console.warn("WebMCP tool failed", error);
  try {
    Promise.resolve(context.registerTool({
      name: "list_job_inbox",
      title: "List job inbox",
      description: "List locally evaluated jobs in the selected Today, Apply, Maybe, or Skip view.",
      inputSchema: { type: "object", properties: { filter: { type: "string", enum: ["today", "apply", "maybe", "skip"] } }, required: ["filter"], additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: true },
      async execute(input) {
        const result = await api(`/api/jobs?filter=${input.filter}`);
        return { jobs: result.jobs.map(job => ({ id: job.id, title: job.title, company: job.company, decision: job.decision, score: job.overall_score })) };
      }
    })).catch(report);
    Promise.resolve(context.registerTool({
      name: "prepare_job_application",
      title: "Prepare job application",
      description: "Create a local preparation checklist and positioning package for one job. Does not submit an application.",
      inputSchema: { type: "object", properties: { jobId: { type: "string", minLength: 1 } }, required: ["jobId"], additionalProperties: false },
      annotations: { readOnlyHint: false, untrustedContentHint: true },
      async execute(input) {
        const pack = await api(`/api/jobs/${input.jobId}/prepare`, { method: "POST", body: "{}" });
        await Promise.all([loadJobs(), loadSummary()]);
        return pack;
      }
    })).catch(report);
  } catch (error) { report(error); }
}

start();
