const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[char]);
const compactSha = (sha) => sha ? `${sha.slice(0, 8)}…${sha.slice(-5)}` : "Not available";
const label = (value) => String(value || "").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (char) => char.toUpperCase());
const list = (values, emptyText) => Array.isArray(values) && values.length
  ? values
  : [emptyText];

let currentData = null;
let toastTimer = null;

$("#demo-button").addEventListener("click", loadDemo);
$("#analysis-form").addEventListener("submit", submitAnalysis);
$("#new-analysis").addEventListener("click", showLanding);
$("#footer-new-analysis").addEventListener("click", (event) => {
  event.preventDefault();
  showLanding();
});

document.querySelectorAll(".side-link").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll(".side-link").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});

async function loadDemo() {
  clearError();
  setBusy(true, "Loading the saved real-PR demo…");
  try {
    const response = await fetch("/api/demo", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`Demo fixture could not be loaded (${response.status}).`);
    const data = await response.json();
    renderAnalysis(data);
    showToast("Demo analysis loaded. Sample report is clearly labeled.");
  } catch (error) {
    showError(error.message || "Unable to load the demo analysis.");
  } finally {
    setBusy(false);
  }
}

async function submitAnalysis(event) {
  event.preventDefault();
  clearError();
  const repository = $("#repository").value.trim();
  const pullNumber = Number($("#pull-number").value);
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    showError("Enter the repository in OWNER/REPO format.");
    return;
  }
  if (!Number.isSafeInteger(pullNumber) || pullNumber < 1) {
    showError("Enter a positive pull request number.");
    return;
  }
  setBusy(true, "Fetching immutable revisions and building evidence-backed impact…");
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        repository,
        pull_number: pullNumber,
        verify: $("#verify-option").checked,
        llm: $("#llm-option").checked
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Analysis failed (${response.status}).`);
    if (data.status === "FAILED") throw new Error(data.error?.message || "Pull request analysis failed.");
    renderAnalysis(data);
  } catch (error) {
    showError(error.message || "Analysis could not be completed.");
  } finally {
    setBusy(false);
  }
}

function renderAnalysis(result) {
  currentData = result;
  const analysis = result.analysis || result;
  const risk = analysis.risk_assessment || {};
  const api = analysis.api_impact || {};
  const blast = analysis.blast_radius || {};
  const report = analysis.investigation_report || null;
  const verification = analysis.verification_result || null;
  const demo = result.demo_fixture === true;
  $("#landing").hidden = true;
  $("#dashboard").hidden = false;
  $("#demo-banner").hidden = !demo;

  $("#pr-state").textContent = result.status || "COMPLETED";
  $("#pr-repo").textContent = result.repository || analysis.repository || "Repository";
  $("#pr-number").textContent = result.pull_number ? `#${result.pull_number}` : "Local analysis";
  $("#pr-title").textContent = demo ? "Incident summary semantics" : analysis.change_summary || "Pull request analysis";
  $("#pr-subtitle").textContent = analysis.change_summary || "Commit-pinned change intelligence";
  $("#base-sha").textContent = compactSha(result.base_sha || analysis.change_reference?.split("..")[0]);
  $("#head-sha").textContent = compactSha(result.head_sha || analysis.change_reference?.split("..")[1]);
  const githubUrl = result.url || (result.repository && result.pull_number
    ? `https://github.com/${encodeURIComponent(result.repository).replace("%2F", "/")}/pull/${result.pull_number}`
    : null);
  const prLink = $("#pr-link");
  prLink.href = githubUrl || "#";
  prLink.hidden = !githubUrl;
  $("#changed-count").textContent = (result.changed_files || analysis.changed_files || []).length;
  $("#change-summary").textContent = analysis.change_summary || "No change summary was returned.";

  const changedFiles = result.changed_files || analysis.changed_files || [];
  $("#file-count").textContent = changedFiles.length;
  $("#changed-files").innerHTML = changedFiles.length
    ? changedFiles.map((path) => `<li>${escapeHtml(path)}</li>`).join("")
    : '<li class="empty-row">No changed files were returned.</li>';

  const symbols = analysis.changed_symbols || [];
  $("#symbol-count").textContent = symbols.length;
  $("#changed-symbols").innerHTML = symbols.length
    ? symbols.map((symbol) => {
      const name = typeof symbol === "string" ? symbol : symbol.qualified_name || symbol.symbol_id;
      const kind = typeof symbol === "string" ? "symbol" : symbol.kind || "symbol";
      return `<li><span class="symbol-kind">${escapeHtml(kind)}</span><span>${escapeHtml(name)}</span></li>`;
    }).join("")
    : '<li class="empty-row">No changed symbols were returned.</li>';

  const impactedSymbols = [
    ...(blast.direct_dependents || []).map((item) => ({ name: item, kind: "direct" })),
    ...(blast.indirect_dependents || []).map((item) => ({ name: item, kind: "indirect" }))
  ];
  const impactedConsumers = blast.cross_language_api_consumers || [];
  $("#impact-count").textContent = impactedSymbols.length + impactedConsumers.length;
  $("#impacted-content").innerHTML = impactedSymbols.length || impactedConsumers.length
    ? `<div class="impacted-tags">${[...impactedSymbols.map((item) =>
      `<span class="impact-tag">${escapeHtml(item.kind)} · ${escapeHtml(item.name)}</span>`),
      ...impactedConsumers.map((item) => `<span class="impact-tag">API consumer · ${escapeHtml(item)}</span>`)].join("")}</div>`
    : '<div>No impacted symbols were resolved. Check the uncertainty and API sections for coverage limits.</div>';

  renderRisk(risk);
  renderApi(api, blast);
  renderVerification(verification, analysis.verification_plan);
  renderInvestigation(analysis, report, demo);
  window.scrollTo({ top: 0, behavior: "instant" });
  if (location.search.includes("view=") && location.hash) {
    requestAnimationFrame(() => document.querySelector(location.hash)?.scrollIntoView());
  }
}

