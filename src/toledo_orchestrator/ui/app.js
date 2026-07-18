"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const previewText = (value) => String(value ?? "")
  .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
  .replace(/(^|\s)#{1,6}\s+/g, "$1")
  .replace(/[*_~`]+/g, "")
  .replace(/\s+/g, " ")
  .trim();
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
let defaultsSavedFlash = null;
const promptPreviewCache = new Map();
const gateDrafts = new Map();
const openQuickTakes = new Set();

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

async function copyPlainText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      // Some embedded Chromium shells expose writeText but leave its promise
      // pending forever. Bound that path so the visible control cannot hang.
      const copied = await Promise.race([
        navigator.clipboard.writeText(text).then(() => true),
        new Promise((resolve) => setTimeout(() => resolve(false), 1200)),
      ]);
      if (copied) return;
    } catch {
      // Local HTTP deployments and embedded browsers may deny the modern API.
      // Fall through to the selection-based copy path instead of losing Copy.
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("This browser did not allow clipboard access.");
}

async function plainTextRunExport() {
  return api(`/api/runs/${encodeURIComponent(currentRunId)}/export?format=text`);
}

function downloadPlainText(filename, text) {
  const url = URL.createObjectURL(new Blob([text], {type: "text/plain;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
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
  openQuickTakes.clear();
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
    steer: state.steer,
    quickTakes: (state.turns || []).map((turn) => [turn.id, turn.quick_take]),
  });
}

async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    let changed = true;
    try {
      const head = await api(`/api/runs/${encodeURIComponent(currentRunId)}/head`);
      const signature = JSON.stringify([head.event_sequence, head.summary_sequence, head.status, head.current_turn, head.pending_human_decision, head.worker]);
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
    stopped: "Stopped by operator — work may be incomplete",
    failed: "Run failed",
  };
  const heading = canRecover
    ? "Run interrupted — recovery available"
    : state.worker?.active
      ? `${state.inflight?.title || stageTitle(state.current_stage)} is running…`
      : terminalHeadings[state.status] || stageTitle(state.current_stage);
  // A run is bound to its launch-time route snapshot. Saved defaults may
  // change while it is paused, but the status strip must advertise what this
  // run will actually invoke, not the mutable bootstrap default.
  const workflow = state.workflow_snapshot || bootstrap.workflows[state.workflow];
  const stage = workflow?.stages?.[state.current_stage] || {};
  const override = state.next_turn_override;
  const overrideValue = override?.target_stage === state.current_stage ? override?.profile_value : null;
  const profile = overrideValue || workflow?.profiles?.[override?.profile || stage.profile] || {};
  const cost = (state.turns || []).reduce((sum, turn) => sum + Number(turn.usage?.total_cost_usd || 0), 0);
  const strip = $("#run-status-strip");
  strip.hidden = false;
  strip.innerHTML = `<span>${escapeHtml(state.status)}</span><span>${escapeHtml(stage.title || state.current_stage || "")}</span><span>${escapeHtml(profile.provider || "")}</span><span>${escapeHtml(profile.model || "")}</span><span>${escapeHtml(profile.effort || "")}</span><span>$${cost.toFixed(2)}</span><span>${state.worker?.active ? "worker active" : "worker idle"}</span>`;
  $("#run-header").innerHTML = `<div><p class="eyebrow">${escapeHtml(state.status.toUpperCase())}</p><h2>${escapeHtml(heading || "Run complete")}</h2></div><div class="run-facts" id="run-facts"><span class="fact">${escapeHtml(state.project)}</span><span class="fact">${escapeHtml((state.working_revision || state.source_revision || "").slice(0, 8))}</span><button class="quiet-button run-action" id="export-run" title="Download the full conversation and transport prompts as plain text">Export plain text</button><button class="quiet-button run-action" id="copy-run" title="Copy the same full plain-text conversation">Copy all</button>${canRecover ? '<button class="accept-button run-action" id="recover-run">Recover run</button>' : ''}</div>`;
  $("#export-run")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Preparing…";
    try {
      const text = await plainTextRunExport();
      downloadPlainText(`${String(currentRunId).replace(/[^a-zA-Z0-9._-]/g, "-")}.txt`, text);
      button.textContent = "Exported";
    } catch (error) {
      button.textContent = "Export failed";
      alert(`Export failed: ${error.message}`);
    } finally {
      setTimeout(() => { button.textContent = "Export plain text"; button.disabled = false; }, 2200);
    }
  });
  $("#copy-run")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Copying…";
    try {
      const text = await plainTextRunExport();
      await copyPlainText(text);
      button.textContent = `Copied ${(text.length / 1000).toFixed(0)}k chars`;
    } catch (error) {
      button.textContent = "Copy failed";
      alert(`Copy failed: ${error.message}`);
    } finally {
      setTimeout(() => { button.textContent = "Copy all"; button.disabled = false; }, 2500);
    }
  });
  $("#recover-run")?.addEventListener("click", recoverCurrentRun);
  renderFilters(state);
  renderTimeline(state);
  if (state.worker?.error) showBanner(state.worker.error, "error");
}

function steerTargetForState(state) {
  // The controller validates stage, active physical session, provider, and
  // exact replacement target. Never infer steerability from a same-slot turn:
  // a slot can span stages while only the current-stage artifact is legal.
  if (!state?.steer?.available || !state.steer.turn_id) return null;
  return (state.turns || []).find((turn) => turn.id === state.steer.turn_id) || null;
}

function openSteerControl(latest = steerTargetForState(currentState)) {
  if (!latest) {
    alert(currentState?.steer?.reason || "There is no current-stage artifact in the active provider session to reply to.");
    return;
  }
  const requestedModel = latest.configured_model || latest.model || "model unknown";
  const requestedEffort = latest.configured_reasoning || "effort unknown";
  const observedModel = latest.observed_model || "not reported";
  const observedEffort = latest.observed_reasoning || "not reported";
  const observation = observedModel !== requestedModel || observedEffort !== requestedEffort
    ? ` The last response reported ${observedModel} · ${observedEffort}; that evidence is shown separately and is not what this control resends.`
    : ` The last response reported the same model and effort.`;
  const excerpt = previewText(latest.preview || "Output preview unavailable. Open the artifact from the timeline to inspect it.").slice(0, 700);
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  dialog.innerHTML = `<form method="dialog"><div class="modal-head"><div><p class="eyebrow">REPLY IN THE ACTIVE SESSION</p><h2>Revise ${escapeHtml(latest.title || "the current artifact")}</h2></div><button value="cancel" aria-label="Close">×</button></div><p class="settings-hint">This resumes ${escapeHtml(sessionDisplay(latest))} with the requested route ${escapeHtml(latest.provider || "provider")} · ${escapeHtml(requestedModel)} · ${escapeHtml(requestedEffort)}.${escapeHtml(observation)} It cannot switch model or session. The response replaces this artifact without consuming a workflow review round; both versions remain sealed.</p><section class="steer-context" aria-label="Artifact being revised"><strong>Artifact being revised</strong><p>${escapeHtml(excerpt)}</p></section><label>Your follow-up<textarea id="steer-note" rows="6" placeholder="What should change, be reconsidered, or be answered?"></textarea></label><div class="modal-actions"><button value="cancel" class="ghost-button">Cancel</button><button type="button" class="primary-button" id="steer-send">Send and replace artifact</button></div></form>`;
  document.body.append(dialog);
  $("#steer-send", dialog).addEventListener("click", async (event) => {
    const note = $("#steer-note", dialog).value.trim();
    if (!note) { $("#steer-note", dialog).focus(); return; }
    event.target.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/steer`, {method:"POST", body:JSON.stringify({note})});
      dialog.close();
      dialog.remove();
      await refreshCurrent();
    } catch (error) { alert(error.message); event.target.disabled = false; }
  });
  dialog.addEventListener("close", () => dialog.remove(), {once: true});
  dialog.showModal();
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
  const steerTarget = steerTargetForState(state);
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
      block.append(turnRow(turn, {canSteer: steerTarget?.id === turn.id}));
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
  if (activeGate && state.pending_human_decision) {
    // A fast click must still wait for the next-turn controls to hydrate. Once
    // visible, their current values travel in the same Run/Retry request.
    activeGate.gatePickerReady = populateGateUpnext(activeGate, state);
  }
  hydratePromptPreviews();
  hydrateDecisionPreviews();
}

