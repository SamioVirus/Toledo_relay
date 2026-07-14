"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
let bootstrap = null;
let currentRunId = null;
let currentState = null;
let inspectorPayload = null;
let pollHandle = null;
let inspectorReturnFocus = null;
let currentRenderSignature = null;
let runListSignature = null;
let pollBusy = false;
let pollCount = 0;
let lastHeadSignature = null;
let railReturnFocus = null;
let settingsWorkflowId = null;
const promptPreviewCache = new Map();
const gateDrafts = new Map();

async function api(path, options = {}, retriedNonce = false) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (options.method && options.method !== "GET") headers["X-Orchestrator-Nonce"] = bootstrap?.nonce || "";
  const response = await fetch(path, {...options, headers});
  const type = response.headers.get("content-type") || "";
  const value = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok && response.status === 403 && !retriedNonce && options.method && options.method !== "GET" && String(value?.error || "").includes("launch nonce")) {
    const refreshed = await fetch("/api/bootstrap", {headers:{"Content-Type":"application/json"}});
    const refreshedType = refreshed.headers.get("content-type") || "";
    const refreshedValue = refreshedType.includes("application/json") ? await refreshed.json() : await refreshed.text();
    if (refreshed.ok) {
      bootstrap = refreshedValue;
      return api(path, options, true);
    }
  }
  if (!response.ok) throw new Error(value.error || value || `${response.status}`);
  return value;
}

function sessionColorClass(label) {
  const logicalLabel = String(label || "?").match(/^([A-Z]+)(?:\.|$)/);
  if (logicalLabel) {
    let ordinal = 0;
    for (const char of logicalLabel[1]) ordinal = ordinal * 26 + char.charCodeAt(0) - 64;
    return `session-color-${(ordinal - 1) % 24}`;
  }
  let hash = 0;
  for (const char of String(label || "?")) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `session-color-${hash % 24}`;
}

function sessionDisplay(turn) {
  const generation = turn.session_generation == null ? 1 : Number(turn.session_generation);
  return `${turn.session_label || "?"}.${generation}`;
}

function sessionIdSuffix(sessionId) {
  if (!sessionId) return "provider ID unavailable";
  const value = String(sessionId);
  return `provider …${value.slice(-8)}`;
}

function compactId(runId) {
  if (!runId) return "";
  const parts = runId.split("_");
  return `${parts[1]?.slice(4, 13) || "run"} · ${parts.at(-1)}`;
}

async function loadBootstrap() {
  bootstrap = await api("/api/bootstrap");
  $("#runtime-label").textContent = bootstrap.runtime_dir;
  $("#connection-dot").classList.add("online");
  $("#connection-label").textContent = "Local engine ready";
  populateNewRun();
  renderSettings();
  renderRunList(bootstrap.runs);
  if (!currentRunId && bootstrap.runs.length) selectRun(bootstrap.runs[0].run_id);
}

function renderRunList(runs) {
  const signature = JSON.stringify([currentRunId, runs.map((run) => [run.run_id, run.status, run.current_turn, run.worker?.active, run.worker?.error])]);
  if (signature === runListSignature) return;
  runListSignature = signature;
  const root = $("#run-list");
  root.innerHTML = "";
  for (const run of runs) {
    const button = document.createElement("button");
    button.className = `run-item ${run.run_id === currentRunId ? "active" : ""}`;
    button.innerHTML = `<div class="run-item-top"><strong>${escapeHtml(compactId(run.run_id))}</strong><span class="status-pill ${escapeHtml(run.worker?.active ? "running" : run.status)}">${escapeHtml(run.worker?.active ? "active" : run.status)}</span></div><p>${escapeHtml(run.project)} · ${escapeHtml(run.workflow)} · ${run.current_turn || 0} turns</p>`;
    button.addEventListener("click", () => selectRun(run.run_id));
    root.append(button);
  }
}

async function refreshRuns() {
  const runs = await api("/api/runs");
  bootstrap.runs = runs;
  renderRunList(runs);
}

async function selectRun(runId) {
  currentRunId = runId;
  currentRenderSignature = null;
  lastHeadSignature = null;
  closeRunRail();
  await refreshCurrent(true);
  await refreshRuns();
  clearInterval(pollHandle);
  pollHandle = setInterval(poll, 1600);
}

function stateSignature(state) {
  return JSON.stringify({
    status: state.status,
    turn: state.current_turn,
    stage: state.current_stage,
    gate: state.pending_human_decision,
    inflight: state.inflight,
    cycles: state.cycles,
    decisions: state.decisions,
    events: state.events,
    worker: state.worker,
    override: state.next_turn_override,
  });
}

async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    let changed = true;
    try {
      const head = await api(`/api/runs/${encodeURIComponent(currentRunId)}/head`);
      const signature = JSON.stringify([head.event_sequence, head.status, head.current_turn, head.pending_human_decision, head.worker]);
      changed = signature !== lastHeadSignature;
      lastHeadSignature = signature;
    } catch { changed = true; }
    if (changed) await refreshCurrent();
    pollCount += 1;
    if (pollCount % 5 === 0) await refreshRuns();
  } finally {
    pollBusy = false;
  }
}

async function refreshCurrent(force = false) {
  if (!currentRunId) return;
  try {
    currentState = await api(`/api/runs/${encodeURIComponent(currentRunId)}`);
    const summary = bootstrap?.runs?.find((run) => run.run_id === currentRunId);
    if (summary) {
      Object.assign(summary, {
        status: currentState.status,
        cycle: currentState.cycle,
        current_turn: currentState.current_turn,
        worker: currentState.worker,
      });
      renderRunList(bootstrap.runs);
    }
    const signature = stateSignature(currentState);
    if (force || signature !== currentRenderSignature) {
      currentRenderSignature = signature;
      renderRun();
    }
    $("#connection-dot").classList.add("online");
    $("#connection-label").textContent = "Local engine ready";
  } catch (error) {
    console.error(error);
    $("#connection-dot").classList.remove("online");
    $("#connection-label").textContent = `Engine unavailable · ${error.message}`;
  }
}

function renderRun() {
  const state = currentState;
  $("#empty-state").hidden = true;
  $("#timeline").hidden = false;
  const cycle = state.cycles?.[state.cycle - 1];
  const canRecover = state.schema_version === "toledo_orchestrator.run.v2" && ["created", "running"].includes(state.status) && !state.worker?.active;
  const terminalHeadings = {
    complete: "Run complete",
    cancelled: "Run cancelled",
    stopped: "Run finished by operator",
    failed: "Run failed",
  };
  const heading = canRecover
    ? "Run interrupted — recovery available"
    : state.worker?.active
      ? `${state.inflight?.title || stageTitle(state.current_stage)} is running…`
      : terminalHeadings[state.status] || stageTitle(state.current_stage);
  const retryReasons = new Set(["operator_step", "provider_requested_human", "provider_invocation_failed", "provider_session_id_missing", "provider_session_missing", "provider_session_not_new", "provider_session_changed_unexpectedly", "missing_substantive_output", "malformed_directive", "unsupported_stage_directive", "invalid_next_turn_profile", "profile_permission_exceeds_stage", "background_operation_failed"]);
  const canOverride = state.schema_version === "toledo_orchestrator.run.v2" && state.current_stage && !state.inflight && (state.status === "created" || state.status === "running" || retryReasons.has(state.pending_human_decision));
  const canSteer = state.schema_version === "toledo_orchestrator.run.v2" && state.status === "paused" && !state.inflight && Boolean(state.turns?.length);
  const workflow = bootstrap.workflows[state.workflow];
  const stage = workflow?.stages?.[state.current_stage] || {};
  const override = state.next_turn_override;
  const overrideValue = override?.target_stage === state.current_stage ? override?.profile_value : null;
  const profile = overrideValue || workflow?.profiles?.[override?.profile || stage.profile] || {};
  const cost = (state.turns || []).reduce((sum, turn) => sum + Number(turn.usage?.total_cost_usd || 0), 0);
  const strip = $("#run-status-strip");
  strip.hidden = false;
  strip.innerHTML = `<span>${escapeHtml(state.status)}</span><span>${escapeHtml(stage.title || state.current_stage || "")}</span><span>${escapeHtml(profile.provider || "")}</span><span>${escapeHtml(profile.model || "")}</span><span>${escapeHtml(profile.effort || "")}</span><span>$${cost.toFixed(2)}</span><span>${state.worker?.active ? "worker active" : "worker idle"}</span>`;
  $("#run-header").innerHTML = `<div><p class="eyebrow">${escapeHtml(state.run_id)} · ${escapeHtml(state.status.toUpperCase())}</p><h2>${escapeHtml(heading || "Run complete")}</h2></div><div class="run-facts" id="run-facts"><span class="fact">cycle ${state.cycle || 1}</span><span class="fact">${state.current_turn || 0} turns</span><span class="fact">${escapeHtml(state.project)}</span><span class="fact">${escapeHtml((state.working_revision || state.source_revision || "").slice(0, 8))}</span>${state.execution_branch ? `<span class="fact">${escapeHtml(state.execution_branch)}</span>` : ''}${canOverride ? '<button class="quiet-button" id="next-turn-control">Override next turn ↗</button>' : ''}${canRecover ? '<button class="accept-button" id="recover-run">Recover run</button>' : ''}</div>`;
  $("#next-turn-control")?.addEventListener("click", openNextTurnControl);
  if (canSteer) {
    const button = document.createElement("button");
    button.className = "quiet-button";
    button.id = "steer-control";
    button.textContent = "Steer latest artifact";
    $("#run-facts").append(button);
    button.addEventListener("click", openSteerControl);
  }
  $("#recover-run")?.addEventListener("click", recoverCurrentRun);
  renderFilters(state);
  renderTimeline(state);
  if (state.worker?.error) showBanner(state.worker.error, "error");
}