function renderRisk(risk) {
  $("#risk-level").textContent = risk.overall_level || "UNKNOWN";
  $("#risk-level").className = `risk-level level-${String(risk.overall_level || "UNKNOWN").toLowerCase()}`;
  const score = Number.isFinite(risk.score) ? risk.score : null;
  $("#risk-score").textContent = score === null ? "—" : score.toFixed(2);
  $("#score-fill").style.width = `${Math.max(0, Math.min(100, score || 0))}%`;
  const coverage = risk.metrics?.score_weight_coverage;
  $("#risk-coverage").textContent = Number.isFinite(coverage) ? `${(coverage * 100).toFixed(1)}%` : "Not available";
  const factors = risk.factors || [];
  $("#risk-factors").innerHTML = factors.length
    ? factors.map((factor) => {
      const normalized = Number.isFinite(factor.normalized_value) ? factor.normalized_value : 0;
      const contribution = Number.isFinite(factor.contribution) ? factor.contribution.toFixed(2) : "—";
      return `<div class="factor-row"><div class="factor-name">${escapeHtml(label(factor.factor))}<span class="factor-state">${escapeHtml(factor.state || "unknown")} · weight ${escapeHtml(factor.weight ?? "—")}</span></div><div class="factor-bar"><i style="width:${Math.max(0, Math.min(100, normalized * 100))}%"></i></div><div class="factor-amount">${contribution}<span class="factor-state">contribution</span></div></div>`;
    }).join("")
    : '<p class="muted">No factor breakdown was returned.</p>';
  const uncertainties = [...(risk.uncertainties || []), ...(risk.factors || [])
    .filter((item) => item.state === "unknown")
    .map((item) => `${label(item.factor)} is unknown: ${item.reason || "No signal was supplied."}`)];
  $("#uncertainty-count").textContent = uncertainties.length;
  $("#uncertainties").innerHTML = bulletItems(uncertainties, "No risk uncertainties were reported.");
  $("#recommendations").innerHTML = bulletItems(risk.recommendations, "No recommendations were returned.");
}