function turnEvidence(turn) {
  const configuredModel = turn.configured_model || turn.model || "model unknown";
  const configuredEffort = turn.configured_reasoning || turn.effort || "effort unknown";
  const hasObserved = Boolean(turn.observed_model || turn.observed_reasoning);
  const observedModel = turn.observed_model || "model not reported";
  const observedEffort = turn.observed_reasoning || "effort not reported";
  const comparableModel = !["", "provider-default", "model unknown"].includes(configuredModel);
  const comparableEffort = !["", "provider-default", "default", "effort unknown"].includes(configuredEffort);
  const mismatch = (turn.observed_model && comparableModel && turn.observed_model !== configuredModel)
    || (turn.observed_reasoning && comparableEffort && turn.observed_reasoning !== configuredEffort);
  return {
    configured: `${configuredModel} · ${configuredEffort}`,
    observed: hasObserved ? `${observedModel} · ${observedEffort}` : `Not reported${turn.observation_error ? ` · ${turn.observation_error}` : ""}`,
    mismatch: Boolean(mismatch),
  };
}

function quickTakeModelLabel(model) {
  const value = String(model || "local model");
  if (value.toLowerCase().startsWith("gemma4")) return "Gemma 4";
  return value;
}

function quickTakeMarkup(turn) {
  const quickTake = turn.quick_take;
  if (!quickTake) return "";
  const model = quickTakeModelLabel(quickTake.model);
  const disclosure = `data-quick-take="${escapeHtml(turn.id)}"${openQuickTakes.has(turn.id) ? " open" : ""}`;
  if (quickTake.status === "ready") {
    return `<details class="turn-quick-take" ${disclosure}><summary><span>Quick take</span><span class="quick-take-state ready">${escapeHtml(model)} · ready</span></summary><p>${escapeHtml(quickTake.text || "Digest unavailable.")}</p></details>`;
  }
  if (["queued", "writing", "missing"].includes(quickTake.status)) {
    return `<details class="turn-quick-take pending" ${disclosure}><summary><span>Quick take</span><span class="quick-take-state writing" aria-live="polite">${escapeHtml(model)} is writing…</span></summary><p>${escapeHtml(model)} is reading this turn. The relay does not wait on this digest.</p></details>`;
  }
  const retryNote = quickTake.retry_after ? " Reopen this run shortly to retry." : "";
  return `<details class="turn-quick-take failed" ${disclosure}><summary><span>Quick take</span><span class="quick-take-state failed">Unavailable</span></summary><p>${escapeHtml(quickTake.error || "The local digest could not be generated. Full output is unaffected.")}${escapeHtml(retryNote)}</p></details>`;
}

function turnRow(turn, {canSteer = false} = {}) {
  const row = document.createElement("div");
  row.className = "timeline-row";
  row.dataset.phase = turn.phase;
  row.dataset.session = turn.session_label;
  const colorClass = sessionColorClass(turn.session_label);
  const preview = previewText(turn.preview || "Output preview unavailable — open the stored artifact.").slice(0, 520);
  const evidence = turnEvidence(turn);
  const interstitialFile = turn.direction_file || turn.interstitial_file || turn.prompt_file;
  const tooltipId = `direction-${String(turn.id || "turn").replaceAll(".", "-")}`;
  row.innerHTML = `<button class="prompt-node" data-interstitial-path="${escapeHtml(turnArtifactPath(interstitialFile))}" aria-describedby="${escapeHtml(tooltipId)}" aria-label="Open ${escapeHtml(turn.prompt_label || turn.title)} direction"><span class="prompt-label">${escapeHtml(turn.prompt_label || promptShort(turn.prompt_kind))}</span><span class="prompt-tooltip" id="${escapeHtml(tooltipId)}" role="tooltip">Loading exact direction…</span></button><article class="turn-card ${escapeHtml(turn.provider)} ${colorClass}"><div class="turn-card-head"><div class="actor"><span class="session-token">${escapeHtml(sessionDisplay(turn))}</span><div><h3>${escapeHtml(turn.title)}</h3><span class="route">${escapeHtml(turn.provider)} · ${escapeHtml(turn.role)}</span></div></div><span class="turn-number">${escapeHtml(turn.id)}</span></div>${quickTakeMarkup(turn)}<div class="turn-preview"><span class="turn-preview-tag">Output excerpt</span><p>${escapeHtml(preview)}</p><div class="turn-preview-actions"><button type="button" data-open-output>Open full output</button><button type="button" data-copy-output>Copy output</button>${canSteer ? '<button type="button" data-steer-output>Reply / revise</button>' : ""}</div></div><div class="turn-evidence ${evidence.mismatch ? "mismatch" : ""}"><span><b>Configured</b> ${escapeHtml(evidence.configured)}</span><span><b>Observed</b> ${escapeHtml(evidence.observed)}</span>${evidence.mismatch ? '<strong>Mismatch</strong>' : ""}</div><div class="chips"><span class="chip ${escapeHtml(turn.session_action)}">${escapeHtml(turn.session_action)} session</span><span class="chip">${escapeHtml(turn.permission)}</span><span class="chip">${Math.round((turn.elapsed_ms || 0)/1000)}s</span></div></article>`;
  const card = $(".turn-card", row);
  const prompt = $(".prompt-node", row);
  $(".turn-quick-take", card)?.addEventListener("toggle", (event) => {
    const turnId = event.currentTarget.dataset.quickTake;
    if (event.currentTarget.open) openQuickTakes.add(turnId);
    else openQuickTakes.delete(turnId);
  });
  $("[data-open-output]", card).addEventListener("click", () => openTurn(turn, "output"));
  $("[data-copy-output]", card).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Copying…";
    try {
      const output = await artifactText(turnArtifactPath(turn.output_file));
      await copyPlainText(output);
      button.textContent = "Copied";
    } catch (error) {
      button.textContent = "Copy failed";
      alert(`Copy failed: ${error.message}`);
    } finally {
      setTimeout(() => { button.textContent = "Copy output"; button.disabled = false; }, 1800);
    }
  });
  $("[data-steer-output]", card)?.addEventListener("click", () => openSteerControl(turn));
  prompt.addEventListener("click", () => openTurn(turn, turn.direction_file ? "direction" : "stance"));
  return row;
}