async function openSteerControl() {
  const note = window.prompt("Steer the active provider session. It will produce a complete replacement artifact.");
  if (note == null || !note.trim()) return;
  try {
    await api(`/api/runs/${encodeURIComponent(currentRunId)}/steer`, {method:"POST", body:JSON.stringify({note:note.trim()})});
    await refreshCurrent();
  } catch (error) { alert(error.message); }
}

async function recoverCurrentRun() {
  const button = $("#recover-run");
  if (button) button.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(currentRunId)}/recover`, {method:"POST", body:"{}"});
    await refreshCurrent();
  } catch (error) {
    alert(error.message);
    if (button) button.disabled = false;
  }
}

function stageTitle(stageId) {
  if (!stageId) return "";
  const workflow = bootstrap?.workflows?.[currentState?.workflow];
  return workflow?.stages?.[stageId]?.title || stageId.replaceAll("-", " ");
}

function renderFilters(state) {
  const phases = [...new Set((state.turns || []).map((turn) => turn.phase))];
  const sessions = [...new Set((state.turns || []).map((turn) => turn.session_label))];
  fillFilter($("#phase-filter"), phases, "All phases");
  fillFilter($("#session-filter"), sessions, "All sessions");
}

function fillFilter(select, values, allLabel) {
  const selected = select.value;
  select.innerHTML = `<option value="all">${escapeHtml(allLabel)}</option>` + values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  if (["all", ...values].includes(selected)) select.value = selected;
}

function visibleTurn(turn) {
  const phase = $("#phase-filter").value;
  const session = $("#session-filter").value;
  return (phase === "all" || turn.phase === phase) && (session === "all" || turn.session_label === session);
}

function renderTimeline(state) {
  const root = $("#timeline");
  root.innerHTML = "";
  for (const cycle of state.cycles || [{number:1,id:"cycle.0001"}]) {
    const block = document.createElement("section");
    block.className = "cycle-block";
    block.dataset.cycle = cycle.number;
    block.innerHTML = `<div class="cycle-heading">${escapeHtml(cycle.id || `cycle.${String(cycle.number).padStart(4,"0")}`)} · ${escapeHtml(cycle.status || "active")}</div>`;
    const turns = (state.turns || []).filter((turn) => turn.cycle === cycle.number && visibleTurn(turn));
    const decisions = (state.decisions || []).filter((decision) => Number(decision.cycle || 1) === Number(cycle.number));
    const shownDecisions = new Set();
    for (const [index, decision] of decisions.entries()) {
      if (Number(decision.after_turn || 0) === 0) {
        block.append(humanDecisionNode(decision, index));
        shownDecisions.add(decision.file);
      }
    }
    let handoffShown = false;
    let completionShown = false;
    for (const turn of turns) {
      if (!handoffShown && cycle.approved_handoff && turn.phase === "implementation") {
        block.append(milestone("Approved handoff sealed", cycle.approved_handoff, false));
        handoffShown = true;
      }
      if (!completionShown && cycle.completion_receipt && turn.phase === "next-task") {
        block.append(milestone("Implementation accepted", cycle.completion_receipt, true));
        completionShown = true;
      }
      block.append(turnRow(turn));
      for (const [index, decision] of decisions.entries()) {
        if (Number(decision.after_turn) === Number(String(turn.id || "").split(".").at(-1))) {
          block.append(humanDecisionNode(decision, index));
          shownDecisions.add(decision.file);
        }
      }
    }
    if (cycle.approved_handoff && !handoffShown) block.append(milestone("Approved handoff sealed", cycle.approved_handoff, false));
    if (cycle.completion_receipt && !completionShown) block.append(milestone("Implementation accepted", cycle.completion_receipt, true));
    for (const [index, decision] of decisions.entries()) {
      if (!shownDecisions.has(decision.file)) block.append(humanDecisionNode(decision, index));
    }
    if (state.cycle === cycle.number && state.worker?.active) {
      const active = document.createElement("div");
      active.className = "active-node";
      active.id = "active-node";
      active.textContent = `${state.inflight?.session_slot || "agent"} · ${stageTitle(state.inflight?.stage || state.current_stage)}`;
      block.append(active);
    }
    if (state.cycle === cycle.number && state.status === "paused" && state.pending_human_decision) {
      const gate = $("#human-gate-template").content.cloneNode(true);
      configureGate(gate, state);
      bindGate(gate);
      block.append(gate);
    }
    root.append(block);
  }
  const activeGate = $("#active-gate", root);
  if (activeGate && state.pending_human_decision) populateGateUpnext(activeGate, state);
  hydratePromptPreviews();
  hydrateDecisionPreviews();
}

function turnRow(turn) {
  const row = document.createElement("div");
  row.className = "timeline-row";
  row.dataset.phase = turn.phase;
  row.dataset.session = turn.session_label;
  const colorClass = sessionColorClass(turn.session_label);
  const preview = (turnPreview(turn) || "Open the stored artifact.").replace(/\s+/g, " ").slice(0, 280);
  const interstitialFile = turn.direction_file || turn.interstitial_file || turn.prompt_file;
  const tooltipId = `direction-${String(turn.id || "turn").replaceAll(".", "-")}`;
  row.innerHTML = `<button class="prompt-node" data-interstitial-path="${escapeHtml(turnArtifactPath(interstitialFile))}" aria-describedby="${escapeHtml(tooltipId)}" aria-label="Open ${escapeHtml(turn.prompt_label || turn.title)} direction"><span class="prompt-label">${escapeHtml(turn.prompt_label || promptShort(turn.prompt_kind))}</span><span class="prompt-tooltip" id="${escapeHtml(tooltipId)}" role="tooltip">Loading exact direction…</span></button><article class="turn-card ${escapeHtml(turn.provider)} ${colorClass}" tabindex="0" role="button" aria-label="Open ${escapeHtml(turn.title)} output"><div class="turn-card-head"><div class="actor"><span class="session-token">${escapeHtml(sessionDisplay(turn))}</span><div><h3>${escapeHtml(turn.title)}</h3><span class="route">${escapeHtml(turn.provider)} · ${escapeHtml(turn.role)}</span></div></div><span class="turn-number">${escapeHtml(turn.id)}</span></div><p class="turn-preview">${escapeHtml(preview)}</p><div class="chips"><span class="chip ${escapeHtml(turn.session_action)}">${escapeHtml(turn.session_action)} session</span><span class="chip">${escapeHtml(turn.profile_label || turn.profile)}</span><span class="chip">${escapeHtml(turn.permission)}</span><span class="chip">${Math.round((turn.elapsed_ms || 0)/1000)}s</span></div></article>`;
  const card = $(".turn-card", row);
  const prompt = $(".prompt-node", row);
  card.addEventListener("click", () => openTurn(turn, "output"));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openTurn(turn, "output");
    }
  });
  prompt.addEventListener("click", () => openTurn(turn, turn.direction_file ? "direction" : "stance"));
  return row;
}

function promptShort(kind) {
  const names = {ideate:"Ideas",skeptic:"Skeptic",adjudicate:"Judge","implementation-kickoff":"Build","implementation-audit":"Audit",correction:"Correct","next-step":"Next","human-other":"Redirect"};
  return names[kind] || kind?.slice(0,8) || "Prompt";
}

function turnPreview(turn) {
  return turn.preview || `${turn.artifact_type?.replaceAll("-", " ") || "work product"} · ${turn.directive?.next || "no directive"}`;
}

function milestone(label, path, accepted) {
  const row = document.createElement("div");
  row.className = "milestone-row";
  const button = document.createElement("button");
  button.className = `milestone ${accepted ? "accepted" : ""}`;
  button.textContent = label;
  button.addEventListener("click", () => openArtifact(label, path));
  row.append(button);
  return row;
}

function humanDecisionNode(decision, index) {
  const row = document.createElement("div");
  row.className = "decision-row";
  const button = document.createElement("button");
  button.className = "decision-node";
  button.dataset.decisionPath = decision.file;
  button.innerHTML = `<span class="decision-choice">Human · ${escapeHtml(humanizeReason(decision.choice || "direction"))}</span><span class="decision-reason">${escapeHtml(humanizeReason(decision.reason))}</span><span class="decision-preview">Open stored direction</span>`;
  button.addEventListener("click", () => openDecision(decision, index));
  row.append(button);
  return row;
}

async function hydrateDecisionPreviews() {
  for (const node of $$(".decision-node[data-decision-path]")) {
    try {
      const text = await artifactText(node.dataset.decisionPath);
      const preview = text.replace(/\s+/g, " ").trim();
      $(".decision-preview", node).textContent = preview || "Stored without additional text";
    } catch { $(".decision-preview", node).textContent = "Decision artifact unavailable"; }
  }
}

async function hydratePromptPreviews() {
  for (const node of $$(".prompt-node[data-interstitial-path]")) {
    try {
      const cacheKey = `${currentRunId}:${node.dataset.interstitialPath}`;
      let preview = promptPreviewCache.get(cacheKey);
      if (!preview) {
        preview = await artifactText(node.dataset.interstitialPath);
        promptPreviewCache.set(cacheKey, preview);
      }
      $(".prompt-tooltip", node).textContent = preview;
    } catch { $(".prompt-tooltip", node).textContent = "Direction artifact unavailable"; }
  }
}

async function artifactText(path) {
  return api(`/api/runs/${encodeURIComponent(currentRunId)}/artifact?path=${encodeURIComponent(path)}`);
}

function turnArtifactPath(file) {
  return String(file || "").includes("/") ? String(file) : `turns/${file}`;
}

async function openTurn(turn, tab = "output") {
  const stancePath = turn.interstitial_file || turn.prompt_file;
  const [stance, transport, output, direction] = await Promise.all([
    artifactText(turnArtifactPath(stancePath)),
    artifactText(turnArtifactPath(turn.prompt_file)),
    artifactText(turnArtifactPath(turn.output_file)),
    turn.direction_file ? artifactText(turnArtifactPath(turn.direction_file)) : Promise.resolve(undefined),
  ]);
  inspectorPayload = {stance, direction, transport, output, metadata: JSON.stringify(turn, null, 2)};
  setDirectionTabLabel("Situational");
  $("#inspector-kicker").textContent = `${sessionDisplay(turn)} · ${turn.profile_label || turn.profile}`;
  $("#inspector-title").textContent = turn.title;
  const observed = turn.observed_model || turn.observed_reasoning
    ? `<span class="chip observed">observed ${escapeHtml(turn.observed_model || "model unknown")} · ${escapeHtml(turn.observed_reasoning || "effort unknown")}</span>`
    : `<span class="chip muted">observation unavailable${turn.observation_error ? ` · ${escapeHtml(turn.observation_error)}` : ""}</span>`;
  $("#inspector-meta").innerHTML = `<span class="chip ${escapeHtml(turn.session_action)}">${escapeHtml(turn.session_action)}</span><span class="chip">logical ${escapeHtml(sessionDisplay(turn))}</span><span class="chip">${escapeHtml(sessionIdSuffix(turn.session_id))}</span><span class="chip">configured ${escapeHtml(turn.configured_model)} · ${escapeHtml(turn.configured_reasoning)}</span>${observed}<span class="chip">${escapeHtml(turn.permission)}</span>`;
  openInspector(tab);
}

async function openArtifact(title, path) {
  const content = await artifactText(path);
  inspectorPayload = {output:content, metadata:JSON.stringify({path}, null, 2)};
  $("#inspector-kicker").textContent = "SEALED ARTIFACT";
  $("#inspector-title").textContent = title;
  $("#inspector-meta").innerHTML = `<span class="chip">${escapeHtml(path)}</span>`;
  openInspector("output");
}

async function openDecision(decision, index) {
  const content = await artifactText(decision.file);
  inspectorPayload = {direction:content, metadata:JSON.stringify(decision, null, 2)};
  setDirectionTabLabel("Owner direction");
  $("#inspector-kicker").textContent = `HUMAN DIRECTION · ${String(index + 1).padStart(2, "0")}`;
  $("#inspector-title").textContent = humanizeReason(decision.reason);
  $("#inspector-meta").innerHTML = `<span class="chip">${escapeHtml(decision.choice)}</span><span class="chip">cycle ${escapeHtml(decision.cycle)}</span><span class="chip">after turn ${escapeHtml(decision.after_turn)}</span>`;
  openInspector("direction");
}

function setDirectionTabLabel(label) {
  $(".inspector-tabs [data-tab=\"direction\"]").textContent = label;
}

function openInspector(tab) {
  const inspector = $("#inspector");
  if (inspector.hidden) inspectorReturnFocus = document.activeElement;
  inspector.hidden = false;
  inspector.inert = false;
  document.querySelector(".app-shell").classList.add("inspector-open");
  inspector.setAttribute("aria-hidden", "false");
  syncInspectorTabs();
  selectInspectorTab(tab);
  $("#close-inspector").focus();
}

function syncInspectorTabs() {
  $$(".inspector-tabs button").forEach((button) => {
    button.hidden = inspectorPayload?.[button.dataset.tab] === undefined;
  });
}

function closeInspector() {
  const inspector = $("#inspector");
  if (inspector.hidden) return;
  document.querySelector(".app-shell").classList.remove("inspector-open");
  inspector.setAttribute("aria-hidden", "true");
  inspector.inert = true;
  inspector.hidden = true;
  if (inspectorReturnFocus?.isConnected) inspectorReturnFocus.focus();
  inspectorReturnFocus = null;
}

function selectInspectorTab(tab) {
  const requested = $(`.inspector-tabs button[data-tab="${tab}"]`);
  if (!requested || requested.hidden) tab = $$(".inspector-tabs button").find((button) => !button.hidden)?.dataset.tab;
  $$(".inspector-tabs button").forEach((button) => {
    const selected = button.dataset.tab === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  $("#inspector-content").textContent = inspectorPayload?.[tab] || "No stored content for this view.";
}

function bindGate(fragment) {
  const gate = $(".human-gate", fragment);
  const textarea = $("textarea", gate);
  textarea.addEventListener("input", () => gateDrafts.set(gate.dataset.draftKey, textarea.value));
  $$('[data-choice]', gate).forEach((button) => button.addEventListener("click", async () => {
    const choice = button.dataset.choice;
    if (choice === "other" && !textarea.value.trim()) { textarea.focus(); return; }
    button.disabled = true;
    try {
      if (gate.dataset.reason === "operator_step") {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/continue`, {method:"POST", body:JSON.stringify({direction:textarea.value})});
      } else {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/decision`, {method:"POST", body:JSON.stringify({choice, text:choice === "other" ? textarea.value : ""})});
      }
      gateDrafts.delete(gate.dataset.draftKey);
      await refreshCurrent();
    } catch (error) { alert(error.message); button.disabled = false; }
  }));
  $("[data-gate-stop]", gate)?.addEventListener("click", async (event) => {
    if (!window.confirm("Finish this run here? It is recorded as finished by you — nothing is committed and nothing further runs.")) return;
    event.target.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/stop`, {method:"POST", body:JSON.stringify({note:textarea.value})});
      gateDrafts.delete(gate.dataset.draftKey);
      await refreshCurrent();
      await refreshRuns();
    } catch (error) { alert(error.message); event.target.disabled = false; }
  });
}

