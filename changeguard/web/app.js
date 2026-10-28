"use strict";

/* ---------- tiny helpers ---------- */

class Raw { constructor(text) { this.text = text; } }
const raw = (text) => new Raw(text);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));
const piece = (value) => {
  if (value instanceof Raw) return value.text;
  if (Array.isArray(value)) return value.map(piece).join("");
  if (value === null || value === undefined || value === false) return "";
  return escapeHtml(value);
};
/** Template tag: interpolated values are escaped unless wrapped in raw() or produced by html``. */
const html = (strings, ...values) => raw(strings.reduce((out, s, i) => out + s + (i < values.length ? piece(values[i]) : ""), ""));
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const main = $("#main");
const label = (value) => String(value || "").replaceAll("_", " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
const badgeClass = (value) => String(value || "").toLowerCase().replace(/[^a-z]/g, "");
const badge = (value, cls) => html`<span class="badge ${cls ?? badgeClass(value)}">${label(value)}</span>`;
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

function when(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso || "";
  const minutes = Math.round((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)} h ago`;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

async function api(path, body) {
  const options = { headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-ChangeGuard"] = "1";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Could not reach the ChangeGuard server. Is `changeguard ui` still running?");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

let toastTimer;
function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, 3500);
}

function setPage(title, content) {
  $("#topbar-title").textContent = title;
  document.title = title === "ChangeGuard" ? title : `${title} · ChangeGuard`;
  main.innerHTML = piece(content);
}

const errorBox = (message) => html`<div class="alert error" role="alert">${message}</div>`;
const loading = (message = "Loading…") => html`<div class="busy"><span class="spinner"></span><span>${message}</span></div>`;

function showError(container, error) {
  container.innerHTML = piece(errorBox(error.message || String(error)));
  container.hidden = false;
}

/** Disable a form while `task` runs, show progress text, and report errors inside the form. */
async function busyForm(form, message, task) {
  const status = $(".form-status", form);
  const buttons = $$("button", form);
  buttons.forEach((b) => { b.disabled = true; });
  status.hidden = false;
  status.innerHTML = piece(loading(message));
  try {
    await task();
    status.hidden = true;
  } catch (error) {
    showError(status, error);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

/* ---------- routing ---------- */

const routes = {
  "": viewHome,
  analyze: viewAnalyze,
  reports: viewReports,
  report: viewReport,
  projects: viewProjects,
  evidence: viewEvidence,
  settings: viewSettings,
  guide: viewGuide,
  demo: viewDemo,
};

function parseHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const [name, ...rest] = path.split("/");
  return { name, arg: rest.join("/"), params: new URLSearchParams(query) };
}

async function route() {
  const { name, arg, params } = parseHash();
  const view = routes[name] || viewHome;
  const nav = name === "report" || name === "demo" ? "reports" : (name || "home");
  $$(".sidebar nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  $("#sidebar").classList.remove("open");
  $("#menu-button").setAttribute("aria-expanded", "false");
  window.scrollTo(0, 0);
  try {
    await view(arg, params);
  } catch (error) {
    setPage("Error", html`${errorBox(error.message || String(error))}<a class="button" href="#/">Back to dashboard</a>`);
  }
}

window.addEventListener("hashchange", route);
$("#menu-button").addEventListener("click", () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#menu-button").setAttribute("aria-expanded", String(open));
});

/* ---------- dashboard ---------- */

async function viewHome() {
  setPage("Dashboard", loading());
  const [status, reports] = await Promise.all([api("/api/status"), api("/api/reports?limit=8")]);
  const firstRun = status.reports === 0 && status.projects === 0;
  const checks = status.checks.map((check) => html`
    <li><span class="dot ${check.ok ? "ok" : check.optional ? "warn" : "bad"}"></span>
      <div><strong>${check.name}</strong><div class="muted small">${check.detail}</div></div></li>`);
  setPage("Dashboard", html`
    <div class="page-head">
      <div><h1>Dashboard</h1><p class="muted">See what a change touches, how risky it is, and what to test, before you merge.</p></div>
      <div class="row"><a class="button" href="#/projects">Add project</a><a class="button primary" href="#/analyze">New analysis</a></div>
    </div>
    ${firstRun ? html`
      <div class="card">
        <h2>Welcome! Get started in three steps</h2>
        <ol class="steps">
          <li><h3>Add a project</h3><p>Point ChangeGuard at a git repository on this computer. It detects languages and frameworks and sets up an evidence folder. <a href="#/projects">Add a project →</a></p></li>
          <li><h3>Analyze a change</h3><p>Pick your latest commit, a branch, or paste a GitHub pull request link. <a href="#/analyze">New analysis →</a></p></li>
          <li><h3>Read the report</h3><p>Risk score, affected code and API callers, related past incidents, and what to test. Not ready to try your own code? <a href="#/demo">Open a sample report →</a></p></li>
        </ol>
        <p class="muted small">The <a href="#/guide">Guide</a> explains every feature, including automatic reports on every pull request.</p>
      </div>` : ""}
    <div class="grid three">
      <div class="card"><div class="stat">${status.projects}</div><div class="stat-label">projects</div></div>
      <div class="card"><div class="stat">${status.reports}</div><div class="stat-label">saved reports</div></div>
      <div class="card"><div class="stat">${label(status.provider)}</div><div class="stat-label">AI provider · <a href="#/settings">change</a></div></div>
    </div>
    <div class="grid two">
      <div class="card">
        <div class="row"><h2>Recent reports</h2><span class="spacer"></span><a class="small" href="#/reports">All reports</a></div>
        ${reportTable(reports, "No reports yet. Run your first analysis to see it here.")}
      </div>
      <div class="card">
        <h2>Setup check</h2>
        <ul class="checklist">${checks}</ul>
        <p class="hint">Yellow items are optional. They unlock AI investigation, private repositories and sandboxed tests.</p>
      </div>
    </div>`);
  $$("tr[data-report]").forEach(bindReportRow);
}

function reportTable(reports, emptyText, { showSummary = false } = {}) {
  if (!reports.length) return html`<div class="empty">${emptyText}</div>`;
  return html`<div class="table-wrap"><table>
    <thead><tr><th>Change</th><th>Risk</th>${showSummary ? html`<th>Summary</th>` : ""}<th>When</th></tr></thead>
    <tbody>${reports.map((r) => html`
      <tr class="clickable" data-report="${r.id}" tabindex="0">
        <td><a href="#/report/${r.id}">${r.target}</a><div class="muted small">${r.kind === "pr" ? "Pull request" : "Local changes"}</div></td>
        <td>${r.risk_level ? badge(r.risk_level) : badge(r.status)}${r.risk_score != null ? html`<div class="muted small">${Math.round(r.risk_score)}/100</div>` : ""}</td>
        ${showSummary ? html`<td class="small">${r.summary}</td>` : ""}
        <td class="muted small">${when(r.created_at)}</td>
      </tr>`)}</tbody></table></div>`;
}

function bindReportRow(row) {
  const open = () => { location.hash = `#/report/${row.dataset.report}`; };
  row.addEventListener("click", (event) => { if (!event.target.closest("a")) open(); });
  row.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
}

/* ---------- new analysis ---------- */

async function viewAnalyze(_arg, params) {
  const [projects, settings] = await Promise.all([api("/api/projects"), api("/api/settings")]);
  const tab = params.get("tab") === "local" || (params.has("path") && params.get("tab") !== "pr") ? "local" : "pr";
  const aiReady = settings.provider === "gemini" ? settings.gemini_key_set : true;
  const options = (prefix) => html`
    <label class="check"><input type="checkbox" name="llm" ${aiReady ? "" : "disabled"}>
      <span>AI investigation <small>${aiReady
        ? `Uses ${label(settings.provider)} to read the change and explain the impact. Code from the change is sent to the AI provider.`
        : raw(`Add a Gemini API key in <a href="#/settings">Settings</a> to enable this.`)}</small></span></label>
    <label class="check"><input type="checkbox" name="verify" ${settings.sandbox?.ok === false ? "disabled" : ""}>
      <span>Run tests in a sandbox <small>${settings.sandbox?.ok === false
        ? `Unavailable: ${settings.sandbox.detail}.`
        : `Runs the matching pytest / npm test and type-check commands in an isolated, offline copy of the ${prefix}. Can take a few minutes.`}</small></span></label>`;
  const projectOptions = projects.map((p) => html`<option value="${p.path}">${p.name} (${p.repository})</option>`);
  setPage("New analysis", html`
    <div class="page-head"><div><h1>New analysis</h1><p class="muted">Analyze a GitHub pull request, or commits in a repository on this computer.</p></div></div>
    <div class="card">
      <div class="tabs" role="tablist">
        <button type="button" role="tab" data-tab="pr" class="${tab === "pr" ? "active" : ""}">Analyze Pull Request</button>
        <button type="button" role="tab" data-tab="local" class="${tab === "local" ? "active" : ""}">Local changes</button>
      </div>
      <form id="pr-form" ${tab === "pr" ? "" : "hidden"}>
        <div class="field">
          <label for="pr-input">Pull request</label>
          <input id="pr-input" name="pr" type="text" placeholder="https://github.com/owner/repo/pull/12  or  owner/repo#12" autocomplete="off" required>
          <div class="hint">Public repositories work right away. For private ones, run <code>gh auth login</code> or add a token in <a href="#/settings">Settings</a>.</div>
        </div>
        ${options("pull request")}
        <button class="button primary" type="submit">Analyze pull request</button>
        <div class="form-status" hidden></div>
      </form>
      <form id="local-form" ${tab === "local" ? "" : "hidden"}>
        <div class="field">
          <label for="local-path">Repository</label>
          ${projects.length ? html`<select id="local-project"><option value="">Choose a project…</option>${projectOptions}<option value="__other">Another folder…</option></select>` : ""}
          <input id="local-path" name="path" type="text" placeholder="/home/you/projects/my-app" autocomplete="off" ${projects.length ? "hidden" : ""}>
          <div class="hint">${projects.length ? "Projects you added on the Projects page." : raw(`Full path to a git repository. Tip: <a href="#/projects">add it as a project</a> to pick it from a list.`)}</div>
        </div>
        <div class="field">
          <label>What to compare</label>
          <div class="row" id="presets">
            <button class="button small" type="button" data-base="HEAD~1" data-head="HEAD">Latest commit</button>
            <button class="button small" type="button" data-preset="branch" hidden>This branch vs main</button>
          </div>
        </div>
        <div class="grid two">
          <div class="field"><label for="base">Base (before)</label><input id="base" name="base" type="text" value="HEAD~1" list="revisions" autocomplete="off"></div>
          <div class="field"><label for="head">Head (after)</label><input id="head" name="head" type="text" value="HEAD" list="revisions" autocomplete="off"></div>
        </div>
        <datalist id="revisions"></datalist>
        <div class="hint" id="revision-hint">Branch names, tags, commit IDs or expressions like HEAD~3 all work.</div>
        <div id="recent-commits"></div>
        <h3>Options</h3>
        ${options("repository")}
        <button class="button primary" type="submit">Analyze changes</button>
        <div class="form-status" hidden></div>
      </form>
    </div>`);

  $$(".tabs button").forEach((button) => button.addEventListener("click", () => {
    $$(".tabs button").forEach((b) => b.classList.toggle("active", b === button));
    $("#pr-form").hidden = button.dataset.tab !== "pr";
    $("#local-form").hidden = button.dataset.tab !== "local";
  }));

  $("#pr-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const text = form.pr.value.trim();
    const body = { llm: form.llm.checked, verify: form.verify.checked };
    const short = text.match(/^([\w.-]+\/[\w.-]+)#(\d+)$/);
    if (short) Object.assign(body, { repository: short[1], pull_number: Number(short[2]) });
    else if (/^https?:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+/.test(text)) body.url = text;
    else return showError($(".form-status", form), new Error("Enter a pull request link (https://github.com/owner/repo/pull/12) or owner/repo#12."));
    busyForm(form, "Fetching the pull request and analyzing it…", async () => {
      const result = await api("/api/analyze/pr", body);
      location.hash = `#/report/${result.report_id}`;
    });
  });

  const pathInput = $("#local-path");
  const select = $("#local-project");
  const currentPath = () => (select && select.value && select.value !== "__other" ? select.value : pathInput.value.trim());
  if (select) {
    select.addEventListener("change", () => {
      pathInput.hidden = select.value !== "__other";
      if (select.value === "__other") pathInput.focus();
      loadRevisions(currentPath());
    });
  }
  pathInput.addEventListener("change", () => loadRevisions(currentPath()));
  const initialPath = params.get("path");
  if (initialPath) {
    if (select && projects.some((p) => p.path === initialPath)) select.value = initialPath;
    else { if (select) select.value = "__other"; pathInput.hidden = false; pathInput.value = initialPath; }
    loadRevisions(initialPath);
  }
  $("#presets").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    $("#base").value = button.dataset.base;
    $("#head").value = button.dataset.head;
  });

  $("#local-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const path = currentPath();
    if (!path) return showError($(".form-status", form), new Error("Choose a project or enter a repository path."));
    busyForm(form, "Analyzing the changes… (AI investigation and tests can take a few minutes)", async () => {
      const result = await api("/api/analyze/local", {
        path, base: form.base.value.trim() || "HEAD~1", head: form.head.value.trim() || "HEAD",
        llm: form.llm.checked, verify: form.verify.checked,
      });
      location.hash = `#/report/${result.report_id}`;
    });
  });
}