function promptShort(kind) {
  const names = {ideate:"Ideas",skeptic:"Skeptic",adjudicate:"Judge","implementation-kickoff":"Build","implementation-audit":"Audit",correction:"Correct","next-step":"Next","human-other":"Redirect"};
  return names[kind] || kind?.slice(0,8) || "Prompt";
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
  const evidence = turnEvidence(turn);
  $("#inspector-meta").innerHTML = `<dl class="inspector-evidence ${evidence.mismatch ? "mismatch" : ""}"><div><dt>Configured</dt><dd>${escapeHtml(evidence.configured)}</dd></div><div><dt>Observed</dt><dd>${escapeHtml(evidence.observed)}</dd></div><div><dt>Session</dt><dd>${escapeHtml(sessionDisplay(turn))} · ${escapeHtml(turn.session_action)} · ${escapeHtml(sessionIdSuffix(turn.session_id))}</dd></div><div><dt>Access</dt><dd>${escapeHtml(turn.permission)}</dd></div></dl>`;
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
    const submitDisplayedOverride = choice !== "no" && ["operator_step", "provider_invocation_failed"].includes(gate.dataset.reason);
    let actionAccepted = false;
    try {
      if (submitDisplayedOverride && gate.gatePickerReady) await gate.gatePickerReady;
      const nextTurnOverride = submitDisplayedOverride && gate.collectNextTurnOverride
        ? gate.collectNextTurnOverride()
        : null;
      if (gate.dataset.reason === "operator_step") {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/continue`, {method:"POST", body:JSON.stringify({
          direction: textarea.value,
          next_turn_override: nextTurnOverride,
        })});
      } else {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/decision`, {method:"POST", body:JSON.stringify({
          choice,
          text: choice === "other" ? textarea.value : "",
          next_turn_override: nextTurnOverride,
        })});
      }
      actionAccepted = true;
      gateDrafts.delete(gate.dataset.draftKey);
      await refreshCurrent();
    } catch (error) {
      const prefix = actionAccepted
        ? "The action was accepted, but this page could not refresh. Reload before trying again. "
        : submitDisplayedOverride
          ? "Displayed settings were rejected before any provider operation was scheduled. "
          : "The action was not accepted. ";
      alert(prefix + error.message);
      button.disabled = false;
    }
  }));
  $("[data-gate-stop]", gate)?.addEventListener("click", async (event) => {
    if (!window.confirm("Stop this run here? It is recorded as stopped by you — nothing is committed, and the work may be incomplete.")) return;
    event.target.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/stop`, {method:"POST", body:JSON.stringify({note:textarea.value})});
      gateDrafts.delete(gate.dataset.draftKey);
      await refreshCurrent();
      await refreshRuns();
    } catch (error) { alert(error.message); event.target.disabled = false; }
  });
}

function gateProviderSwitchable(preview) {
  // A provider-invocation failure unlocks provider switching even on stages
  // that lock theirs: the locked provider is unavailable (quota, outage,
  // network) and the operator needs a route around it. Mirrors the backend
  // rescue rule in set_next_turn_override.
  return Boolean(preview.stage?.provider_switchable)
    || preview.pending_human_decision === "provider_invocation_failed";
}

function gateProfileOptions(workflow, preview) {
  const writeAllowed = preview.stage?.phase === "implementation" && preview.stage?.role === "implementer";
  return Object.entries(workflow?.profiles || {})
    .map(([id, value]) => ({id, ...value}))
    .filter((candidate) => writeAllowed || candidate.permission !== "workspace-write")
    .filter((candidate) => gateProviderSwitchable(preview) || candidate.provider === preview.profile?.provider);
}

function gateProfileLabel(profile) {
  const prefix = `${profile.provider || ""}-`;
  const purposeId = String(profile.id || "route").startsWith(prefix)
    ? String(profile.id).slice(prefix.length)
    : String(profile.id || "route");
  return `${humanizeReason(purposeId)} · ${profile.permission || "default access"} · ${profile.timeout_seconds || "?"}s`;
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
  const workflow = state.workflow_snapshot || bootstrap?.workflows?.[state.workflow] || {};
  const candidateProfiles = gateProfileOptions(workflow, preview);
  panel.innerHTML = `<div class="gate-upnext-line"><span class="gate-applied-label">Applied</span><strong>${escapeHtml(profile.model || "provider default")}</strong><span>·</span><span>${escapeHtml(profile.effort || "default effort")}</span><span>·</span><span>${escapeHtml(profile.provider || "")}</span><span>·</span><span>${escapeHtml(profile.permission || "")}</span>${preview.override?.active ? '<span class="override-chip">one-turn override active</span>' : ""}${profile.custom ? '<span class="warn-chip">custom — unverified</span>' : ""}</div>
    <dl>
      <div><dt>Actor</dt><dd>${escapeHtml(session.label || "?")} (${escapeHtml(preview.stage?.role || "agent")}) · ${escapeHtml(session.action || "?")} session</dd></div>
      <div><dt>Receives</dt><dd>${escapeHtml(receives)}</dd></div>
      <div><dt>Produces</dt><dd>${escapeHtml(String(preview.stage?.produces || "").replaceAll("-", " "))}</dd></div>
      <div><dt>Afterward</dt><dd>${dedupedAfterward.map((line) => escapeHtml(line)).join("<br>")}</dd></div>
      ${preview.rounds ? `<div><dt>Rounds</dt><dd>${preview.rounds.used} of ${preview.rounds.cap} used</dd></div>` : ""}
      ${preview.direction_preview ? `<div><dt>Direction</dt><dd>${escapeHtml(preview.direction_preview.replace(/\s+/g, " ").slice(0, 220))}</dd></div>` : ""}
    </dl>
    <div class="gate-picker">
      <label>Provider<select data-gate-provider aria-label="Next turn provider"></select></label>
      <label title="The provider, access, and timeout policy. Model and effort are selected separately below.">Access preset<select data-gate-profile aria-label="Next turn access preset"></select></label>
      <label>Model<select data-field="model" aria-label="Next turn model"></select></label>
      <label data-gate-effort>Effort<select data-field="effort" aria-label="Next turn reasoning effort"></select></label>
      <label>Session<select data-field="session" aria-label="Next turn session action"><option value="">As planned (${escapeHtml(session.action || "?")})</option><option value="continue">Continue current session</option><option value="new">Start a new session</option></select></label>
      <button type="button" class="quiet-button" data-gate-apply title="Save this one-turn setup and refresh the exact prompt without running it">Save without running</button>
    </div>
    <div data-catalog-custom hidden><label>Exact model ID<input data-field="custom-model" autocomplete="off"></label><label>Exact reasoning effort<input data-field="custom-effort" autocomplete="off"></label></div>
    <p class="catalog-detail" data-catalog-detail hidden></p>
    <p class="gate-session-rule" data-gate-session-rule></p>`;
  const providerSelect = $("[data-gate-provider]", panel);
  const profileSelect = $("[data-gate-profile]", panel);
  const sessionSelect = $('[data-field="session"]', panel);
  const providers = [...new Set(candidateProfiles.map((candidate) => candidate.provider))];
  providerSelect.innerHTML = providers.map((providerName) => `<option value="${escapeHtml(providerName)}">${escapeHtml(providerName)}</option>`).join("");
  providerSelect.value = providers.includes(profile.provider) ? profile.provider : (providers[0] || profile.provider || "");
  providerSelect.disabled = providers.length < 2;
  providerSelect.title = providers.length < 2 && !gateProviderSwitchable(preview)
    ? "This workflow stage locks the provider. Model and effort can still change."
    : (gateProviderSwitchable(preview) && !preview.stage?.provider_switchable
      ? "Unlocked for this retry: the planned provider failed, so you may route this turn to another provider. A provider change starts a new physical session."
      : "");
  const fillProfiles = (preferredId = null) => {
    const matching = candidateProfiles.filter((candidate) => candidate.provider === providerSelect.value);
    profileSelect.innerHTML = matching.map((candidate) => `<option value="${escapeHtml(candidate.id)}">${escapeHtml(gateProfileLabel(candidate))}</option>`).join("");
    profileSelect.value = matching.some((candidate) => candidate.id === preferredId) ? preferredId : (matching[0]?.id || "");
  };
  const selectedProfile = () => candidateProfiles.find((candidate) => candidate.id === profileSelect.value) || candidateProfiles[0] || profile;
  const updateSessionRule = () => {
    const selected = selectedProfile();
    const selection = catalogSelection(panel);
    const providerChanged = selected.provider !== profile.provider;
    const modelChanged = selection.model !== profile.model;
    const effortChanged = selection.effort !== profile.effort;
    const planned = $("option[value='']", sessionSelect);
    const continuing = $("option[value='continue']", sessionSelect);
    const canContinue = Boolean(
      session.has_active_session
      && session.active_provider === selected.provider
      && !providerChanged
    );
    planned.disabled = providerChanged;
    continuing.disabled = !canContinue;
    if (providerChanged) sessionSelect.value = "new";
    else if (!canContinue && sessionSelect.value === "continue") sessionSelect.value = "";
    const resolvedAction = sessionSelect.value || session.action;
    const selectedCatalogModel = catalogModels(selected.provider).find((item) => item.selection_token === selection.model);
    const resumeObserved = selectedCatalogModel?.live_verified?.resume_switch_observed;
    const resumeEvidence = resumeObserved
      ? `Live-tested on this host${selectedCatalogModel.live_verified?.at ? ` (${new Date(selectedCatalogModel.live_verified.at).toLocaleDateString()})` : ""}; the result will still show configured versus observed model.`
      : "Current CLI behavior permits this, but this target has not been live-tested on this host; the result will show configured versus observed model.";
    let rule;
    if (providerChanged) {
      rule = "A provider change requires a new physical session; prior session context will not carry over. The next prompt includes the sealed workflow inputs.";
    } else if (!canContinue) {
      rule = `No active ${selected.provider} session exists in this stage's session slot. The next turn will start a new physical session.`;
    } else if (modelChanged && resolvedAction === "continue") {
      rule = `This same-provider model switch will continue the active session. ${resumeEvidence}`;
    } else if (modelChanged) {
      rule = "This model switch will start a new physical session. The next prompt includes the sealed workflow inputs instead of relying on prior session context.";
    } else if (effortChanged && resolvedAction === "continue") {
      rule = "This effort change will continue the active session. CLI acceptance is not proof of effective effort; observed evidence will be shown when the provider reports it.";
    } else {
      rule = gateProviderSwitchable(preview)
        ? (preview.stage?.provider_switchable
          ? "Provider changes require a new physical session. Same-provider model or effort changes may continue the active session."
          : "Provider switching is unlocked for this retry because the planned provider failed. A provider change starts a new physical session; same-provider model or effort changes may continue the active session.")
        : "This stage keeps its provider. Same-provider model or effort changes may continue the active session.";
    }
    $("[data-gate-session-rule]", panel).textContent = rule;
  };
  const installSelectedProfile = (usePreviewValues = false) => {
    const selected = selectedProfile();
    installCatalogPicker(
      panel,
      selected.provider,
      usePreviewValues ? profile.model : selected.model,
      usePreviewValues ? profile.effort : selected.effort,
      updateSessionRule,
    );
    updateSessionRule();
  };
  fillProfiles(profile.id);
  installSelectedProfile(true);
  sessionSelect.value = session.overridden ? session.action : "";
  updateSessionRule();
  providerSelect.addEventListener("change", () => {
    fillProfiles();
    installSelectedProfile();
  });
  profileSelect.addEventListener("change", () => installSelectedProfile());
  sessionSelect.addEventListener("change", updateSessionRule);
  gate.collectNextTurnOverride = () => {
    const selection = catalogSelection(panel);
    if (!selection.model || !selection.effort) throw new Error("Model and reasoning effort are required.");
    return {
      profile: profileSelect.value || preview.profile?.id || null,
      ...selection,
      session_action: sessionSelect.value || null,
    };
  };
  $("[data-gate-apply]", panel).addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/override`, {
        method: "POST",
        body: JSON.stringify(gate.collectNextTurnOverride()),
      });
      await refreshCurrent(true);
    } catch (error) { alert(error.message); event.target.disabled = false; }
  });
  panel.hidden = false;
  tools.hidden = false;
  const canResumeCutOff = preview.pending_human_decision === "provider_invocation_failed"
    && session.has_active_session
    && session.active_provider === profile.provider;
  if (canResumeCutOff && !$("[data-gate-continue]", gate)) {
    const quick = document.createElement("div");
    quick.className = "gate-quick-retry";
    quick.innerHTML = `<button type="button" class="accept-button" data-gate-continue title="For cut-offs outside the relay's control — quota limits, lost connection, provider outage. Once capacity is back (for example after the limit resets), this resumes the same ${escapeHtml(profile.provider || "")} session with the same settings and tells it: we were cut off, please continue. The gate below closes.">We were cut off — resume &amp; continue</button><p>Same session, same settings. Use after the quota resets or the connection recovers; to switch provider or model instead, use the controls below.</p>`;
    panel.before(quick);
    $("[data-gate-continue]", quick).addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/decision`, {method: "POST", body: JSON.stringify({
          choice: "other",
          text: "We were cut off by an issue outside this relay's control (quota, network, or provider outage). Please continue exactly where you left off and complete this turn in full.",
          next_turn_override: {
            profile: profile.id || null,
            model: profile.model || null,
            effort: profile.effort || null,
            custom: Boolean(profile.custom),
            session_action: "continue",
          },
        })});
        gateDrafts.delete(gate.dataset.draftKey);
        await refreshCurrent();
      } catch (error) { alert(error.message); event.target.disabled = false; }
    });
  }
  $("[data-gate-prompt]", gate).onclick = () => {
    inspectorPayload = {
      transport: preview.prompt || `Prompt preview unavailable: ${preview.prompt_error || "unknown"}`,
      metadata: JSON.stringify({stage: preview.stage, profile: preview.profile, session: preview.session, inputs: preview.inputs}, null, 2),
    };
    setDirectionTabLabel("Situational");
    $("#inspector-kicker").textContent = "EXACT NEXT PROMPT · PREVIEW";
    $("#inspector-title").textContent = preview.stage?.title || "Next turn";
    $("#inspector-meta").innerHTML = `<dl class="inspector-evidence"><div><dt>Planned</dt><dd>${escapeHtml(profile.provider || "")} · ${escapeHtml(profile.model || "provider default")} · ${escapeHtml(profile.effort || "default effort")}</dd></div><div><dt>Session</dt><dd>${escapeHtml(session.label || "?")} · ${escapeHtml(session.action || "?")}</dd></div><div><dt>Access</dt><dd>${escapeHtml(profile.permission || "")}</dd></div></dl>`;
    openInspector("transport");
  };
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
    provider_invocation_failed: ["Provider invocation failed", "No successful model response was accepted (quota, network, or CLI failure). If the cause was outside the relay's control, resume the interrupted session once capacity is back. Or use the provider, model, effort, and session controls below — provider switching is unlocked at this gate — then retry; the override applies to the retried turn.", "Retry with displayed settings", "Cancel run", "Retry with direction", "Optional direction for the retried turn"],
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
  const text = String(reason || "review required").replace(/[_-]+/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function populateNewRun() {
  const selectedProject = $("#new-project").value;
  const selectedWorkflow = $("#new-workflow").value;
  $("#new-project").innerHTML = Object.entries(bootstrap.projects).map(([id, project]) => `<option value="${escapeHtml(id)}">${escapeHtml(id)} · ${escapeHtml(project.root)}</option>`).join("");
  $("#new-workflow").innerHTML = Object.entries(bootstrap.workflows).map(([id, workflow]) => `<option value="${escapeHtml(id)}">${escapeHtml(workflow.label)}</option>`).join("");
  if (bootstrap.projects?.[selectedProject]) $("#new-project").value = selectedProject;
  const initialWorkflow = bootstrap.workflows?.[selectedWorkflow]
    ? selectedWorkflow
    : (bootstrap.workflows?.["continuous-development"] ? "continuous-development" : Object.keys(bootstrap.workflows || {})[0]);
  if (initialWorkflow) $("#new-workflow").value = initialWorkflow;
  renderNewRunPreflight();
}

const launchOverrides = new Map();
const launchRoundOverrides = new Map();
const launchPromptOverrides = new Map();
const stagePromptCache = new Map();

function clearLaunchAdjustments() {
  launchOverrides.clear();
  launchRoundOverrides.clear();
  launchPromptOverrides.clear();
}

function collectLaunchAdjustments(workflow) {
  const profileOverrides = {};
  for (const [profileId, selection] of launchOverrides) {
    if (workflow.profiles?.[profileId]) profileOverrides[profileId] = selection;
  }
  const roundOverrides = Object.fromEntries(launchRoundOverrides);
  const promptOverrides = Object.fromEntries(launchPromptOverrides);
  return {
    profile_overrides: Object.keys(profileOverrides).length ? profileOverrides : null,
    round_overrides: Object.keys(roundOverrides).length ? roundOverrides : null,
    prompt_overrides: Object.keys(promptOverrides).length ? promptOverrides : null,
  };
}

function showNewRunError(message) {
  const box = $("#new-run-error");
  box.hidden = !message;
  box.textContent = message || "";
}

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

function preflightActorLabel(workflow, stage) {
  const slotIndex = (workflow.session_slots || []).indexOf(stage.session_slot);
  const actor = slotIndex >= 0 ? String.fromCharCode(65 + slotIndex) : humanizeReason(stage.session_slot);
  return `${actor} · ${humanizeReason(stage.role)}`;
}

function preflightSessionPolicy(value) {
  return ({
    "new-if-missing": "new session if none",
    "continue": "continue session",
    "new": "new session",
  })[value] || humanizeReason(value);
}

function concreteTarget(target) {
  const value = String(target);
  if (value.startsWith("@seal:") || value.startsWith("@complete:")) return value.split(":").at(-1);
  return value.startsWith("@") ? null : value;
}

function graphStageGroups(stages) {
  // Strongly connected stages are real review/repair loops. This discovers
  // their membership without projecting the whole backend graph as a card wall.
  const edges = new Map(stages.map((stage) => [stage.id, []]));
  for (const stage of stages) {
    for (const target of Object.values(stage.transitions || {})) {
      const next = concreteTarget(target);
      if (next && edges.has(next) && next !== stage.id) edges.get(stage.id).push(next);
    }
  }
  const reachableFrom = (start) => {
    const seen = new Set([start]);
    const stack = [start];
    while (stack.length) {
      for (const next of edges.get(stack.pop()) || []) {
        if (!seen.has(next)) { seen.add(next); stack.push(next); }
      }
    }
    return seen;
  };
  const reach = new Map(stages.map((stage) => [stage.id, reachableFrom(stage.id)]));
  const assigned = new Set();
  const groups = [];
  for (const stage of stages) {
    if (assigned.has(stage.id)) continue;
    const members = stages.filter((other) => !assigned.has(other.id)
      && reach.get(stage.id).has(other.id) && reach.get(other.id).has(stage.id));
    members.forEach((member) => assigned.add(member.id));
    const roundStage = members.find((member) => member.round);
    groups.push({
      stages: members,
      loop: members.length > 1 && roundStage ? roundStage.round : null,
    });
  }
  return groups;
}

function reachableWorkflowStages(workflow) {
  const stages = workflow.stages || {};
  const ordered = [];
  const visited = new Set();
  const visit = (stageId) => {
    if (!stageId || visited.has(stageId) || !stages[stageId]) return;
    visited.add(stageId);
    const stage = stages[stageId];
    ordered.push(stage);
    for (const target of Object.values(stage.transitions || {})) visit(concreteTarget(target));
  };
  visit(workflow.start_stage);
  return ordered;
}

function semanticPreflightGroups(workflow) {
  // The revision stage is entered by the human gate's redirect choice, not by
  // a provider directive. Treat it as conditional even if a future snapshot
  // happens to expose an edge to it; otherwise the preview lies about a step
  // every run will execute. Some test workflows deliberately reuse the same
  // stage for proposal and revision, so only split distinct stage IDs.
  const branchOnlyIds = new Set();
  if (workflow.next_task_revision_stage && workflow.next_task_revision_stage !== workflow.next_task_stage) {
    branchOnlyIds.add(workflow.next_task_revision_stage);
  }
  for (const stageId of workflow.conditional_stages || []) branchOnlyIds.add(stageId);
  const main = reachableWorkflowStages(workflow).filter((stage) => !branchOnlyIds.has(stage.id));
  const connected = graphStageGroups(main);
  const planningLoop = connected.find((group) => group.loop?.counter === "planning")?.stages || [];
  const planningLoopIds = new Set(planningLoop.map((stage) => stage.id));
  const build = main.filter((stage) => String(stage.phase || "").startsWith("implementation"));
  const buildIds = new Set(build.map((stage) => stage.id));
  const closure = main.filter((stage) => String(stage.phase || "").startsWith("next-task") || stage.id === workflow.next_task_stage);
  const closureIds = new Set(closure.map((stage) => stage.id));
  const plan = main.filter((stage) => !planningLoopIds.has(stage.id) && !buildIds.has(stage.id) && !closureIds.has(stage.id));
  const mainIds = new Set(main.map((stage) => stage.id));
  const conditional = Object.values(workflow.stages || {}).filter((stage) => branchOnlyIds.has(stage.id) || !mainIds.has(stage.id));
  const groups = [];
  if (plan.length) groups.push({id: "plan", title: "Idea generation & planning", description: "Generate options, explain the rationale, and produce the first plan.", stages: plan});
  if (planningLoop.length) {
    const policy = planningLoop.find((stage) => stage.round)?.round;
    groups.push({id: "plan-review", title: "Plan review loop", description: "Challenge the plan, adjudicate findings, and seal the approved handoff.", stages: planningLoop, loop: policy});
  }
  if (build.length) {
    const policy = build.find((stage) => stage.round)?.round;
    groups.push({id: "build-audit", title: "Build / audit loop", description: "Implement the handoff, audit the evidence, and repair verified findings.", stages: build, loop: policy});
  }
  if (closure.length || conditional.length) {
    groups.push({id: "closure", title: "Closure", description: "Validate and accept the change, then decide whether another cycle should start.", stages: closure, conditional});
  }
  if (!groups.length && main.length) groups.push({id: "route", title: "Route", description: "Configured workflow actions.", stages: main, conditional});
  return groups;
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
  const groups = semanticPreflightGroups(workflow);
  const scheduled = groups.reduce((count, group) => count + group.stages.length, 0);
  const conditional = groups.reduce((count, group) => count + (group.conditional?.length || 0), 0);
  const verified = bootstrap?.catalog?.verified_at ? new Date(bootstrap.catalog.verified_at).toLocaleDateString() : "never";
  $("#route-preflight-summary").textContent = `${groups.length} phases · ${scheduled} scheduled actions${conditional ? ` · ${conditional} conditional` : ""} · catalog ${verified}${bootstrap?.catalog?.stale ? " (stale)" : ""}`;
  root.innerHTML = "";
  for (const group of groups) root.append(routeGroupRow(workflowId, workflow, group));
  renderRouteWarnings(workflow, Object.values(workflow.stages || {}));
}

function routeProfileRow(workflow, stage, groupStages) {
  const row = document.createElement("div");
  row.className = "route-profile-row";
  const profile = effectiveProfile(workflow, stage.profile);
  const profileStages = Object.values(workflow.stages || {}).filter((other) => other.profile === stage.profile);
  const visibleProfileStages = groupStages.filter((other) => other.profile === stage.profile);
  const actors = [...new Set(visibleProfileStages.map((item) => preflightActorLabel(workflow, item)))];
  const policies = [...new Set(visibleProfileStages.map((item) => preflightSessionPolicy(item.session_policy)))];
  const inCatalog = catalogModels(profile.provider).some((entry) => entry.selection_token === profile.model);
  const sharedWith = profileStages.filter((other) => other.id !== stage.id).map((other) => other.title);
  row.innerHTML = `<div><strong>${escapeHtml(actors.join(" / "))}</strong><span>${escapeHtml(profile.provider)} · ${escapeHtml(profile.model || "provider default")} · ${escapeHtml(profile.effort || "default")} · ${escapeHtml(profile.permission || "read-only")} · ${escapeHtml(policies.join(" / "))}</span></div><div class="route-profile-actions">${profile.overridden ? '<span class="text-status success">This run only</span>' : ""}${!inCatalog && catalogModels(profile.provider).length ? '<span class="text-status warning">Not in catalog</span>' : ""}<button type="button" class="quiet-button" data-profile-adjust>Change for this run</button></div><div class="route-step-editor" data-step-editor hidden></div>`;
  $("[data-profile-adjust]", row).addEventListener("click", () => toggleStageEditor(row, workflow, stage, sharedWith));
  return row;
}

function routeActionRow(workflowId, workflow, stage) {
  const row = document.createElement("li");
  row.className = "route-action-row";
  const edited = launchPromptOverrides.has(stage.prompt_file);
  row.innerHTML = `<div class="route-action-copy"><span>${escapeHtml(preflightActorLabel(workflow, stage))}</span><strong>${escapeHtml(stage.title)}</strong><p>Produces ${escapeHtml(String(stage.artifact_type || "").replaceAll("-", " "))}. ${escapeHtml(transitionLines(workflow, stage).join(" · "))}</p></div><div class="route-action-tools">${edited ? '<span class="text-status success">Edited for this run</span>' : ""}<button type="button" class="quiet-button" data-step-instruction>Edit instruction</button></div><div class="route-step-detail" data-step-detail hidden></div>`;
  $("[data-step-instruction]", row).addEventListener("click", () => toggleStageInstruction(row, workflowId, stage));
  return row;
}

function routeGroupRow(workflowId, workflow, group) {
  const row = document.createElement("li");
  row.className = `route-group route-group-${group.id}`;
  let loopControl = "";
  if (group.loop) {
    const counter = String(group.loop.counter);
    const baseCap = Number(group.loop.cap);
    const shownCap = launchRoundOverrides.has(counter) ? launchRoundOverrides.get(counter) : baseCap;
    const roundLabel = counter === "planning" ? "review rounds" : `${counter} rounds`;
    loopControl = `<label class="route-group-loop">Up to <input type="number" data-loop-cap required min="1" max="20" step="1" value="${shownCap}" aria-label="Maximum ${escapeHtml(counter)} rounds for this run"> ${escapeHtml(roundLabel)}, then pause <span class="text-status success" data-loop-override ${launchRoundOverrides.has(counter) ? "" : "hidden"}>This run only</span></label>`;
  }
  row.innerHTML = `<header class="route-group-head"><div><h4>${escapeHtml(group.title)}</h4><p>${escapeHtml(group.description)}</p></div>${loopControl}</header><div class="route-group-profiles"></div><ul class="route-action-list"></ul>${group.conditional?.length ? '<section class="route-conditional"><strong>If you redirect or request changes</strong><ul></ul></section>' : ""}`;
  if (group.loop) {
    const counter = String(group.loop.counter);
    const baseCap = Number(group.loop.cap);
    const capInput = $("[data-loop-cap]", row);
    const captureCap = (event) => {
      const raw = event.target.value;
      const cap = Number(raw);
      const valid = raw !== "" && Number.isInteger(cap) && cap >= 1 && cap <= 20;
      if (valid && cap === baseCap) launchRoundOverrides.delete(counter);
      else launchRoundOverrides.set(counter, valid ? cap : raw);
      $("[data-loop-override]", row).hidden = valid && cap === baseCap;
      return valid;
    };
    capInput.addEventListener("input", captureCap);
    capInput.addEventListener("change", (event) => {
      if (captureCap(event)) renderNewRunPreflight();
    });
  }
  const profilesRoot = $(".route-group-profiles", row);
  const represented = new Set();
  const groupStages = [...group.stages, ...(group.conditional || [])];
  for (const stage of group.stages) {
    if (!represented.has(stage.profile)) {
      represented.add(stage.profile);
      profilesRoot.append(routeProfileRow(workflow, stage, groupStages));
    }
    $(".route-action-list", row).append(routeActionRow(workflowId, workflow, stage));
  }
  if (group.conditional?.length) {
    const conditionalRoot = $(".route-conditional ul", row);
    for (const stage of group.conditional) {
      if (!represented.has(stage.profile)) {
        represented.add(stage.profile);
        profilesRoot.append(routeProfileRow(workflow, stage, groupStages));
      }
      conditionalRoot.append(routeActionRow(workflowId, workflow, stage));
    }
  }
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
    const overridden = launchPromptOverrides.has(stage.prompt_file);
    detail.innerHTML = `<p class="instruction-meta">${escapeHtml(value.prompt_file)} — exact static instruction · ${escapeHtml(contextLine)} · The full transport prompt adds the orchestration law (new sessions), the listed context artifacts, and the strict contract.</p><textarea data-instruction-text rows="13" spellcheck="false" aria-label="${escapeHtml(stage.title)} instruction"></textarea><div class="editor-actions"><button type="button" class="ghost-button" data-instruction-reset ${overridden ? "" : "hidden"}>Reset to saved instruction</button><button type="button" class="primary-button" data-instruction-apply>Apply to this run</button></div><p class="editor-note">Applies to this run only. Use “Save adjustments as new workflow” below to keep it.</p>`;
    const textarea = $("[data-instruction-text]", detail);
    textarea.value = overridden ? launchPromptOverrides.get(stage.prompt_file) : value.template;
    $("[data-instruction-apply]", detail).addEventListener("click", () => {
      const edited = textarea.value;
      if (!edited.trim()) { textarea.focus(); return; }
      if (edited === value.template) launchPromptOverrides.delete(stage.prompt_file);
      else launchPromptOverrides.set(stage.prompt_file, edited);
      renderNewRunPreflight();
    });
    $("[data-instruction-reset]", detail).addEventListener("click", () => {
      launchPromptOverrides.delete(stage.prompt_file);
      renderNewRunPreflight();
    });
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

function catalogModels(provider) {
  return (bootstrap?.catalog?.models || []).filter((model) => model.provider === provider);
}

function renderCatalogStatus() {
  const root = $("#catalog-status");
  if (!root) return;
  const catalog = bootstrap?.catalog || {};
  const refreshInfo = catalog.last_refresh;
  const checked = catalog.verified_at ? new Date(catalog.verified_at).toLocaleString() : "never";
  root.innerHTML = `<div class="catalog-meta"><span>${(catalog.models || []).length} selectable models</span><span>catalog checked ${escapeHtml(checked)}</span>${catalog.stale ? '<span class="warn">stale — refresh recommended</span>' : ""}${refreshInfo && !refreshInfo.succeeded ? `<span class="warn">last refresh failed ${escapeHtml(new Date(refreshInfo.at).toLocaleString())}</span>` : ""}<button type="button" class="quiet-button" id="catalog-refresh">Refresh from installed CLIs</button></div>` +
    [["codex", "Codex CLI"], ["claude", "Claude Code"]].map(([provider, providerLabel]) => {
      const source = catalog.sources?.[provider] || refreshInfo?.sources?.[provider] || {};
      const models = catalogModels(provider);
      const rows = models.map((model) => {
        const efforts = (model.supported_efforts || []).length ? model.supported_efforts.join(" · ") : "default only";
        const evidence = model.live_verified;
        const observedEfforts = evidence?.observed_efforts || evidence?.efforts || [];
        const acceptedEfforts = (evidence?.accepted_efforts || []).filter((value) => !observedEfforts.includes(value));
        const checkedAt = evidence?.at ? ` · ${new Date(evidence.at).toLocaleString()}` : "";
        const evidenceScope = evidence?.latest_run_observed ? "Latest matrix" : "Historical proof";
        const live = evidence
          ? `<span class="evidence-status model-observed" title="A live invocation returned the requested model${escapeHtml(checkedAt)}">${evidenceScope} · model observed${observedEfforts.length ? ` · effort observed: ${escapeHtml(observedEfforts.join("/"))}` : " · effort not observable"}${acceptedEfforts.length ? ` · CLI accepted: ${escapeHtml(acceptedEfforts.join("/"))}` : ""}${escapeHtml(checkedAt)}</span>`
          : '<span class="evidence-status not-tested">Not live-tested</span>';
        const latestAttempt = model.last_live_attempt;
        const latestFailure = latestAttempt && !latestAttempt.ok
          ? `<span class="evidence-status latest-failed">Latest matrix blocked · ${latestAttempt.error === "claude_api_error_429" ? "Claude session limit (429)" : escapeHtml(latestAttempt.error || "provider error")}${latestAttempt.at ? ` · ${escapeHtml(new Date(latestAttempt.at).toLocaleString())}` : ""}</span>`
          : "";
        return `<li><strong>${escapeHtml(model.display_name || model.selection_token)}</strong><code>${escapeHtml(model.selection_token)}</code><span>Advertised efforts: ${escapeHtml(efforts)}</span>${model.special_modes?.length ? `<em>${escapeHtml(model.special_modes.join(", "))}</em>` : ""}${live}${latestFailure}</li>`;
      }).join("");
      const listing = source.listing || (provider === "codex" ? "installed CLI discovery" : "curated official manifest + installed CLI capability check");
      const footnote = provider === "codex"
        ? `Catalog source: ${listing}. Availability reflects this installed account and CLI build; live evidence is labeled separately.`
        : `Catalog source: ${listing}. Full model IDs are used headlessly; model observation and effort acceptance/observation are labeled separately.`;
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

function installCatalogPicker(root, provider, model, effort, onSelectionChange = null) {
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
  let desiredEffort = effort || "";
  const sync = ({initial = false} = {}) => {
    // Capture the choice before rebuilding <option>s. Setting innerHTML makes
    // the browser select option zero, which previously downgraded xhigh/max to
    // the first advertised effort without the operator touching the control.
    const previousEffort = initial ? desiredEffort : effortSelect.value;
    const selected = entries.find((candidate) => candidate.selection_token === modelSelect.value);
    const custom = !selected;
    customBox.hidden = !custom;
    (effortSelect.closest("label") || effortSelect).hidden = custom;
    // The catalog provenance lives once in Settings → Models; per-card text
    // appears only for the deliberate custom escape hatch.
    if (custom) {
      detail.hidden = false;
      detail.textContent = "Not in the local catalog — runs as an unverified custom selection, recorded as evidence.";
      return;
    }
    // Models with no documented effort ladder (Haiku 4.5) expose only the
    // provider default; the adapter then omits the effort flag. Say so instead
    // of presenting a silently locked control.
    if (selected.supported_efforts?.length) {
      detail.hidden = true;
      detail.textContent = "";
    } else {
      detail.hidden = false;
      detail.textContent = `${selected.display_name || selected.selection_token} does not support the reasoning-effort parameter (provider effort docs), so requests always run at the provider default. Pick an effort-capable model to change effort.`;
    }
    const efforts = selected.supported_efforts?.length ? selected.supported_efforts : ["default"];
    effortSelect.innerHTML = efforts.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    const advertisedDefault = selected.default_effort;
    desiredEffort = efforts.includes(previousEffort)
      ? previousEffort
      : efforts.includes(advertisedDefault)
        ? advertisedDefault
        : (efforts[0] || "");
    effortSelect.value = desiredEffort;
  };
  modelSelect.onchange = () => { sync(); onSelectionChange?.(); };
  effortSelect.onchange = () => onSelectionChange?.();
  customModel.oninput = () => onSelectionChange?.();
  customEffort.oninput = () => onSelectionChange?.();
  sync({initial: true});
  return sync;
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
  settingsWorkflowId = workflows[settingsWorkflowId]
    ? settingsWorkflowId
    : (workflows["continuous-development"] ? "continuous-development" : Object.keys(workflows)[0]);
  const selector = $("#settings-workflow");
  selector.innerHTML = Object.entries(workflows).map(([id, workflow]) => `<option value="${escapeHtml(id)}">${escapeHtml(workflow.label)}</option>`).join("");
  selector.value = settingsWorkflowId;
  const workflow = workflows[settingsWorkflowId];
  const root = $("#profile-settings");
  root.innerHTML = "";
  // A compact table, one row per route profile; the only free text lives in
  // the explicit Custom… branch inside the cells.
  const table = document.createElement("table");
  table.className = "defaults-table";
  table.innerHTML = `<thead><tr><th>Used for</th><th>Provider</th><th>Model</th><th>Effort</th><th></th></tr></thead><tbody></tbody>`;
  const body = $("tbody", table);
  // List stages in execution order so planning reads before closure work.
  const orderedStages = workflow ? reachableWorkflowStages(workflow) : [];
  const orderedIds = new Set(orderedStages.map((stage) => stage.id));
  const remainingStages = Object.values(workflow?.stages || {}).filter((stage) => !orderedIds.has(stage.id));
  for (const [profileId, profile] of Object.entries(workflow?.profiles || {})) {
    const usedFor = [...orderedStages, ...remainingStages]
      .filter((stage) => stage.profile === (profile.id || profileId))
      .map((stage) => stage.title);
    const usedForList = (usedFor.length ? usedFor : [profile.label])
      .map((title) => `<strong>${escapeHtml(title)}</strong>`)
      .join("");
    const row = document.createElement("tr");
    row.innerHTML = `<td data-label="Used for"><div class="defaults-used-for">${usedForList}</div><span class="defaults-profile-id">${escapeHtml(profile.id || profileId)}</span></td><td data-label="Provider"><select data-field="provider" aria-label="${escapeHtml(profile.label)} provider">${["codex", "claude"].map((name) => `<option value="${name}"${name === profile.provider ? " selected" : ""}>${name}</option>`).join("")}</select></td><td data-label="Model"><select data-field="model" aria-label="${escapeHtml(profile.label)} model"></select><div data-catalog-custom hidden><input data-field="custom-model" autocomplete="off" placeholder="exact model ID"></div></td><td data-label="Effort"><select data-field="effort" aria-label="${escapeHtml(profile.label)} reasoning effort"></select><div data-catalog-custom-effort hidden><input data-field="custom-effort" autocomplete="off" placeholder="exact effort"></div><p class="catalog-detail" data-catalog-detail hidden></p></td><td data-label="Action"><button type="button" class="quiet-button" aria-label="Save ${escapeHtml(profile.label)} default">Save</button></td>`;
    // installCatalogPicker expects one custom container; bridge the split cells.
    const customEffortBox = $("[data-catalog-custom-effort]", row);
    const customBox = $("[data-catalog-custom]", row);
    const syncCustomVisibility = () => { customEffortBox.hidden = customBox.hidden; };
    installCatalogPicker(row, profile.provider, profile.model, profile.effort);
    new MutationObserver(syncCustomVisibility).observe(customBox, {attributes: true, attributeFilter: ["hidden"]});
    syncCustomVisibility();
    const providerSelect = $('[data-field="provider"]', row);
    providerSelect.addEventListener("change", () => {
      // Rebuild model/effort choices for the newly selected provider. Keep the
      // saved selection when returning to the profile's stored provider;
      // otherwise start from that provider's first catalog entry.
      const keepSaved = providerSelect.value === profile.provider;
      const fallback = catalogModels(providerSelect.value)[0]?.selection_token || "";
      installCatalogPicker(
        row,
        providerSelect.value,
        keepSaved ? profile.model : fallback,
        keepSaved ? profile.effort : "",
      );
      syncCustomVisibility();
    });
    $("button", row).addEventListener("click", async (event) => {
      const selection = catalogSelection(row);
      selection.provider = providerSelect.value;
      if (!selection.model || !selection.effort) { alert("Custom model and reasoning effort are required."); return; }
      event.target.disabled = true;
      try {
        await api("/api/profile", {method:"POST",body:JSON.stringify({workflow:workflow.id, profile:profile.id || profileId, ...selection})});
        // loadBootstrap re-renders this table, discarding the clicked button —
        // flag the row so the rebuilt one confirms the write visibly.
        defaultsSavedFlash = profile.id || profileId;
        await loadBootstrap();
      } catch(error) {
        alert(`Not saved: ${error.message}. Reload to discard this draft.`);
        event.target.disabled = false;
      }
    });
    if (defaultsSavedFlash === (profile.id || profileId)) {
      defaultsSavedFlash = null;
      const note = document.createElement("span");
      note.className = "defaults-saved-note";
      note.textContent = "Saved";
      $("td[data-label=Action]", row).append(note);
      setTimeout(() => { note.classList.add("fading"); setTimeout(() => note.remove(), 600); }, 3500);
    }
    body.append(row);
  }
  const tableWrap = document.createElement("div");
  tableWrap.className = "defaults-table-wrap";
  tableWrap.append(table);
  root.append(tableWrap);
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

async function createRun() {
  const request = $("#new-request").value.trim();
  if (!request) { $("#new-request").focus(); return; }
  const invalidControl = $("#new-run-preflight input:invalid");
  if (invalidControl) { invalidControl.reportValidity(); invalidControl.focus(); return; }
  const button = $("#create-run");
  button.disabled = true;
  showNewRunError("");
  try {
    const workflowId = $("#new-workflow").value;
    const workflow = bootstrap?.workflows?.[workflowId] || {};
    const adjustments = collectLaunchAdjustments(workflow);
    const result = await api("/api/runs", {method:"POST",body:JSON.stringify({project:$("#new-project").value,workflow:workflowId,run_mode:$("#new-run-mode").value,request,...adjustments})});
    $("#new-run-dialog").close();
    $("#new-request").value = "";
    clearLaunchAdjustments();
    await refreshRuns();
    await selectRun(result.run_id);
  } catch(error) { showNewRunError(`The run was not started. ${error.message}`); }
  finally { button.disabled = false; }
}

async function saveWorkflowAs() {
  const nameInput = $("#workflow-saveas-name");
  const label = nameInput.value.trim();
  if (!label) { nameInput.focus(); return; }
  const invalidControl = $("#new-run-preflight input:invalid");
  if (invalidControl) { invalidControl.reportValidity(); invalidControl.focus(); return; }
  const id = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
  const button = $("#workflow-saveas-confirm");
  if (!id) { showNewRunError("Workflow names need at least one letter or digit."); return; }
  button.disabled = true;
  showNewRunError("");
  try {
    const base = $("#new-workflow").value;
    const workflow = bootstrap?.workflows?.[base] || {};
    const saved = await api("/api/workflows/save-as", {method:"POST", body:JSON.stringify({
      base_workflow: base,
      id,
      label,
      ...collectLaunchAdjustments(workflow),
    })});
    clearLaunchAdjustments();
    stagePromptCache.clear();
    await loadBootstrap();
    $("#new-workflow").value = saved.id;
    renderNewRunPreflight();
    nameInput.value = "";
    $("#workflow-saveas-form").hidden = true;
    const toggle = $("#workflow-saveas-toggle");
    toggle.textContent = `Saved “${saved.label}” — it is now the selected workflow`;
    setTimeout(() => { toggle.textContent = "Save adjustments as new workflow…"; }, 4000);
  } catch (error) {
    showNewRunError(`Workflow not saved. ${error.message}`);
  } finally {
    button.disabled = false;
  }
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
  $("#new-workflow").addEventListener("change", () => { clearLaunchAdjustments(); showNewRunError(""); renderNewRunPreflight(); });
  $("#workflow-saveas-toggle").addEventListener("click", () => {
    const form = $("#workflow-saveas-form");
    form.hidden = !form.hidden;
    if (!form.hidden) $("#workflow-saveas-name").focus();
  });
  $("#workflow-saveas-confirm").addEventListener("click", saveWorkflowAs);
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
  $("#jump-active").addEventListener("click", () => ($("#active-node") || $("#active-gate"))?.scrollIntoView({behavior:"smooth",block:"start"}));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if ($("#inspector").getAttribute("aria-hidden") === "false") closeInspector();
    else if (document.body.classList.contains("mobile-rail-open")) closeRunRail();
  });
  applyTimelineDensity($("#zoom-slider").value);
  // Deep links so dialogs are directly addressable (and screenshotable).
  if (location.hash === "#new") openNew();
  if (location.hash.startsWith("#settings")) {
    $("#settings-dialog").showModal();
    const tab = location.hash.split("-")[1];
    if (tab) $(`[data-settings-tab="${tab}"]`)?.click();
  }
}

bindStaticEvents();
loadBootstrap().catch((error) => {
  $("#connection-label").textContent = error.message;
  console.error(error);
});