async function populateGateUpnext(gate, state) {
  const panel = $("[data-gate-upnext]", gate);
  const tools = $("[data-gate-tools]", gate);
  if (!panel || state.schema_version !== "toledo_orchestrator.run.v2" || !state.current_stage || state.worker?.active) return;
  let preview;
  try {
    preview = await api(`/api/runs/${encodeURIComponent(currentRunId)}/next-turn`);
  } catch { return; }
  if (!preview?.available || !gate.isConnected) return;
  const profile = preview.profile || {};
  const session = preview.session || {};
  const receives = (preview.inputs || []).map((input) => `${input.title}${input.empty ? " (none yet)" : ` (${input.chars >= 1000 ? `${(input.chars / 1000).toFixed(1)}k` : input.chars} chars)`}`).join(", ") || "nothing beyond the session";
  const afterward = (preview.afterward || []).map((item) => `${item.directive} → ${item.description}`);
  const dedupedAfterward = [...new Set(afterward)];
  panel.innerHTML = `<div class="gate-upnext-line"><strong>${escapeHtml(profile.model || "provider default")}</strong><span>·</span><span>${escapeHtml(profile.effort || "default effort")}</span><span>·</span><span>${escapeHtml(profile.provider || "")}</span><span>·</span><span>${escapeHtml(profile.permission || "")}</span>${profile.overridden || session.overridden ? '<span class="override-chip">one-turn override active</span>' : ""}${profile.custom ? '<span class="warn-chip">custom — unverified</span>' : ""}</div>
    <dl>
      <div><dt>Actor</dt><dd>${escapeHtml(session.label || "?")} (${escapeHtml(preview.stage?.role || "agent")}) · ${escapeHtml(session.action || "?")} session</dd></div>
      <div><dt>Receives</dt><dd>${escapeHtml(receives)}</dd></div>
      <div><dt>Produces</dt><dd>${escapeHtml(String(preview.stage?.produces || "").replaceAll("-", " "))}</dd></div>
      <div><dt>Afterward</dt><dd>${dedupedAfterward.map((line) => escapeHtml(line)).join("<br>")}</dd></div>
      ${preview.rounds ? `<div><dt>Rounds</dt><dd>${preview.rounds.used} of ${preview.rounds.cap} used</dd></div>` : ""}
      ${preview.direction_preview ? `<div><dt>Direction</dt><dd>${escapeHtml(preview.direction_preview.replace(/\s+/g, " ").slice(0, 220))}</dd></div>` : ""}
    </dl>`;
  panel.hidden = false;
  tools.hidden = false;
  $("[data-gate-prompt]", gate).onclick = () => {
    inspectorPayload = {
      transport: preview.prompt || `Prompt preview unavailable: ${preview.prompt_error || "unknown"}`,
      metadata: JSON.stringify({stage: preview.stage, profile: preview.profile, session: preview.session, inputs: preview.inputs}, null, 2),
    };
    setDirectionTabLabel("Situational");
    $("#inspector-kicker").textContent = "EXACT NEXT PROMPT · PREVIEW";
    $("#inspector-title").textContent = preview.stage?.title || "Next turn";
    $("#inspector-meta").innerHTML = `<span class="chip">${escapeHtml(profile.provider || "")}</span><span class="chip">${escapeHtml(profile.model || "")} · ${escapeHtml(profile.effort || "")}</span><span class="chip ${escapeHtml(session.action || "")}">${escapeHtml(session.action || "")} session</span><span class="chip">${escapeHtml(profile.permission || "")}</span>`;
    openInspector("transport");
  };
  $("[data-gate-adjust]", gate).onclick = () => openNextTurnControl();
}