async function loadRevisions(path) {
  const list = $("#revisions");
  const recent = $("#recent-commits");
  const branchButton = $('[data-preset="branch"]');
  if (!list) return;
  list.innerHTML = "";
  recent.innerHTML = "";
  branchButton.hidden = true;
  if (!path) return;
  try {
    const data = await api(`/api/revisions?path=${encodeURIComponent(path)}`);
    list.innerHTML = piece([
      ...data.branches.map((b) => html`<option value="${b}">branch</option>`),
      ...data.commits.map((c) => html`<option value="${c.short}">${c.subject}</option>`),
    ]);
    const mainBranch = data.branches.find((b) => b === "main" || b === "master");
    if (mainBranch) {
      branchButton.hidden = false;
      branchButton.dataset.base = mainBranch;
      branchButton.dataset.head = "HEAD";
      branchButton.textContent = `This branch vs ${mainBranch}`;
    }
    if (data.commits.length) {
      recent.innerHTML = piece(html`<details><summary class="small">Recent commits</summary><ul class="file-list small">
        ${data.commits.slice(0, 10).map((c) => html`<li><code>${c.short}</code> ${c.subject} <span class="muted">· ${c.author}, ${c.when}</span></li>`)}
      </ul></details>`);
    }
  } catch (error) {
    recent.innerHTML = piece(errorBox(error.message));
  }
}