function renderApi(api, blast) {
  const endpoints = api.endpoints || [];
  const consumers = api.consumers || [];
  const edges = api.edges || [];
  const endpointById = new Map(endpoints.map((item) => [item.endpoint_id, item]));
  const consumerById = new Map(consumers.map((item) => [item.consumer_id, item]));
  const changedEndpointIds = api.changed_endpoint_ids || [];
  $("#changed-endpoint-count").textContent = changedEndpointIds.length;
  $("#changed-endpoints").innerHTML = changedEndpointIds.length
    ? changedEndpointIds.map((id) => {
      const endpoint = endpointById.get(id);
      return endpoint
        ? `<div class="relationship-item"><b>${escapeHtml(endpoint.http_method)} ${escapeHtml(endpoint.route)}</b>${escapeHtml(endpoint.source_file)}:${escapeHtml(endpoint.source_line)}</div>`
        : `<div class="relationship-item"><b>Changed endpoint</b>${escapeHtml(id)}</div>`;
    }).join("")
    : '<div class="relationship-empty">No changed endpoints were identified.</div>';
  $("#endpoint-count").textContent = api.metrics?.endpoints_discovered ?? endpoints.length;
  $("#consumer-count").textContent = api.metrics?.consumers_discovered ?? consumers.length;
  $("#api-edge-count").textContent = `${edges.length} detected edge${edges.length === 1 ? "" : "s"}`;
  const unresolved = api.unresolved_relationships || [];
  const ambiguous = api.ambiguous_relationships || [];
  $("#api-unresolved-count").textContent = unresolved.length + ambiguous.length;
  $("#api-unresolved").innerHTML = unresolved.length || ambiguous.length
    ? [...unresolved.map((item) => relationshipCard(item, "Unresolved")),
      ...ambiguous.map((item) => relationshipCard(item, "Ambiguous"))].join("")
    : '<div class="relationship-empty">No unresolved or ambiguous relationships.</div>';
  const impactedIds = new Set(api.impacted_consumer_ids || []);
  const impacted = consumers.filter((item) => impactedIds.has(item.consumer_id));
  $("#api-impacted").innerHTML = impacted.length
    ? impacted.map((item) => `<div class="relationship-item"><b>${escapeHtml(item.http_method)} ${escapeHtml(item.route || "Dynamic URL")}</b>${escapeHtml(item.source_file)}:${escapeHtml(item.source_line)}</div>`).join("")
    : '<div class="relationship-empty">No impacted frontend consumers were identified.</div>';
  const visibleEdges = edges.slice(0, 8);
  $("#api-graph").innerHTML = visibleEdges.length
    ? visibleEdges.map((edge) => {
      const endpoint = endpointById.get(edge.endpoint_id);
      const consumer = consumerById.get(edge.target_node);
      const route = edge.endpoint_identity || (endpoint ? `${endpoint.http_method} ${endpoint.route}` : "API endpoint");
      const consumerFile = consumer?.source_file || edge.target_node || "Frontend consumer";
      const consumerLine = consumer?.source_line ? `Line ${consumer.source_line}` : "";
      return `<div class="api-edge"><div class="api-node"><strong>${escapeHtml(route)}</strong><small>${escapeHtml(endpoint?.source_file || edge.source_node || "Backend provider")}</small></div><div class="edge-arrow">⟶</div><div class="api-node frontend"><strong>${escapeHtml(consumerFile.split("/").pop())}</strong><small>${escapeHtml(consumerFile)} ${escapeHtml(consumerLine)}</small></div></div>`;
    }).join("")
    : '<div class="relationship-empty">No deterministic API edges were returned.</div>';
  if (edges.length > visibleEdges.length) {
    $("#api-graph").insertAdjacentHTML("beforeend", `<div class="relationship-empty">Showing ${visibleEdges.length} of ${edges.length} edges. All remain available in the analysis JSON.</div>`);
  }
}

function relationshipCard(item, title) {
  const refs = item.evidence_refs || [];
  return `<div class="relationship-item"><b>${escapeHtml(title)}${item.reason ? ` · ${escapeHtml(item.reason)}` : ""}</b>${refs.map(escapeHtml).join("<br>") || "No source reference returned."}</div>`;
}

function renderVerification(verification, plan) {
  const status = verification?.status || "NOT_RUN";
  $("#verification-status").textContent = label(status);
  $("#verification-detail").textContent = verification?.limitations?.join(" ") ||
    (plan?.limitations || []).join(" ") ||
    (status === "NOT_RUN" ? "Verification was not requested for this analysis." : "No additional verification details were returned.");
  $("#verification-icon").textContent = status === "PASSED" ? "✓" : status === "FAILED" || status === "ERROR" ? "×" : "!";
  $("#verification-icon").className = `verification-icon ${status === "PASSED" ? "passed" : status === "FAILED" || status === "ERROR" ? "failed" : ""}`;
  const checks = verification?.checks || [];
  $("#verification-checks").innerHTML = checks.length
    ? checks.map((item) => `<div class="check-line"><b>${escapeHtml(label(item.status))}</b><span>${escapeHtml(item.check?.operation || item.check_type || "Check")} ${escapeHtml((item.check?.targets || []).join(", "))}</span></div>`).join("")
    : `<div class="check-line"><b>${escapeHtml(status === "BLOCKED" ? "0 checks executed" : "No checks")}</b><span>${escapeHtml(plan?.status ? `Plan: ${label(plan.status)}` : "No verification evidence available.")}</span></div>`;
  const evidence = verification?.evidence || [];
  $("#verification-evidence").innerHTML = evidence.length
    ? evidence.map((item) => `<div class="evidence-line"><b>${escapeHtml(label(item.status))}</b><span>${escapeHtml(item.check_type || "Check")} · ${escapeHtml(item.revision || "")}<br>${escapeHtml(item.output_summary || item.failure_summary || "")}</span></div>`).join("")
    : `<div class="evidence-line"><b>${escapeHtml(status)}</b><span>${escapeHtml(verification?.stdout_summary || "No verification checks produced evidence.")}</span></div>`;
}