function configureGate(fragment, state) {
  const reason = state.pending_human_decision || "human_decision";
  const gate = $(".human-gate", fragment);
  const title = $("[data-gate-title]", gate);
  const description = $("[data-gate-description]", gate);
  const textarea = $("[data-gate-text]", gate);
  const label = $("[data-gate-label]", gate);
  const note = $("[data-gate-note]", gate);
  const stop = $("[data-gate-stop]", gate);
  const yes = $('[data-choice="yes"]', gate);
  const no = $('[data-choice="no"]', gate);
  const other = $('[data-choice="other"]', gate);
  const nextTitle = stageTitle(state.current_stage) || "next turn";
  const copy = {
    operator_step: [`Up next: ${nextTitle}`, "", `Run: ${nextTitle}`, "", "", "One-turn guidance (optional)"],
    next_task_approval: ["Is this the right next task?", "The proposal is preserved exactly. Accept it, finish the loop, or redirect the current strategic session.", "Yes — start planning", "No — finish here", "Other — revise proposal", "Tell the strategic session what to change"],
    validation_execution_approval: ["Run the validation commands?", "These commands execute on the host against the isolated implementation worktree. Review the pending commands before approving.", "Yes — run validation", "No — cancel run", "Other — send to repair", "Explain what the implementation session must change before validation"],
    validation_receipt_required: ["Validation receipt required", `Attach the patch-bound receipt from a terminal with: python -m toledo_orchestrator validate ${state.run_id} --receipt-file "C:\\path\\to\\receipt.json"`, "", "No — cancel run", "Other — add direction", "Add receipt or validation guidance"],
    unknown_validation_execution: ["Validation completion is unknown", "The controller stopped after host validation started but before a trustworthy completion record was sealed. It will not rerun the commands automatically. Route the work to repair/inspection, cancel, or add exact recovery direction.", "Yes — inspect and repair", "No — cancel run", "Other — direct recovery", "Tell the implementation session what evidence to inspect before any rerun"],
    provider_invocation_failed: ["Provider invocation failed", "No successful model response was accepted (quota, network, or CLI failure). Use “Change model · effort · session” below to switch to a model with headroom, then retry — the override applies to the retried turn.", "Retry with displayed settings", "Cancel run", "Retry with direction", "Optional direction for the retried turn"],
    planning_round_cap_reached: ["Planning round cap reached", "The planning loop used its configured rounds without agreement. Extend it, stop, or redirect the next revision.", "Yes — extend one round", "No — cancel run", "Other — extend with direction", "Tell the planning sessions what must change"],
    implementation_round_cap_reached: ["Implementation round cap reached", "The implementation loop used its configured repair rounds. Extend it, stop, or direct one more repair.", "Yes — extend one round", "No — cancel run", "Other — extend with direction", "Tell the implementation sessions what must change"],
    validation_failed_at_repair_cap: ["Validation still fails", "Required validation failed after the configured repair rounds, and the failure does not match the clean baseline. Extend repair, stop, or give a specific recovery direction.", "Yes — extend repair", "No — cancel run", "Other — direct repair", "Describe the evidence or repair you require"],
    validation_baseline_failure_decision: ["Only a pre-existing failure remains", "Required validation failed, but every failure matches the clean baseline at the same revision — this change did not introduce it. Accept and commit with the debt recorded, stop without committing, or send it to repair anyway.", "Yes — accept with recorded debt", "No — stop without commit", "Other — repair with direction", "Tell the implementation session what to change instead of accepting"],
  }[reason];
  if (copy) {
    [title.textContent, description.textContent, yes.textContent, no.textContent, other.textContent, textarea.placeholder] = copy;
  } else {
    title.textContent = humanizeReason(reason);
    description.textContent = `The run paused at ${stageTitle(state.current_stage) || "the current stage"}. Review the stored evidence, then continue, cancel, or add direction.`;
  }
  label.textContent = textarea.placeholder || "Optional direction";
  gate.dataset.draftKey = `${currentRunId}:${reason}`;
  textarea.value = gateDrafts.get(gate.dataset.draftKey) || "";
  if (reason === "validation_execution_approval") {
    const commands = (state.pending_validation?.commands || []).map((item) => `${item.id}: ${item.command}`);
    if (commands.length) description.textContent += `\n\nPending host commands:\n${commands.join("\n")}`;
    description.textContent = `You are approving these host commands once in the isolated worktree, not a provider turn.\n\n${description.textContent}\n\nWhere: ${state.execution_worktree || "isolated worktree"} at ${(state.working_revision || state.source_revision || "").slice(0, 12)}`;
    textarea.hidden = true;
    label.hidden = true;
  }
  if (reason === "validation_baseline_failure_decision") {
    const classification = state.pending_baseline_acceptance?.classification;
    if (classification) {
      const failures = Object.entries(classification.failures || {})
        .map(([id, item]) => `${id}: ${(item.current?.lines || []).join("; ") || "see validation output"}`);
      description.textContent += `\n\nBaseline ${String(classification.baseline_revision || "").slice(0, 12)} · matched failures:\n${failures.join("\n")}`;
    }
  }
  gate.dataset.reason = reason;
  if (reason === "operator_step") {
    no.hidden = true;
    other.hidden = true;
    stop.hidden = false;
    description.hidden = true;
    note.hidden = false;
    note.textContent = "Guidance is appended to this turn's prompt only — the workflow instruction is unchanged. It is recorded as an operator decision.";
  }
  if (reason === "validation_receipt_required") yes.hidden = true;
}