/* ---------- reports ---------- */

async function viewReports(_arg, params) {
  const repository = params.get("repository") || "";
  const [reports, projects] = await Promise.all([
    api(`/api/reports?limit=200${repository ? `&repository=${encodeURIComponent(repository)}` : ""}`),
    api("/api/projects"),
  ]);
  const repos = [...new Set([...projects.map((p) => p.repository), ...reports.map((r) => r.repository)])].sort();
  setPage("Reports", html`
    <div class="page-head">
      <div><h1>Reports</h1><p class="muted">Every analysis is saved here, newest first.</p></div>
      <div class="row">
        <select id="repo-filter" aria-label="Filter by repository"><option value="">All repositories</option>
          ${repos.map((r) => html`<option value="${r}" ${r === repository ? "selected" : ""}>${r}</option>`)}</select>
        <a class="button" href="#/demo">Sample report</a>
      </div>
    </div>
    <div class="card">${reportTable(reports, "No reports yet.", { showSummary: true })}</div>`);
  $("#repo-filter").addEventListener("change", (event) => {
    location.hash = event.target.value ? `#/reports?repository=${encodeURIComponent(event.target.value)}` : "#/reports";
  });
  $$("tr[data-report]").forEach(bindReportRow);
}

async function viewReport(id) {
  setPage("Report", loading("Loading report…"));
  const report = await api(`/api/reports/${encodeURIComponent(id)}`);
  renderReport(report.payload, { ...report, saved: true });
}

async function viewDemo() {
  setPage("Sample report", loading());
  const payload = await api("/api/demo");
  renderReport(payload, { target: `${payload.repository}#${payload.pull_number}`, kind: "pr", demo: true });
}

