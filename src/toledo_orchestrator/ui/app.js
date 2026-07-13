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
  const heading = canRecover
    ? "Run interrupted — recovery available"
    : state.worker?.active
      ? `${state.inflight?.title || stageTitle(state.current_stage)} is running…`
      : stageTitle(state.current_stage);
  const retryReasons = new Set(["operator_step", "provider_requested_human", "provider_invocation_failed", "provider_session_id_missing", "provider_session_missing", "provider_session_not_new", "provider_session_changed_unexpectedly", "missing_substantive_output", "malformed_directive", "unsupported_stage_directive", "invalid_next_turn_profile", "profile_permission_exceeds_stage", "background_operation_failed"]);
  const canOverride = state.schema_version === "toledo_orchestrator.run.v2" && state.current_stage && !state.inflight && (state.status === "created" || state.status === "running" || retryReasons.has(state.pending_human_decision));
  const workflow = bootstrap.workflows[state.workflow];
  const stage = workflow?.stages?.[state.current_stage] || {};
  const profile = workflow?.profiles?.[state.next_turn_override?.profile || stage.profile] || {};
  const cost = (state.turns || []).reduce((sum, turn) => sum + Number(turn.usage?.total_cost_usd || 0), 0);
  const strip = $("#run-status-strip");
  strip.hidden = false;
  strip.innerHTML = `<span>${escapeHtml(state.status)}</span><span>${escapeHtml(stage.title || state.current_stage || "")}</span><span>${escapeHtml(profile.provider || "")}</span><span>${escapeHtml(profile.model || "")}</span><span>${escapeHtml(profile.effort || "")}</span><span>$${cost.toFixed(2)}</span><span>${state.worker?.active ? "worker active" : "worker idle"}</span>`;
  $("#run-header").innerHTML = `<div><p class="eyebrow">${escapeHtml(state.run_id)} · ${escapeHtml(state.status.toUpperCase())}</p><h2>${escapeHtml(heading || "Run complete")}</h2></div><div class="run-facts" id="run-facts"><span class="fact">cycle ${state.cycle || 1}</span><span class="fact">${state.current_turn || 0} turns</span><span class="fact">${escapeHtml(state.project)}</span><span class="fact">${escapeHtml((state.working_revision || state.source_revision || "").slice(0, 8))}</span>${state.execution_branch ? `<span class="fact">${escapeHtml(state.execution_branch)}</span>` : ''}${canOverride ? '<button class="quiet-button" id="next-turn-control">Override next turn ↗</button>' : ''}${canRecover ? '<button class="accept-button" id="recover-run">Recover run</button>' : ''}</div>`;
  $("#next-turn-control")?.addEventListener("click", openNextTurnControl);
  $("#recover-run")?.addEventListener("click", recoverCurrentRun);
  renderFilters(state);
  renderTimeline(state);
  if (state.worker?.error) showBanner(state.worker.error, "error");
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
}