function humanizeReason(reason) {
  const text = String(reason || "review required").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function populateNewRun() {
  $("#new-project").innerHTML = Object.entries(bootstrap.projects).map(([id, project]) => `<option value="${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(project.root)}</option>`).join("");
  $("#new-workflow").innerHTML = Object.entries(bootstrap.workflows).map(([id, workflow]) => `<option value="${escapeHtml(id)}">${escapeHtml(workflow.label)}</option>`).join("");
  renderNewRunPreflight();
}

const launchOverrides = new Map();
const stagePromptCache = new Map();

function describeTransition(workflow, target) {
  const value = String(target);
  if (value.startsWith("@pause:")) {
    const reason = value.split(":")[1];
    const named = {
      next_task_approval: "pause for your approval of the proposed next task",
      provider_requested_human: "pause for your input",
    };
    return named[reason] || `pause: ${reason.replaceAll("_", " ")}`;
  }
  if (value.startsWith("@seal:")) {
    const [, sealedType, next] = value.split(":");
    return `seal the ${sealedType.replaceAll("-", " ")} and move to ${workflow.stages?.[next]?.title || next}`;
  }
  if (value.startsWith("@complete:")) {
    const next = value.split(":")[1];
    return `run validation, commit the accepted change, then ${workflow.stages?.[next]?.title || next}`;
  }
  return `move to ${workflow.stages?.[value]?.title || value}`;
}

function transitionLines(workflow, stage) {
  const seen = new Set();
  const lines = [];
  for (const [directive, target] of Object.entries(stage.transitions || {})) {
    const description = describeTransition(workflow, target);
    if (seen.has(description)) continue;
    seen.add(description);
    lines.push(`${directive} → ${description}`);
  }
  return lines;
}

function effectiveProfile(workflow, profileId) {
  const base = workflow.profiles?.[profileId] || {};
  const draft = launchOverrides.get(profileId);
  return draft ? {...base, ...draft, overridden: true} : {...base, overridden: false};
}

function renderNewRunPreflight() {
  const workflowId = $("#new-workflow").value;
  const workflow = bootstrap?.workflows?.[workflowId];
  const root = $("#new-run-preflight");
  if (!workflow) {
    root.innerHTML = '<li class="route-preflight-empty">No route definition is available.</li>';
    $("#route-preflight-summary").textContent = "";
    $("#route-warnings").hidden = true;
    return;
  }
  const stages = orderedWorkflowStages(workflow);
  const verified = bootstrap?.catalog?.verified_at ? new Date(bootstrap.catalog.verified_at).toLocaleDateString() : "never";
  $("#route-preflight-summary").textContent = `${stages.length} stages · catalog verified ${verified}${bootstrap?.catalog?.stale ? " (stale)" : ""}`;
  root.innerHTML = "";
  for (const [index, stage] of stages.entries()) root.append(routeStepRow(workflowId, workflow, stage, index));
  renderRouteWarnings(workflow, stages);
}

function routeStepRow(workflowId, workflow, stage, index) {
  const row = document.createElement("li");
  row.className = "route-step";
  const profile = effectiveProfile(workflow, stage.profile);
  const inCatalog = catalogModels(profile.provider).some((entry) => entry.selection_token === profile.model);
  const sharedWith = Object.values(workflow.stages || {}).filter((other) => other.profile === stage.profile && other.id !== stage.id).map((other) => other.title);
  const round = stage.round;
  row.innerHTML = `<span class="route-step-marker">${index + 1}</span>
    <div class="route-step-head"><span class="route-step-kind">${escapeHtml(stage.prompt_label || promptShort(stage.prompt_kind))}</span><h4>${escapeHtml(stage.title)}</h4>${round ? `<span class="route-step-loop">up to ${round.cap} rounds</span>` : ""}</div>
    <p class="route-step-who">${escapeHtml(stage.session_slot)}<span class="dot">·</span>${escapeHtml(profile.provider)}<span class="dot">·</span><strong>${escapeHtml(profile.model || "provider default")}</strong><span class="dot">·</span>${escapeHtml(profile.effort || "default")}<span class="dot">·</span>${escapeHtml(profile.permission || "read-only")}<span class="dot">·</span>${escapeHtml(stage.session_policy)} session${profile.overridden ? '<span class="override-chip">this run only</span>' : ""}${!inCatalog && catalogModels(profile.provider).length ? '<span class="unverified-chip">not in catalog</span>' : ""}</p>
    <p class="route-step-then">Produces ${escapeHtml(String(stage.artifact_type || "").replaceAll("-", " "))}. ${escapeHtml(transitionLines(workflow, stage).join(" · "))}</p>
    <div class="route-step-actions"><button type="button" class="quiet-button" data-step-instruction>View instruction</button><button type="button" class="quiet-button" data-step-adjust>Change model · effort</button></div>
    <div class="route-step-detail" data-step-detail hidden></div>
    <div class="route-step-editor" data-step-editor hidden></div>`;
  $("[data-step-instruction]", row).addEventListener("click", () => toggleStageInstruction(row, workflowId, stage));
  $("[data-step-adjust]", row).addEventListener("click", () => toggleStageEditor(row, workflow, stage, sharedWith));
  return row;
}

async function toggleStageInstruction(row, workflowId, stage) {
  const detail = $("[data-step-detail]", row);
  if (!detail.hidden) { detail.hidden = true; detail.innerHTML = ""; return; }
  detail.hidden = false;
  detail.innerHTML = '<pre>Loading exact instruction…</pre>';
  const key = `${workflowId}:${stage.id}`;
  try {
    let value = stagePromptCache.get(key);
    if (!value) {
      value = await api(`/api/workflows/${encodeURIComponent(workflowId)}/stage-prompt?stage=${encodeURIComponent(stage.id)}`);
      stagePromptCache.set(key, value);
    }
    const contextLine = (value.context || []).length ? `Receives: ${value.context.join(", ")}` : "Receives: nothing beyond the session";
    detail.innerHTML = "";
    const pre = document.createElement("pre");
    pre.textContent = `# ${value.prompt_file} — exact static instruction\n# ${contextLine}\n# The full transport prompt adds the orchestration law (new sessions), the listed context artifacts, and the strict contract.\n\n${value.template}`;
    detail.append(pre);
  } catch (error) {
    detail.innerHTML = `<pre>Instruction unavailable: ${escapeHtml(error.message)}</pre>`;
  }
}

function toggleStageEditor(row, workflow, stage, sharedWith) {
  const editor = $("[data-step-editor]", row);
  if (!editor.hidden) { editor.hidden = true; editor.innerHTML = ""; return; }
  const profile = effectiveProfile(workflow, stage.profile);
  editor.hidden = false;
  editor.innerHTML = `<div class="editor-grid"><label>Model<select data-field="model" aria-label="${escapeHtml(stage.title)} model"></select></label><label>Reasoning effort<select data-field="effort" aria-label="${escapeHtml(stage.title)} reasoning effort"></select></label></div><div data-catalog-custom hidden><label>Exact model ID<input data-field="custom-model" autocomplete="off"></label><label>Exact reasoning effort<input data-field="custom-effort" autocomplete="off"></label></div><p class="catalog-detail" data-catalog-detail hidden></p><p class="editor-note">Applies to route profile <strong>${escapeHtml(stage.profile)}</strong> for this run only${sharedWith.length ? ` — also used by: ${escapeHtml(sharedWith.join(", "))}` : ""}. Saved defaults are in Settings.</p><div class="editor-actions">${launchOverrides.has(stage.profile) ? '<button type="button" class="ghost-button" data-editor-reset>Reset to default</button>' : ""}<button type="button" class="primary-button" data-editor-apply>Apply to this run</button></div>`;
  installCatalogPicker(editor, profile.provider, profile.model, profile.effort);
  $("[data-editor-apply]", editor).addEventListener("click", () => {
    const selection = catalogSelection(editor);
    if (!selection.model || !selection.effort) { alert("Model and reasoning effort are required."); return; }
    launchOverrides.set(stage.profile, selection);
    renderNewRunPreflight();
  });
  $("[data-editor-reset]", editor)?.addEventListener("click", () => {
    launchOverrides.delete(stage.profile);
    renderNewRunPreflight();
  });
}

function renderRouteWarnings(workflow, stages) {
  const root = $("#route-warnings");
  const catalog = bootstrap?.catalog || {};
  const lines = [];
  const usedProfiles = [...new Set(stages.map((stage) => stage.profile))];
  const missing = [];
  for (const profileId of usedProfiles) {
    const profile = effectiveProfile(workflow, profileId);
    const entries = catalogModels(profile.provider);
    if (entries.length && !entries.some((entry) => entry.selection_token === profile.model)) {
      missing.push(`${profile.model} (${profileId})`);
    }
  }
  if (missing.length) lines.push(`Not in the local catalog — will run as unverified custom selections: ${missing.join(", ")}.`);
  for (const provider of ["codex", "claude"]) {
    const error = catalog.sources?.[provider]?.error || catalog.last_refresh?.sources?.[provider]?.error;
    if (!catalogModels(provider).length) {
      lines.push(`${provider === "codex" ? "Codex" : "Claude"} discovery unavailable${error ? `: ${error}` : ""} — refresh from Settings → Models.`);
    }
  }
  root.hidden = !lines.length;
  root.innerHTML = lines.map((line) => `<span>${escapeHtml(line)}</span>`).join("");
}

function orderedWorkflowStages(workflow) {
  const stages = workflow.stages || {};
  const ordered = [];
  const visited = new Set();
  const visit = (stageId) => {
    if (!stageId || visited.has(stageId) || !stages[stageId]) return;
    visited.add(stageId);
    const stage = stages[stageId];
    ordered.push(stage);
    for (const target of Object.values(stage.transitions || {})) {
      const value = String(target);
      const next = value.startsWith("@seal:") || value.startsWith("@complete:") ? value.split(":").at(-1) : value.startsWith("@") ? null : value;
      visit(next);
    }
  };
  visit(workflow.start_stage);
  for (const stageId of Object.keys(stages)) visit(stageId);
  return ordered;
}

function catalogModels(provider) {
  return (bootstrap?.catalog?.models || []).filter((model) => model.provider === provider);
}

function renderCatalogStatus() {
  const root = $("#catalog-status");
  if (!root) return;
  const catalog = bootstrap?.catalog || {};
  const refreshInfo = catalog.last_refresh;
  const verified = catalog.verified_at ? new Date(catalog.verified_at).toLocaleString() : "never";
  root.innerHTML = `<div class="catalog-meta"><span>${(catalog.models || []).length} selectable models</span><span>verified ${escapeHtml(verified)}</span>${catalog.stale ? '<span class="warn">stale — refresh recommended</span>' : ""}${refreshInfo && !refreshInfo.succeeded ? `<span class="warn">last refresh failed ${escapeHtml(new Date(refreshInfo.at).toLocaleString())}</span>` : ""}<button type="button" class="quiet-button" id="catalog-refresh">Refresh from installed CLIs</button></div>` +
    [["codex", "Codex CLI"], ["claude", "Claude Code"]].map(([provider, providerLabel]) => {
      const source = catalog.sources?.[provider] || refreshInfo?.sources?.[provider] || {};
      const models = catalogModels(provider);
      const rows = models.map((model) => `<li><strong>${escapeHtml(model.display_name || model.selection_token)}</strong><code>${escapeHtml(model.selection_token)}</code><span>${escapeHtml((model.supported_efforts || []).join(" · "))}</span>${model.special_modes?.length ? `<em>${escapeHtml(model.special_modes.join(", "))}</em>` : ""}</li>`).join("");
      const footnote = provider === "codex"
        ? "Discovered from the installed CLI (codex debug models) — reflects this account and build."
        : "Curated official manifest; the installed CLI is verified for --model/--effort support. Pass full model IDs — family aliases are unreliable headless.";
      return `<article class="catalog-provider"><header><strong>${providerLabel}</strong><span>${escapeHtml(source.cli_version || "version unknown")}</span></header>${source.error ? `<p class="catalog-error">Discovery failed: ${escapeHtml(source.error)}</p>` : ""}${rows ? `<ul>${rows}</ul>` : '<p class="settings-hint">No selectable models recorded. Refresh to query the installed CLI.</p>'}<p class="catalog-footnote">${footnote}</p></article>`;
    }).join("");
  $("#catalog-refresh")?.addEventListener("click", async (event) => {
    const button = event.target;
    button.disabled = true;
    button.textContent = "Refreshing…";
    try {
      await api("/api/catalog/refresh", {method: "POST", body: "{}"});
      await loadBootstrap();
    } catch (error) {
      alert(`Refresh failed: ${error.message}`);
      button.disabled = false;
      button.textContent = "Refresh from installed CLIs";
    }
  });
}

function installCatalogPicker(root, provider, model, effort) {
  const modelSelect = $('[data-field="model"]', root);
  const effortSelect = $('[data-field="effort"]', root);
  const customBox = $('[data-catalog-custom]', root);
  const customModel = $('[data-field="custom-model"]', root);
  const customEffort = $('[data-field="custom-effort"]', root);
  const detail = $('[data-catalog-detail]', root);
  const entries = catalogModels(provider);
  const entry = entries.find((candidate) => candidate.selection_token === model);
  modelSelect.innerHTML = `${entries.map((candidate) => `<option value="${escapeHtml(candidate.selection_token)}">${escapeHtml(candidate.display_name || candidate.selection_token)}</option>`).join("")}<option value="__custom__">Custom…</option>`;
  modelSelect.value = entry ? entry.selection_token : "__custom__";
  customModel.value = entry ? "" : (model || "");
  customEffort.value = entry ? "" : (effort || "");
  const sync = () => {
    const selected = entries.find((candidate) => candidate.selection_token === modelSelect.value);
    const custom = !selected;
    customBox.hidden = !custom;
    effortSelect.closest("label").hidden = custom;
    // The catalog provenance lives once in Settings → Models; per-card text
    // appears only for the deliberate custom escape hatch.
    if (custom) {
      detail.hidden = false;
      detail.textContent = "Not in the local catalog — runs as an unverified custom selection, recorded as evidence.";
      return;
    }
    detail.hidden = true;
    detail.textContent = "";
    const efforts = selected.supported_efforts || [];
    effortSelect.innerHTML = efforts.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    effortSelect.value = efforts.includes(effortSelect.value) ? effortSelect.value : (efforts.includes(effort) ? effort : (efforts[0] || ""));
  };
  effortSelect.value = effort || "";
  modelSelect.addEventListener("change", sync);
  customModel.addEventListener("input", sync);
  sync();
}

function catalogSelection(root) {
  const custom = $('[data-field="model"]', root).value === "__custom__";
  return {
    custom,
    model: custom ? $('[data-field="custom-model"]', root).value.trim() : $('[data-field="model"]', root).value,
    effort: custom ? $('[data-field="custom-effort"]', root).value.trim() : $('[data-field="effort"]', root).value,
  };
}

function renderSettings() {
  renderCatalogStatus();
  const workflows = bootstrap.workflows;
  settingsWorkflowId = workflows[settingsWorkflowId] ? settingsWorkflowId : Object.keys(workflows)[0];
  const selector = $("#settings-workflow");
  selector.innerHTML = Object.entries(workflows).map(([id, workflow]) => `<option value="${escapeHtml(id)}">${escapeHtml(workflow.label)}</option>`).join("");
  selector.value = settingsWorkflowId;
  const workflow = workflows[settingsWorkflowId];
  const root = $("#profile-settings");
  root.innerHTML = "";
  for (const [profileId, profile] of Object.entries(workflow?.profiles || {})) {
    const card = document.createElement("article");
    card.className = "profile-editor";
    // Replace the legacy free-text controls with catalog-backed selects. The
    // only text entry lives inside the explicit Custom… branch.
    card.innerHTML = `<header><strong>${escapeHtml(profile.label)}</strong><span>${escapeHtml(profile.id || profileId)} / ${escapeHtml(profile.provider)}</span></header><div class="profile-grid"><label>Model<select data-field="model" aria-label="${escapeHtml(profile.label)} model"></select></label><label>Reasoning effort<select data-field="effort" aria-label="${escapeHtml(profile.label)} reasoning effort"></select></label><button type="button" class="quiet-button" aria-label="Save ${escapeHtml(profile.label)} profile">Save</button></div><div data-catalog-custom hidden><label>Custom model<input data-field="custom-model" autocomplete="off"></label><label>Custom reasoning effort<input data-field="custom-effort" autocomplete="off"></label></div><p class="catalog-detail" data-catalog-detail></p>`;
    installCatalogPicker(card, profile.provider, profile.model, profile.effort);
    $("button", card).addEventListener("click", async () => {
      const selection = catalogSelection(card);
      if (!selection.model || !selection.effort) { alert("Custom model and reasoning effort are required."); return; }
      const payload = {workflow:workflow.id, profile:profile.id || profileId, ...selection};
      try {
        const saved = await api("/api/profile", {method:"POST",body:JSON.stringify(payload)});
        $('[data-field="model"]',card).value = saved.model;
        $('[data-field="effort"]',card).value = saved.effort;
        $("strong", card).textContent = saved.label;
        await loadBootstrap();
      } catch(error) { alert(`Not saved: ${error.message}. Reload to discard this draft.`); }
    });
    root.append(card);
  }
  const projects = $("#project-settings");
  projects.innerHTML = Object.entries(bootstrap.projects).map(([id, value]) => `<article class="project-card"><strong>${escapeHtml(id)}</strong><code>${escapeHtml(value.root)}</code><p>${value.implementation_enabled ? "Isolated implementation enabled" : "Read-only"} · ${escapeHtml(value.branch || "detached")} · ${value.dirty ? "source has local changes" : "source clean"} · ${escapeHtml((value.source_revision || "").slice(0,8))}</p></article>`).join("");
}

function showProjectForm() {
  if ($("#project-add-form")) return;
  const form = document.createElement("article");
  form.id = "project-add-form";
  form.className = "project-card";
  form.innerHTML = `<label>ID<input data-field="id" placeholder="my-repo"></label><label>Repository root<input data-field="root" placeholder="C:\\src\\my-repo"></label><label>Instruction files<input data-field="instructions" placeholder="AGENTS.md, docs/plan.md"></label><label>Implementation write paths<input data-field="write-paths" value="." placeholder="src, tests, docs"></label><label>Required local validation<input data-field="validation" value="python -m pytest -q" placeholder="python -m pytest -q"></label><button type="button" class="primary-button">Save repository</button>`;
  $("button", form).addEventListener("click", async () => {
    const id = $('[data-field="id"]',form).value.trim();
    const root = $('[data-field="root"]',form).value.trim();
    const instructions = $('[data-field="instructions"]',form).value.split(",").map((value)=>value.trim()).filter(Boolean);
    const writePaths = $('[data-field="write-paths"]',form).value.split(",").map((value)=>value.trim()).filter(Boolean);
    const validation = $('[data-field="validation"]',form).value.trim();
    const payload = {id,root,read_only:true,instruction_files:instructions,validations:[{id:"local-checks",environment:"local",command:validation,required:true}],implementation:{enabled:true,write_allowlist:writePaths,commit_on_accept:true,allow_no_validations:false,validation_requires_approval:true}};
    try { await api("/api/project",{method:"POST",body:JSON.stringify(payload)}); await loadBootstrap(); }
    catch(error){ alert(error.message); }
  });
  $("#project-settings").append(form);
}

function openNextTurnControl() {
  const workflow = bootstrap.workflows[currentState.workflow];
  const stage = workflow.stages[currentState.current_stage];
  const defaultProfile = workflow.profiles[stage.profile];
  const writeAllowed = stage.phase === "implementation" && stage.role === "implementer";
  const profiles = Object.values(workflow.profiles).filter((profile) => profile.provider === defaultProfile.provider && (writeAllowed || profile.permission !== "workspace-write"));
  const retryingFailure = currentState.pending_human_decision === "provider_invocation_failed";
  const overrideNote = retryingFailure
    ? "Apply saves this override. Then choose Retry on the failure gate to launch the displayed next provider turn."
    : "The override is recorded in the run and applies only to the displayed next provider turn.";
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  dialog.innerHTML = `<form method="dialog"><div class="modal-head"><div><p class="eyebrow">NEXT TURN</p><h2>${escapeHtml(stage.title)}</h2></div><button value="cancel" aria-label="Close override">×</button></div><label>Profile<select id="override-profile">${profiles.map((profile)=>`<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label)}</option>`).join("")}</select></label><div class="override-fields"><label>Model<select data-field="model"></select></label><label>Reasoning effort<select data-field="effort"></select></label></div><div data-catalog-custom hidden><label>Custom model<input data-field="custom-model" autocomplete="off"></label><label>Custom reasoning effort<input data-field="custom-effort" autocomplete="off"></label></div><p class="catalog-detail" data-catalog-detail></p><input id="override-model" type="hidden"><input id="override-effort" type="hidden"><label>Session action<select id="override-session"><option value="">Workflow default</option><option value="continue">Continue current session</option><option value="new">Start a new session</option></select></label><div class="override-preview" id="override-preview" aria-live="polite"></div><p class="override-note">${escapeHtml(overrideNote)}</p><div class="modal-actions"><button value="cancel" class="ghost-button">Cancel</button><button type="button" class="primary-button" id="save-override">Apply override</button></div></form>`;
  document.body.append(dialog);
  $("#override-profile",dialog).value = currentState.next_turn_override?.profile || stage.profile;
  $("#override-session",dialog).value = currentState.next_turn_override?.session_action || "";
  const selectedProfile = () => workflow.profiles[$("#override-profile", dialog).value];
  const refreshOverridePreview = ({resetValues = false} = {}) => {
    const profile = selectedProfile();
    if (resetValues) {
      $("#override-model", dialog).value = profile.model || "";
      $("#override-effort", dialog).value = profile.effort || "";
    }
    const session = $("#override-session", dialog).value || stage.session_policy;
    $("#override-preview", dialog).innerHTML = `<span>${escapeHtml(profile.provider)}</span><strong>${escapeHtml($("#override-model", dialog).value || "provider default")}</strong><span>${escapeHtml($("#override-effort", dialog).value || "default effort")}</span><span>${escapeHtml(profile.permission)}</span><span>${escapeHtml(session)}</span>`;
  };
  $("#override-model",dialog).value = currentState.next_turn_override?.profile_value?.model || selectedProfile().model || "";
  $("#override-effort",dialog).value = currentState.next_turn_override?.profile_value?.effort || selectedProfile().effort || "";
  const syncCatalogOverride = () => {
    const selection = catalogSelection(dialog);
    $("#override-model", dialog).value = selection.model;
    $("#override-effort", dialog).value = selection.effort;
  };
  const installOverridePicker = () => {
    installCatalogPicker(dialog, selectedProfile().provider, $("#override-model", dialog).value, $("#override-effort", dialog).value);
    $$("[data-field]", dialog).forEach((input) => input.addEventListener("input", () => { syncCatalogOverride(); refreshOverridePreview(); }));
    $$("select[data-field]", dialog).forEach((input) => input.addEventListener("change", () => { syncCatalogOverride(); refreshOverridePreview(); }));
  };
  installOverridePicker();
  $("#override-profile",dialog).addEventListener("change", () => refreshOverridePreview({resetValues:true}));
  $("#override-model",dialog).addEventListener("input", () => refreshOverridePreview());
  $("#override-effort",dialog).addEventListener("input", () => refreshOverridePreview());
  $("#override-session",dialog).addEventListener("change", () => refreshOverridePreview());
  $("#override-profile",dialog).addEventListener("change", () => { installOverridePicker(); syncCatalogOverride(); refreshOverridePreview(); });
  refreshOverridePreview();
  $("#save-override",dialog).addEventListener("click", async () => {
    try {
      syncCatalogOverride();
      const selection = catalogSelection(dialog);
      if (!selection.model || !selection.effort) throw new Error("Custom model and reasoning effort are required.");
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/override`, {method:"POST",body:JSON.stringify({profile:$("#override-profile",dialog).value,...selection,session_action:$("#override-session",dialog).value || null})});
      dialog.close();dialog.remove();await refreshCurrent();
    } catch(error){alert(error.message);}
  });
  dialog.addEventListener("close",()=>dialog.remove(),{once:true});
  dialog.showModal();
}

async function createRun() {
  const request = $("#new-request").value.trim();
  if (!request) { $("#new-request").focus(); return; }
  const button = $("#create-run");
  button.disabled = true;
  try {
    const workflowId = $("#new-workflow").value;
    const workflow = bootstrap?.workflows?.[workflowId] || {};
    const overrides = {};
    for (const [profileId, selection] of launchOverrides) {
      if (workflow.profiles?.[profileId]) overrides[profileId] = selection;
    }
    const result = await api("/api/runs", {method:"POST",body:JSON.stringify({project:$("#new-project").value,workflow:workflowId,run_mode:$("#new-run-mode").value,request,profile_overrides:Object.keys(overrides).length ? overrides : null})});
    $("#new-run-dialog").close();
    $("#new-request").value = "";
    launchOverrides.clear();
    await refreshRuns();
    await selectRun(result.run_id);
  } catch(error) { alert(error.message); }
  finally { button.disabled = false; }
}

function showBanner(message, kind) {
  let banner = $("#run-banner");
  if (!banner) { banner=document.createElement("div");banner.id="run-banner";banner.className="human-gate";$("#timeline").prepend(banner); }
  banner.innerHTML=`<p class="eyebrow">${escapeHtml(kind.toUpperCase())}</p><p>${escapeHtml(message)}</p>`;
}

function openRunRail() {
  railReturnFocus = document.activeElement;
  document.body.classList.add("mobile-rail-open");
  const rail = $("#run-rail");
  rail.inert = false;
  rail.setAttribute("aria-hidden", "false");
  $(".workspace").inert = true;
  $(".topbar").inert = true;
  $("#mobile-runs-button").setAttribute("aria-expanded", "true");
  $("#close-run-rail").focus();
}

function closeRunRail() {
  document.body.classList.remove("mobile-rail-open");
  $("#mobile-runs-button")?.setAttribute("aria-expanded", "false");
  if (window.matchMedia("(max-width: 720px)").matches) {
    const rail = $("#run-rail");
    rail.inert = true;
    rail.setAttribute("aria-hidden", "true");
    $(".workspace").inert = false;
    $(".topbar").inert = false;
    if (railReturnFocus?.isConnected) railReturnFocus.focus();
    railReturnFocus = null;
  }
}

function syncRunRail() {
  const mobile = window.matchMedia("(max-width: 720px)").matches;
  const rail = $("#run-rail");
  if (mobile && !document.body.classList.contains("mobile-rail-open")) {
    rail.inert = true;
    rail.setAttribute("aria-hidden", "true");
  } else if (!mobile) {
    rail.inert = false;
    rail.removeAttribute("aria-hidden");
    $(".workspace").inert = false;
    $(".topbar").inert = false;
  }
}

function applyTimelineDensity(value) {
  const numeric = Number(value);
  const density = numeric <= 84 ? "overview" : numeric >= 105 ? "detail" : "balanced";
  document.documentElement.style.setProperty("--zoom", numeric / 100);
  $("#timeline").dataset.density = density;
  $("#zoom-slider").setAttribute("aria-valuetext", `${humanizeReason(density)} density`);
}

function bindStaticEvents() {
  const openNew = () => $("#new-run-dialog").showModal();
  $("#new-run-button").addEventListener("click", openNew);
  $("#empty-new-run").addEventListener("click", openNew);
  $("#create-run").addEventListener("click", createRun);
  $("#settings-button").addEventListener("click", () => $("#settings-dialog").showModal());
  $("#mobile-runs-button").addEventListener("click", openRunRail);
  $("#close-run-rail").addEventListener("click", closeRunRail);
  window.addEventListener("resize", syncRunRail);
  syncRunRail();
  $("#add-project-button").addEventListener("click", showProjectForm);
  $("#new-workflow").addEventListener("change", () => { launchOverrides.clear(); renderNewRunPreflight(); });
  $$("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => {
    $$("[data-settings-tab]").forEach((other) => {
      const selected = other === button;
      other.classList.toggle("active", selected);
      other.setAttribute("aria-selected", String(selected));
    });
    $$("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== button.dataset.settingsTab; });
  }));
  $("#settings-workflow").addEventListener("change", (event) => { settingsWorkflowId = event.target.value; renderSettings(); });
  $("#refresh-button").addEventListener("click", async () => { await loadBootstrap(); if(currentRunId) await refreshCurrent(); });
  $("#close-inspector").addEventListener("click", closeInspector);
  $$(".inspector-tabs button").forEach((button) => button.addEventListener("click", () => selectInspectorTab(button.dataset.tab)));
  $("#zoom-slider").addEventListener("input", (event) => applyTimelineDensity(event.target.value));
  $("#phase-filter").addEventListener("change", () => currentState && renderTimeline(currentState));
  $("#session-filter").addEventListener("change", () => currentState && renderTimeline(currentState));
  $("#jump-active").addEventListener("click", () => ($("#active-node") || $("#active-gate"))?.scrollIntoView({behavior:"smooth",block:"center"}));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if ($("#inspector").getAttribute("aria-hidden") === "false") closeInspector();
    else if (document.body.classList.contains("mobile-rail-open")) closeRunRail();
  });
  applyTimelineDensity($("#zoom-slider").value);
}

bindStaticEvents();
loadBootstrap().catch((error) => {
  $("#connection-label").textContent = error.message;
  console.error(error);
});