function renderReport(payload, meta) {
  const a = payload.analysis || {};
  const risk = a.risk_assessment || {};
  const isPr = "pull_number" in payload;
  const subtitle = isPr
    ? html`Pull request <a href="${payload.url}" target="_blank" rel="noopener noreferrer">${payload.repository}#${payload.pull_number}</a>
        · <code>${(payload.base_sha || "").slice(0, 8)}..${(payload.head_sha || "").slice(0, 8)}</code>`
    : html`<code>${payload.repo_path}</code> · <code>${payload.base}..${payload.head}</code>`;
  const sections = [
    ["risk", "Risk"], ["changes", "Changes"], ["impact", "Impact"], ["api", "API"],
    ["history", "History"], ["verification", "Tests"], ["ai", "AI investigation"], ["next", "Next steps"],
  ];
  setPage(meta.target || "Report", html`
    ${meta.demo ? html`<div class="alert info">This is a saved sample analysis of a real pull request, to show what reports look like. Nothing was run just now.</div>` : ""}
    ${payload.head_changed ? html`<div class="alert warn">The pull request has new commits since this analysis. Run it again for the latest version.</div>` : ""}
    <div class="card">
      <div class="risk-head">
        <div>
          <div class="muted small">Risk</div>
          <div class="risk-score">${risk.score != null ? Math.round(risk.score) : "–"}<span class="muted small">/100</span></div>
        </div>
        <div>${badge(risk.overall_level || "unknown")}</div>
        <div class="spacer"></div>
        <div class="row">
          ${meta.saved ? html`
            <button class="button small" type="button" id="copy-md">Copy Markdown</button>
            <button class="button small" type="button" id="download-md">Download</button>
            <button class="button small" type="button" id="rerun">Run again</button>
            <button class="button small danger" type="button" id="delete-report">Delete</button>` : ""}
        </div>
      </div>
      <h1>${meta.target}</h1>
      <p class="muted small">${subtitle}${meta.created_at ? ` · ${when(meta.created_at)}` : ""}</p>
      <p>${a.change_summary}</p>
      <div class="form-status" id="report-status" hidden></div>
    </div>
    <nav class="section-nav" aria-label="Report sections">${sections.map(([key, text]) => html`<a href="#section-${key}" data-section="${key}">${text}</a>`)}</nav>
    ${riskSection(risk)}
    ${changesSection(a)}
    ${impactSection(a.blast_radius || {})}
    ${apiSection(a.api_impact)}
    ${historySection(a.historical_evidence)}
    ${verificationSection(a.verification_plan, a.verification_result)}
    ${aiSection(a.investigation_report, a.investigation_state)}
    ${nextSection(a)}
    <details class="card"><summary>Raw data (JSON)</summary><pre><code>${JSON.stringify(payload, null, 2)}</code></pre></details>`);

  $$(".meter > span[data-width]").forEach((bar) => { bar.style.width = `${bar.dataset.width}%`; });
  $$("[data-section]").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    document.getElementById(`section-${link.dataset.section}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  if (!meta.saved) return;
  $("#copy-md").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(meta.markdown || ""); toast("Markdown copied."); }
    catch { toast("Copy failed; use Download instead."); }
  });
  $("#download-md").addEventListener("click", () => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([meta.markdown || ""], { type: "text/markdown" }));
    link.download = `changeguard-${String(meta.target).replace(/[^\w.-]+/g, "-")}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
  $("#delete-report").addEventListener("click", async () => {
    if (!confirm("Delete this report? This cannot be undone.")) return;
    await api(`/api/reports/${meta.id}/delete`, {});
    toast("Report deleted.");
    location.hash = "#/reports";
  });
  $("#rerun").addEventListener("click", async () => {
    const status = $("#report-status");
    const button = $("#rerun");
    button.disabled = true;
    status.hidden = false;
    status.innerHTML = piece(loading("Running the analysis again…"));
    try {
      const result = isPr
        ? await api("/api/analyze/pr", { repository: payload.repository, pull_number: payload.pull_number })
        : await api("/api/analyze/local", { path: payload.repo_path, base: payload.base, head: payload.head });
      location.hash = `#/report/${result.report_id}`;
    } catch (error) {
      showError(status, error);
      button.disabled = false;
    }
  });
}

const section = (key, title, body, extra = "") => html`
  <section class="card" id="section-${key}"><div class="row"><h2>${title}</h2><span class="spacer"></span>${extra}</div>${body}</section>`;
const bullets = (items, empty) => (items && items.length
  ? html`<ul>${items.map((item) => html`<li>${typeof item === "string" ? item : nodeName(item)}</li>`)}</ul>`
  : html`<p class="muted">${empty}</p>`);
const nodeName = (item) => (typeof item === "string" ? item
  : item.qualified_name || item.symbol_id || item.title || item.name || JSON.stringify(item));
/** "path/file.py::pkg.module.func" -> "pkg.module.func  (path/file.py)" */
const symbolLabel = (id) => {
  const text = String(id);
  const split = text.indexOf("::");
  return split > 0 ? html`${text.slice(split + 2)} <span class="muted small">(${text.slice(0, split)})</span>` : text;
};

function riskSection(risk) {
  const factors = (risk.factors || []).map((f) => {
    const width = f.normalized_value == null ? 0 : Math.round(Math.min(1, Math.max(0, f.normalized_value)) * 100);
    return html`<div class="factor">
      <div><strong>${label(f.factor)}</strong> ${f.state !== "known" ? badge(f.state) : ""}</div>
      <div class="meter" title="${width}%"><span data-width="${width}"></span></div>
      <div class="small mono">${f.contribution != null ? `+${f.contribution}` : "n/a"}</div>
      <div class="reason">${f.reason}</div></div>`;
  });
  const triggers = risk.review_triggers || [];
  const before = risk.metrics?.level_before_review_triggers;
  const triggerBlock = triggers.length ? html`
    <h3>Review triggers</h3>
    <p class="muted small">A senior developer should look at this change${before ? `; these raised the level from ${before}` : ""}.</p>
    <ul>${triggers.map((t) => html`<li>${badge(t.severity.toLowerCase())} ${t.reason}${t.location ? html` <span class="muted small mono">(${t.location})</span>` : ""}</li>`)}</ul>` : "";
  return section("risk", "Why this risk level", html`
    ${triggerBlock}
    <p class="muted small">The score adds up weighted factors. "Uncertain" factors could not be measured and are not counted, so the score may be too low.</p>
    ${factors.length ? factors : html`<p class="muted">No risk factors recorded.</p>`}
    ${risk.recommendations?.length ? html`<h3>Recommendations</h3>${bullets(risk.recommendations)}` : ""}`);
}

/** A list showing the first `limit` items, with the rest behind an expander. */
function capped(items, render, limit = 25) {
  const head = html`<ul class="file-list">${items.slice(0, limit).map(render)}</ul>`;
  if (items.length <= limit) return head;
  return html`${head}<details><summary class="small">Show ${items.length - limit} more</summary>
    <ul class="file-list">${items.slice(limit).map(render)}</ul></details>`;
}

function changesSection(a) {
  const files = a.changed_files || [];
  const symbols = a.changed_symbols || [];
  return section("changes", "What changed", html`<div class="grid two">
    <div><h3>${plural(files.length, "file")}</h3>
      ${capped(files, (f) => html`<li class="mono">${f}</li>`)}</div>
    <div><h3>${plural(symbols.length, "function, class or type")}</h3>
      ${symbols.length ? capped(symbols, (s) => html`<li><span class="kind">${label(s.kind)}</span>${s.qualified_name}
        <div class="muted small mono">${s.file_path}</div></li>`) : html`<p class="muted">No functions or classes were mapped for these files.</p>`}</div>
  </div>`);
}

function impactSection(blast) {
  const direct = blast.direct_dependents || [];
  const indirect = blast.indirect_dependents || [];
  const list = (items, empty) => (items.length
    ? html`<ul class="file-list">${items.slice(0, 60).map((item) => html`<li>${symbolLabel(nodeName(item))}</li>`)}</ul>${items.length > 60 ? html`<p class="muted small">…and ${items.length - 60} more</p>` : ""}`
    : html`<p class="muted">${empty}</p>`);
  return section("impact", "What else is affected", html`
    <p class="muted small">Code that calls the changed code, directly or through other functions, including frontend code that calls a changed API.</p>
    <div class="grid two">
      <div><h3>Direct callers (${direct.length})</h3>${list(direct, "No callers found.")}</div>
      <div><h3>Indirect callers (${indirect.length})</h3>${list(indirect, "None found.")}</div>
    </div>
    ${blast.unresolved_relationships ? html`<p class="hint">${plural(blast.unresolved_relationships, "call")} could not be resolved (dynamic code or external libraries), so some callers may be missing.</p>` : ""}`);
}

function apiSection(api) {
  if (!api) return section("api", "API impact", html`<p class="muted">No HTTP endpoints were involved in this change.</p>`);
  const changed = new Set(api.changed_endpoint_ids || []);
  const endpoints = (api.endpoints || []).filter((e) => changed.has(e.endpoint_id));
  const consumers = new Map((api.consumers || []).map((c) => [c.consumer_id, c]));
  const edges = (api.edges || []).filter((e) => changed.has(e.endpoint_id));
  return section("api", "API impact", html`
    ${endpoints.length ? html`<h3>Changed endpoints</h3><ul class="file-list">${endpoints.map((e) => html`
      <li><span class="badge">${e.http_method}</span> <code>${e.route}</code> <span class="muted small mono">${e.source_file}</span></li>`)}</ul>`
      : html`<p class="muted">No changed endpoints.</p>`}
    ${edges.length ? html`<h3>Callers of changed endpoints</h3><div class="table-wrap"><table>
      <thead><tr><th>Endpoint</th><th>Called from</th><th>Match</th></tr></thead>
      <tbody>${edges.map((edge) => {
        const consumer = consumers.get(edge.target_node) || {};
        return html`<tr><td><code>${edge.endpoint_identity}</code></td>
          <td class="mono small">${consumer.source_file || edge.target_node}${consumer.source_line ? `:${consumer.source_line}` : ""}</td>
          <td>${badge(edge.confidence || edge.resolution || "matched", "")}</td></tr>`;
      })}</tbody></table></div>` : endpoints.length ? html`<p class="muted">No frontend callers of the changed endpoints were found.</p>` : ""}
    ${(api.ambiguous_relationships?.length || api.unresolved_relationships?.length) ? html`<p class="hint">
      ${api.ambiguous_relationships?.length || 0} ambiguous and ${api.unresolved_relationships?.length || 0} unresolved API calls (for example URLs built at runtime).</p>` : ""}`);
}

function historySection(history) {
  const results = history?.results || [];
  return section("history", "Related history", results.length ? html`
    <p class="muted small">Past incidents, decisions and notes that mention the same code. Add more on the <a href="#/evidence">Evidence</a> page.</p>
    ${results.map((r) => html`<div class="card">
      <div class="row"><strong>${r.title}</strong>${badge(r.source_type || "evidence", "")}<span class="spacer"></span>
        <span class="muted small">match ${Math.round((r.similarity_score || 0) * 100)}%</span></div>
      <p class="small muted">${r.source}</p><p class="small">${String(r.matched_text || "").slice(0, 400)}</p></div>`)}`
    : html`<p class="muted">No related incidents or notes were found. ChangeGuard can only find history you have added: see <a href="#/guide/evidence">teaching it your history</a>.</p>`);
}

function verificationSection(plan, result) {
  const checks = result?.checks || [];
  const status = result?.status || plan?.status;
  const limitations = [...new Set([...(plan?.limitations || []), ...(result?.limitations || [])])];
  return section("verification", "Tests", html`
    ${!plan && !result ? html`<p class="muted">Tests were not run. Turn on "Run tests in a sandbox" to run the matching tests automatically.</p>` : ""}
    ${checks.length ? html`<div class="table-wrap"><table><thead><tr><th>Check</th><th>Result</th><th>Time</th></tr></thead><tbody>
      ${checks.map((c) => html`<tr><td><strong>${c.check?.check_id || ""}</strong><div class="muted small">${c.check?.description || label(c.check?.operation)}</div>
        ${c.failure_summary ? html`<pre><code>${c.failure_summary}</code></pre>` : ""}</td>
        <td>${badge(c.status)}</td><td class="small">${c.duration_seconds}s</td></tr>`)}</tbody></table></div>` : ""}
    ${limitations.length ? html`<h3>Notes</h3>${bullets(limitations)}` : ""}`, status ? badge(status) : "");
}

function aiSection(report, state) {
  if (!report) {
    return section("ai", "AI investigation", html`<p class="muted">${state?.status === "failed"
      ? `The AI investigation failed: ${state.error || "see the raw data below"}.`
      : "Not run. Turn on \"AI investigation\" when analyzing to get a written explanation backed by evidence."}</p>`);
  }
  return section("ai", "AI investigation", html`
    <p>${report.summary}</p>
    ${report.findings?.length ? html`<h3>Findings</h3><ul>${report.findings.map((f) => html`<li>${f.statement}
      ${f.evidence_refs?.length ? html`<div class="muted small">evidence: ${f.evidence_refs.map((ref) => html`<code>${ref}</code> `)}</div>` : ""}</li>`)}</ul>` : ""}
    ${report.uncertainties?.length ? html`<h3>What the AI was unsure about</h3>${bullets(report.uncertainties)}` : ""}
    <p class="hint">${plural(report.tool_trace?.length || state?.tool_calls?.length || 0, "tool call")} made. Findings must cite evidence the tools returned.</p>`,
  state?.status ? badge(state.status, "") : "");
}

function nextSection(a) {
  return section("next", "Next steps", html`
    <h3>Recommended checks</h3>${bullets(a.recommended_verification, "No specific checks recommended.")}
    <h3>Limits of this analysis</h3>${bullets(a.uncertainties, "None reported.")}`);
}

/* ---------- projects ---------- */

async function viewProjects() {
  const projects = await api("/api/projects");
  setPage("Projects", html`
    <div class="page-head"><div><h1>Projects</h1><p class="muted">Repositories ChangeGuard knows about. Adding one detects its languages and imports its evidence.</p></div></div>
    <div class="card">
      <h2>Add a project</h2>
      <form id="project-form">
        <div class="field"><label for="project-path">Repository folder</label>
          <input id="project-path" name="path" type="text" placeholder="/home/you/projects/my-app" required autocomplete="off">
          <div class="hint">Any git repository on this computer. Same as running <code>changeguard init</code> inside it.</div></div>
        <label class="check"><input type="checkbox" name="evidence_folders" checked>
          <span>Create an evidence folder <small>Adds <code>.changeguard/evidence/</code> with incidents/, decisions/ and runbooks/ folders for Markdown notes.</small></span></label>
        <label class="check"><input type="checkbox" name="github_action">
          <span>Report on every pull request <small>Adds <code>.github/workflows/changeguard.yml</code>. Commit and push it, and GitHub posts a ChangeGuard report on each PR.</small></span></label>
        <button class="button primary" type="submit">Add project</button>
        <div class="form-status" hidden></div>
      </form>
      <div id="project-result"></div>
    </div>
    ${projects.length ? projects.map(projectCard) : html`<div class="card empty">No projects yet.</div>`}`);
  $("#project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    busyForm(form, "Scanning the repository…", async () => {
      const result = await api("/api/projects", {
        path: form.path.value.trim(), evidence_folders: form.evidence_folders.checked, github_action: form.github_action.checked,
      });
      await viewProjects();
      $("#project-result").innerHTML = piece(html`<div class="alert info">
        <strong>Added ${result.name}</strong> (${result.repository}).
        ${result.created.length ? html`<div>Created: ${result.created.map((c) => html`<code>${c}</code> `)}</div>` : ""}
        <div>Evidence: ${result.evidence.added} imported, ${result.evidence.existing} already stored.</div>
        ${result.workflow ? html`<div>Workflow <code>${result.workflow.path}</code> ${result.workflow.created ? "created. Commit and push it to turn on PR reports." : "already exists (left unchanged)."}</div>` : ""}
        ${result.notes.length ? html`<ul>${result.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ""}
        <div class="row"><a class="button small primary" href="#/analyze?tab=local&path=${encodeURIComponent(result.path)}">Analyze latest commit</a></div></div>`);
    });
  });
  $$("[data-sync]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api("/api/evidence/sync", { path: button.dataset.sync });
      toast(`Evidence synced: ${result.added} new, ${result.existing} already stored.`);
    } catch (error) { toast(error.message); } finally { button.disabled = false; }
  }));
  $$("[data-remove]").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Remove this project from ChangeGuard? Files and reports are not deleted.")) return;
    await api("/api/projects/remove", { path: button.dataset.remove });
    viewProjects();
  }));
}

function projectCard(p) {
  const languages = Object.entries(p.languages || {});
  return html`<div class="card">
    <div class="row"><h2>${p.name}</h2><span class="muted small">${p.repository}</span><span class="spacer"></span>
      <a class="button small primary" href="#/analyze?tab=local&path=${encodeURIComponent(p.path)}">Analyze</a>
      <a class="button small" href="#/reports?repository=${encodeURIComponent(p.repository)}">Reports</a>
      <button class="button small" type="button" data-sync="${p.path}">Sync evidence</button>
      <button class="button small danger" type="button" data-remove="${p.path}">Remove</button></div>
    <p class="mono small muted">${p.path}</p>
    <div class="row small">${languages.map(([name, count]) => html`<span class="badge">${name} · ${count}</span>`)}
      ${(p.frameworks || []).map((f) => html`<span class="badge ok">${f}</span>`)}</div></div>`;
}

/* ---------- evidence ---------- */

const EVIDENCE_TYPES = ["incident", "postmortem", "runbook", "architecture_decision", "issue", "pull_request", "release_note"];

async function viewEvidence(_arg, params) {
  const projects = await api("/api/projects");
  const repository = params.get("repository") || "";
  const documents = await api(`/api/evidence${repository ? `?repository=${encodeURIComponent(repository)}` : ""}`);
  const repos = [...new Set(projects.map((p) => p.repository))].sort();
  setPage("Evidence", html`
    <div class="page-head"><div><h1>Evidence</h1>
      <p class="muted">Past incidents, design decisions and runbooks. When a change touches the same code or topics, they show up in the report and raise the risk score.</p></div>
      <select id="evidence-repo" aria-label="Repository"><option value="">All repositories</option>
        ${repos.map((r) => html`<option value="${r}" ${r === repository ? "selected" : ""}>${r}</option>`)}</select></div>
    <div class="grid two">
      <div class="card">
        <h2>Search</h2>
        <form id="search-form" class="row"><input type="search" name="q" placeholder="e.g. payment timeout, login" aria-label="Search evidence">
          <button class="button" type="submit">Search</button><div class="form-status" hidden></div></form>
        <div id="search-results"></div>
        <h2 class="spaced">Stored documents (${documents.length})</h2>
        ${documents.length ? html`<ul class="file-list">${documents.map((d) => html`<li><strong>${d.title}</strong> ${badge(d.source_type, "")}
          <div class="muted small">${d.source}</div><div class="small">${d.preview}</div></li>`)}</ul>`
          : html`<p class="muted">Nothing stored${repository ? " for this repository" : ""} yet. Add notes here, or put Markdown files in <code>.changeguard/evidence/</code> and sync the project.</p>`}
      </div>
      <div class="card">
        <h2>Add a note</h2>
        <form id="evidence-form">
          <div class="field"><label for="ev-title">Title</label><input id="ev-title" name="title" type="text" required placeholder="Checkout outage on 2026-03-02"></div>
          <div class="grid two">
            <div class="field"><label for="ev-type">Type</label><select id="ev-type" name="source_type">
              ${EVIDENCE_TYPES.map((t) => html`<option value="${t}">${label(t)}</option>`)}</select></div>
            <div class="field"><label for="ev-repo">Repository</label><input id="ev-repo" name="repository" type="text" list="repo-list" required placeholder="owner/repo" value="${repository || repos[0] || ""}">
              <datalist id="repo-list">${repos.map((r) => html`<option value="${r}"></option>`)}</datalist></div>
          </div>
          <div class="field"><label for="ev-content">Content (Markdown)</label>
            <textarea id="ev-content" name="content" required placeholder="What happened, which files or functions were involved, how it was fixed…"></textarea>
            <div class="hint">Name files, functions and endpoints. Matching works on those words.</div></div>
          <button class="button primary" type="submit">Save note</button>
          <div class="form-status" hidden></div>
        </form>
      </div>
    </div>`);
  $("#evidence-repo").addEventListener("change", (event) => {
    location.hash = event.target.value ? `#/evidence?repository=${encodeURIComponent(event.target.value)}` : "#/evidence";
  });
  $("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const q = form.q.value.trim();
    if (!q) return;
    busyForm(form, "Searching…", async () => {
      const results = await api(`/api/evidence/search?q=${encodeURIComponent(q)}${repository ? `&repository=${encodeURIComponent(repository)}` : ""}`);
      $("#search-results").innerHTML = piece(results.length
        ? html`<ul class="file-list">${results.map((r) => html`<li><strong>${r.title}</strong> <span class="muted small">${Math.round(r.similarity_score * 100)}%</span>
            <div class="small">${String(r.matched_text).slice(0, 300)}</div></li>`)}</ul>`
        : html`<p class="muted">No matches.</p>`);
    });
  });
  $("#evidence-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    busyForm(form, "Saving…", async () => {
      const result = await api("/api/evidence", {
        title: form.title.value, source_type: form.source_type.value, repository: form.repository.value, content: form.content.value,
      });
      toast(result.duplicate ? "That note was already stored." : "Note saved.");
      viewEvidence(_arg, params);
    });
  });
}