function renderInvestigation(analysis, report, demo) {
  const state = analysis.investigation_state || {};
  const calls = state.tool_calls || [];
  const history = analysis.historical_evidence?.results || [];
  const reportEvidence = report?.evidence || [];
  const evidence = reportEvidence.length
    ? reportEvidence
    : history.map((item) => ({
      reference: item.evidence_id,
      tool_name: "search_evidence",
      description: item.title || item.source_type || "Retrieved engineering evidence"
    }));
  const liveStatus = analysis.investigation_error
    ? "Failed"
    : report && !demo
      ? "Completed"
      : demo
        ? "Sample report"
        : state.status && state.status !== "not_run"
          ? label(state.status)
          : "Not run";
  $("#llm-status").textContent = liveStatus;
  $("#tool-count").textContent = calls.length || report?.tool_trace?.length || 0;
  $("#tool-list").innerHTML = calls.length
    ? calls.map((call) => `<div class="tool-item"><span class="tool-status">${call.error ? "!" : "✓"}</span><div><b>${escapeHtml(call.tool_name)}</b><small>${escapeHtml(call.error || call.call_id || "Tool result recorded")}</small></div></div>`).join("")
    : report?.tool_trace?.length
      ? report.tool_trace.map((tool) => `<div class="tool-item"><span class="tool-status">✓</span><div><b>${escapeHtml(typeof tool === "string" ? tool : tool.tool_name || "Investigation tool")}</b><small>Included in the sample investigation trace</small></div></div>`).join("")
      : '<div class="relationship-empty">No LLM tools were run. Enable local LLM investigation to opt in.</div>';
  $("#evidence-count").textContent = evidence.length;
  $("#evidence-list").innerHTML = evidence.length
    ? evidence.map((item) => `<div class="evidence-item"><span class="tool-status">◉</span><div><b>${escapeHtml(item.tool_name || "Evidence")}</b><small>${escapeHtml(item.description || item.reference)}<br>ref: ${escapeHtml(item.reference)}</small></div></div>`).join("")
    : '<div class="relationship-empty">No historical evidence was retrieved.</div>';
  $("#report-mode").textContent = demo ? "Sample · not live LLM output" : report ? "Structured report" : "No report";
  $("#report-summary").textContent = report?.summary || analysis.investigation_error ||
    "No grounded AI report was generated. The deterministic analysis remains available above.";
  const findings = report?.findings || [];
  $("#report-findings").innerHTML = findings.length
    ? findings.map((finding) => `<div class="finding">${escapeHtml(finding.statement)}<span class="finding-cites">Evidence: ${(finding.evidence_refs || []).map(escapeHtml).join(" · ") || "No citation provided"}</span></div>`).join("")
    : '<div class="relationship-empty">No findings were returned.</div>';
  $("#report-recommendations").innerHTML = bulletItems(
    report?.recommended_verification || analysis.recommended_verification,
    "No verification recommendations were returned."
  );
  const uncertainties = report?.uncertainties || [];
  $("#report-uncertainties").textContent = uncertainties.length
    ? `Uncertainties · ${uncertainties.join(" ")}`
    : "";
}

function bulletItems(values, emptyText) {
  const items = list(values, emptyText);
  return items.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function showLanding() {
  $("#dashboard").hidden = true;
  $("#landing").hidden = false;
  $("#form-error").hidden = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setBusy(busy, message = "") {
  $("#busy-overlay").hidden = !busy;
  if (message) $("#busy-overlay span").textContent = message;
  $("#analyze-button").disabled = busy;
  $("#demo-button").disabled = busy;
}

function showError(message) {
  $("#form-error").textContent = message;
  $("#form-error").hidden = false;
}

function clearError() {
  $("#form-error").hidden = true;
  $("#form-error").textContent = "";
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}

const screenshotView = new URLSearchParams(location.search).get("view");
if (screenshotView && screenshotView !== "landing") {
  loadDemo().then(() => {
    const section = screenshotView === "dashboard" ? "#overview" : `#${screenshotView}`;
    if (section !== "#overview") document.querySelector(section)?.scrollIntoView();
  });
}