function configureGate(fragment, state) {
  const reason = state.pending_human_decision || "human_decision";
  const gate = $(".human-gate", fragment);
  const title = $("[data-gate-title]", gate);
  const description = $("[data-gate-description]", gate);
  const textarea = $("[data-gate-text]", gate);
  const label = $("[data-gate-label]", gate);
  const yes = $('[data-choice="yes"]', gate);
  const no = $('[data-choice="no"]', gate);
  const other = $('[data-choice="other"]', gate);
  const copy = {
    operator_step: ["Ready for the next turn?", "Step mode paused before the next provider invocation. You can adjust the one-turn profile or session action above, add a concise direction, then continue.", "Run next turn", "", "", "Optional direction for the next turn"],
    next_task_approval: ["Is this the right next task?", "The proposal is preserved exactly. Accept it, finish the loop, or redirect the current strategic session.", "Yes — start planning", "No — finish here", "Other — revise proposal", "Tell the strategic session what to change"],
    validation_execution_approval: ["Run the validation commands?", "These commands execute on the host against the isolated implementation worktree. Review the pending commands before approving.", "Yes — run validation", "No — cancel run", "Other — send to repair", "Explain what session C must change before validation"],
    validation_receipt_required: ["Validation receipt required", `Attach the patch-bound receipt from a terminal with: python -m toledo_orchestrator validate ${state.run_id} --receipt-file "C:\\path\\to\\receipt.json"`, "", "No — cancel run", "Other — add direction", "Add receipt or validation guidance"],
    unknown_validation_execution: ["Validation completion is unknown", "The controller stopped after host validation started but before a trustworthy completion record was sealed. It will not rerun the commands automatically. Route the work to repair/inspection, cancel, or add exact recovery direction.", "Yes — inspect and repair", "No — cancel run", "Other — direct recovery", "Tell the implementation session what evidence to inspect before any rerun"],
    provider_invocation_failed: ["Provider invocation failed", "No successful model response was accepted. Set any one-turn model, effort, or session override above, then retry; the saved override will be used for that retry.", "Retry with displayed settings", "Cancel run", "Retry with direction", "Optional direction for the retried turn"],
    planning_round_cap_reached: ["Planning round cap reached", "The planning loop used its configured rounds without agreement. Extend it, stop, or redirect the next revision.", "Yes — extend one round", "No — cancel run", "Other — extend with direction", "Tell the planning sessions what must change"],
    implementation_round_cap_reached: ["Implementation round cap reached", "The implementation loop used its configured repair rounds. Extend it, stop, or direct one more repair.", "Yes — extend one round", "No — cancel run", "Other — extend with direction", "Tell the implementation sessions what must change"],
    validation_failed_at_repair_cap: ["Validation still fails", "Required validation failed after the configured repair rounds. Extend repair, stop, or give a specific recovery direction.", "Yes — extend repair", "No — cancel run", "Other — direct repair", "Describe the evidence or repair you require"],
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
  gate.dataset.reason = reason;
  if (reason === "operator_step") {
    no.hidden = true;
    other.hidden = true;
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

function renderNewRunPreflight() {
  const workflow = bootstrap?.workflows?.[$("#new-workflow").value];
  const root = $("#new-run-preflight");
  if (!workflow) {
    root.innerHTML = '<p class="route-preflight-empty">No route definition is available.</p>';
    $("#route-preflight-summary").textContent = "";
    return;
  }
  const stages = orderedWorkflowStages(workflow);
  $("#route-preflight-summary").textContent = `${stages.length} stages · ${workflow.label}`;
  root.innerHTML = stages.map((stage, index) => {
    const profile = workflow.profiles?.[stage.profile] || {};
    const label = stage.prompt_label || promptShort(stage.prompt_kind);
    return `<article class="route-preflight-card"><span class="route-index">${String(index + 1).padStart(2, "0")}</span><p>${escapeHtml(label)}</p><h4>${escapeHtml(stage.title)}</h4><dl><div><dt>Actor</dt><dd>${escapeHtml(stage.session_slot)}</dd></div><div><dt>Profile</dt><dd>${escapeHtml(profile.model || stage.profile)} · ${escapeHtml(profile.effort || "default")}</dd></div><div><dt>Access</dt><dd>${escapeHtml(profile.permission || "unspecified")}</dd></div><div><dt>Session</dt><dd>${escapeHtml(stage.session_policy)}</dd></div></dl></article>`;
  }).join("");
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

function renderSettings() {
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
    card.innerHTML = `<header><strong>${escapeHtml(profile.label)}</strong><span>${escapeHtml(profile.id || profileId)} · ${escapeHtml(profile.provider)}</span></header><div class="profile-grid"><input data-field="model" value="${escapeHtml(profile.model)}" aria-label="${escapeHtml(profile.label)} model"><input data-field="effort" value="${escapeHtml(profile.effort)}" aria-label="${escapeHtml(profile.label)} reasoning effort"><button type="button" class="quiet-button" aria-label="Save ${escapeHtml(profile.label)} profile">Save</button></div>`;
    $("button", card).addEventListener("click", async () => {
      const payload = {workflow:workflow.id, profile:profile.id || profileId, model:$('[data-field="model"]',card).value, effort:$('[data-field="effort"]',card).value};
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
  dialog.innerHTML = `<form method="dialog"><div class="modal-head"><div><p class="eyebrow">ONE-TURN OVERRIDE</p><h2>${escapeHtml(stage.title)}</h2></div><button value="cancel" aria-label="Close override">×</button></div><label>Profile<select id="override-profile">${profiles.map((profile)=>`<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label)}</option>`).join("")}</select></label><div class="override-fields"><label>Model<input id="override-model" autocomplete="off"></label><label>Reasoning effort<input id="override-effort" autocomplete="off"></label></div><label>Session action<select id="override-session"><option value="">Workflow default</option><option value="continue">Continue current session</option><option value="new">Start a new session</option></select></label><div class="override-preview" id="override-preview" aria-live="polite"></div><p class="override-note">${escapeHtml(overrideNote)}</p><div class="modal-actions"><button value="cancel" class="ghost-button">Cancel</button><button type="button" class="primary-button" id="save-override">Apply override</button></div></form>`;
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
  $("#override-profile",dialog).addEventListener("change", () => refreshOverridePreview({resetValues:true}));
  $("#override-model",dialog).addEventListener("input", () => refreshOverridePreview());
  $("#override-effort",dialog).addEventListener("input", () => refreshOverridePreview());
  $("#override-session",dialog).addEventListener("change", () => refreshOverridePreview());
  refreshOverridePreview();
  $("#save-override",dialog).addEventListener("click", async () => {
    try {
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/override`, {method:"POST",body:JSON.stringify({profile:$("#override-profile",dialog).value,model:$("#override-model",dialog).value.trim() || null,effort:$("#override-effort",dialog).value.trim() || null,session_action:$("#override-session",dialog).value || null})});
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
    const result = await api("/api/runs", {method:"POST",body:JSON.stringify({project:$("#new-project").value,workflow:$("#new-workflow").value,run_mode:$("#new-run-mode").value,request})});
    $("#new-run-dialog").close();
    $("#new-request").value = "";
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
  $("#new-workflow").addEventListener("change", renderNewRunPreflight);
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