/* ---------- settings ---------- */

async function viewSettings() {
  const s = await api("/api/settings");
  setPage("Settings", html`
    <div class="page-head"><div><h1>Settings</h1><p class="muted">Saved to <code>${s.settings_file}</code>, readable only by you.</p></div></div>
    <form id="settings-form">
      <div class="card">
        <h2>AI provider</h2>
        <p class="muted small">AI investigation is optional. Without it, ChangeGuard still maps the impact, scores risk and runs tests.</p>
        <label class="check"><input type="radio" name="provider" value="gemini" ${s.provider === "gemini" ? "checked" : ""}>
          <span>Google Gemini <small>Fast and accurate. Needs a free API key from aistudio.google.com/apikey. Code from analyzed changes is sent to Google.</small></span></label>
        <label class="check"><input type="radio" name="provider" value="ollama" ${s.provider === "ollama" ? "checked" : ""}>
          <span>Ollama (local) <small>Runs on your computer; nothing leaves it. Needs Ollama installed and a model pulled. Small models give weaker results.</small></span></label>
        <div class="grid two">
          <div class="field"><label for="gemini-key">Gemini API key</label>
            <input id="gemini-key" name="gemini_api_key" type="password" autocomplete="off" placeholder="${s.gemini_key_set ? "Saved. Leave empty to keep it" : "Paste your key"}">
            ${s.gemini_key_set ? html`<label class="check small"><input type="checkbox" name="remove_key"> Remove the saved key</label>` : ""}</div>
          <div class="field"><label for="gemini-model">Gemini model</label><input id="gemini-model" name="gemini_model" type="text" value="${s.gemini_model}"></div>
          <div class="field"><label for="ollama-model">Ollama model</label><input id="ollama-model" name="ollama_model" type="text" value="${s.ollama_model}"></div>
        </div>
      </div>
      <div class="card">
        <h2>GitHub</h2>
        <p>${s.github_token ? html`${badge("ok", "ok")} Using a token from the ${s.github_token}.` : html`${badge("not set", "warn")} Public repositories only.`}</p>
        <div class="field"><label for="gh-token">Personal access token (optional)</label>
          <input id="gh-token" name="github_token" type="password" autocomplete="off" placeholder="Leave empty to use gh auth login">
          <div class="hint">Simplest option: run <code>gh auth login</code> in a terminal. ChangeGuard uses it automatically.</div></div>
      </div>
      <div class="card">
        <h2>Storage</h2>
        <p class="small">Reports, projects and evidence are stored in <code>${s.database}</code>. Set <code>CHANGEGUARD_DATABASE</code> to use another file.</p>
        <p class="hint">A <code>.env</code> file in the folder where you start ChangeGuard overrides these settings.</p>
      </div>
      <div class="row"><button class="button primary" type="submit">Save settings</button><div class="form-status" hidden></div></div>
    </form>`);
  $("#settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = { provider: form.provider.value || s.provider };
    if (form.gemini_api_key.value.trim()) body.gemini_api_key = form.gemini_api_key.value.trim();
    if (form.remove_key?.checked) body.gemini_api_key = null;
    if (form.gemini_model.value.trim() !== s.gemini_model) body.gemini_model = form.gemini_model.value.trim();
    if (form.ollama_model.value.trim() !== s.ollama_model) body.ollama_model = form.ollama_model.value.trim();
    if (form.github_token.value.trim()) body.github_token = form.github_token.value.trim();
    busyForm(form, "Saving…", async () => {
      await api("/api/settings", body);
      toast("Settings saved.");
      viewSettings();
    });
  });
}

/* ---------- guide ---------- */

const GUIDE = [
  ["start", "1. Check your setup", html`
    <p>The <a href="#/">Dashboard</a> shows a setup check. Only <strong>git</strong> is required; everything else is optional:</p>
    <ul>
      <li><strong>AI provider</strong>: adds a written investigation. Set it up in <a href="#/settings">Settings</a>.</li>
      <li><strong>GitHub access</strong>: needed for private repositories and to post PR comments. Run <code>gh auth login</code> once.</li>
      <li><strong>Sandbox</strong> (bubblewrap): needed to run tests safely. Install with <code>sudo apt install bubblewrap</code>.</li>
      <li><strong>Node.js</strong>: needed to run tests and type checks for JavaScript/TypeScript projects.</li>
    </ul>
    <p>From a terminal: <code>changeguard doctor</code>.</p>`],
  ["projects", "2. Add your projects", html`
    <p>On <a href="#/projects">Projects</a>, enter the folder of a git repository (or run <code>changeguard init</code> inside it). ChangeGuard:</p>
    <ul>
      <li>detects languages and frameworks (FastAPI, Flask, Express, Next.js, React, Spring…) and tells you what it can't analyze;</li>
      <li>creates <code>.changeguard/evidence/</code> for your incident notes and decisions (optional);</li>
      <li>imports notes already in <code>docs/adr</code>, <code>docs/incidents</code>, <code>docs/runbooks</code> and your <code>CHANGELOG.md</code>.</li>
    </ul>
    <p>Do this for every new project you create. It takes a few seconds.</p>`],
  ["analyze", "3. Analyze a change", html`
    <p>Open <a href="#/analyze">New analysis</a>:</p>
    <ul>
      <li><strong>Local changes</strong>: pick a project, then <em>Latest commit</em> or <em>This branch vs main</em>. Commit your work first; ChangeGuard compares commits, not unsaved edits.</li>
      <li><strong>Pull request</strong>: paste a GitHub PR link.</li>
    </ul>
    <p>Options:</p>
    <ul>
      <li><strong>AI investigation</strong>: the AI reads the diff and related code through limited, read-only tools and writes findings that cite evidence.</li>
      <li><strong>Run tests in a sandbox</strong>: runs only the tests related to the change (pytest for Python; <code>npm run typecheck</code> and <code>npm test</code> for JS/TS) in an offline, throw-away copy of the code. For JS/TS run <code>npm install</code> in the project first.</li>
    </ul>`],
  ["report", "4. Read the report", html`
    <ul>
      <li><strong>Risk (0–100)</strong>: low under 25, medium under 50, high under 75, then critical. Each factor shows how much it added. <em>Uncertain</em> factors couldn't be measured, so treat the score as a minimum.</li>
      <li><strong>What changed</strong>: files plus the exact functions, classes and components.</li>
      <li><strong>What else is affected</strong>: code that calls the changed code, directly or indirectly. This includes React components that call a changed FastAPI/Express endpoint.</li>
      <li><strong>API impact</strong>: changed endpoints and every <code>fetch</code>/<code>axios</code> call that hits them.</li>
      <li><strong>Related history</strong>: your past incidents and decisions that match this change.</li>
      <li><strong>Next steps</strong>: what to test, and what the analysis could <em>not</em> see.</li>
    </ul>
    <p>Every report is saved under <a href="#/reports">Reports</a>. Use <em>Copy Markdown</em> to paste it into a PR description. <a href="#/demo">See a sample report.</a></p>`],
  ["evidence", "5. Teach it your history", html`
    <p>ChangeGuard gets better as you record what went wrong. Two ways:</p>
    <ul>
      <li>Add a note on the <a href="#/evidence">Evidence</a> page.</li>
      <li>Commit Markdown files to <code>.changeguard/evidence/incidents/</code>, <code>decisions/</code> or <code>runbooks/</code>, then click <em>Sync evidence</em> on the project (CI syncs automatically).</li>
    </ul>
    <p>Write the file names, function names and endpoints involved. Matching is based on those words.</p>`],
  ["automate", "6. Get a report on every pull request", html`
    <ol>
      <li>Run <code>changeguard init --github-action</code> in the repository, or tick <em>Report on every pull request</em> when adding the project.</li>
      <li>Commit and push <code>.github/workflows/changeguard.yml</code>.</li>
      <li>Optional: add a <code>GEMINI_API_KEY</code> secret (repository → Settings → Secrets and variables → Actions) for AI investigation.</li>
    </ol>
    <p>Each PR then gets one ChangeGuard comment, updated on every push. The report also appears in the Actions run summary.</p>`],
  ["docker", "Running in Docker", html`
    <p>With Docker, ChangeGuard sees only the projects folder you mounted. Paths are the same as on your computer, e.g. <code>/home/you/projects/app</code>.</p>
    <ul>
      <li>Set <code>PROJECTS_DIR</code> in the <code>.env</code> next to <code>docker-compose.yml</code> if your code is not in <code>~/projects</code>.</li>
      <li>GitHub: <code>gh</code> isn't available in the container, so add a token in <a href="#/settings">Settings</a> for private repositories.</li>
      <li>Ollama on your computer is reached automatically at <code>host.docker.internal:11434</code>.</li>
      <li>Sandboxed tests don't run inside Docker. Install with pip for that.</li>
      <li>Terminal commands: <code>docker compose exec changeguard changeguard impact --repo /home/you/projects/app</code>.</li>
    </ul>`],
  ["cli", "Terminal commands", html`<pre><code>changeguard ui                          # open this app
changeguard init [--github-action]      # set up the current repository
changeguard doctor                      # check your setup
changeguard impact                      # analyze the latest commit
changeguard impact --base main --head HEAD --verify --llm
changeguard pr analyze owner/repo#12 [--llm] [--verify] [--comment]
changeguard evidence sync               # import .changeguard/evidence and docs/
changeguard evidence add incident "Title" notes.md
changeguard evidence search "payment timeout"</code></pre>`],
  ["support", "What is supported", html`<div class="table-wrap"><table>
    <thead><tr><th>Language</th><th>Analysis</th><th>Tests (--verify)</th></tr></thead><tbody>
    <tr><td>Python</td><td>Functions, classes, methods, imports; FastAPI and Flask routes</td><td>Related pytest tests, compile check</td></tr>
    <tr><td>TypeScript / JavaScript</td><td>Functions, React components, classes, types; Express, Fastify, Hono and Next.js routes; fetch/axios calls</td><td><code>npm run typecheck</code> or <code>tsc</code>, <code>npm test</code></td></tr>
    <tr><td>Java</td><td>Classes, methods, Spring endpoints</td><td>Not yet</td></tr>
    <tr><td>Other languages</td><td>Changed files and lines only</td><td>Not yet</td></tr></tbody></table></div>`],
  ["troubleshooting", "Troubleshooting", html`<ul>
    <li><strong>"Not found" for a pull request</strong>: the repository is private. Run <code>gh auth login</code> or add a token in <a href="#/settings">Settings</a>.</li>
    <li><strong>Tests show "Blocked"</strong>: the sandbox couldn't start. Check that <code>bwrap --unshare-all --ro-bind / / true</code> works in a terminal. For JS/TS, run <code>npm install</code> first.</li>
    <li><strong>Gemini errors (429/503)</strong>: rate limit or overload. ChangeGuard retries automatically; wait a minute and run again.</li>
    <li><strong>Port 8000 in use</strong>: start with <code>CHANGEGUARD_PORT=8010 changeguard ui</code>.</li>
    <li><strong>Few callers found</strong>: code reached through dynamic imports, dependency injection or URLs built at runtime can't be traced. The report counts these as unresolved.</li>
  </ul>`],
];

function viewGuide(anchor) {
  setPage("Guide", html`
    <div class="page-head"><div><h1>Guide</h1><p class="muted">How to use ChangeGuard on every project, from setup to automatic PR reports.</p></div>
      <a class="button" href="#/demo">See a sample report</a></div>
    <div class="card toc"><strong>Contents</strong>${GUIDE.map(([key, title]) => html`<a href="#/guide/${key}">${title}</a>`)}</div>
    ${GUIDE.map(([key, title, body]) => html`<section class="card" id="guide-${key}"><h2>${title}</h2>${body}</section>`)}`);
  if (anchor) document.getElementById(`guide-${anchor}`)?.scrollIntoView({ block: "start" });
}

route();
