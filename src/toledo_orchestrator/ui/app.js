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
let railReturnFocus = null;
const promptPreviewCache = new Map();

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (options.method && options.method !== "GET") headers["X-Orchestrator-Nonce"] = bootstrap?.nonce || "";
  const response = await fetch(path, {...options, headers});
  const type = response.headers.get("content-type") || "";
  const value = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(value.error || value || `${response.status}`);
  return value;
}

function sessionColorClass(label) {
  let hash = 0;
  for (const char of String(label || "?")) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `session-color-${hash % 8}`;
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
    events: state.events,
    worker: state.worker,
    override: state.next_turn_override,
  });
}

async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    await refreshCurrent();
    pollCount += 1;
    if (pollCount % 3 === 0) await refreshRuns();
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
  const heading = state.worker?.active ? `${state.inflight?.title || stageTitle(state.current_stage)} is running…` : stageTitle(state.current_stage);
  const retryReasons = new Set(["operator_step", "provider_requested_human", "provider_invocation_failed", "provider_session_id_missing", "provider_session_missing", "provider_session_not_new", "provider_session_changed_unexpectedly", "missing_substantive_output", "malformed_directive", "unsupported_stage_directive", "invalid_next_turn_profile", "profile_permission_exceeds_stage", "background_operation_failed"]);
  const canOverride = state.schema_version === "toledo_orchestrator.run.v2" && state.current_stage && !state.inflight && (state.status === "created" || state.status === "running" || retryReasons.has(state.pending_human_decision));
  $("#run-header").innerHTML = `<div><p class="eyebrow">${escapeHtml(state.run_id)} · ${escapeHtml(state.status.toUpperCase())}</p><h2>${escapeHtml(heading || "Run complete")}</h2></div><div class="run-facts" id="run-facts"><span class="fact">cycle ${state.cycle || 1}</span><span class="fact">${state.current_turn || 0} turns</span><span class="fact">${escapeHtml(state.project)}</span><span class="fact">${escapeHtml((state.working_revision || state.source_revision || "").slice(0, 8))}</span>${state.execution_branch ? `<span class="fact">${escapeHtml(state.execution_branch)}</span>` : ''}${canOverride ? '<button class="quiet-button" id="next-turn-control">Override next turn ↗</button>' : ''}</div>`;
  $("#next-turn-control")?.addEventListener("click", openNextTurnControl);
  renderFilters(state);
  renderTimeline(state);
  if (state.worker?.error) showBanner(state.worker.error, "error");
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
    }
    if (cycle.approved_handoff && !handoffShown) block.append(milestone("Approved handoff sealed", cycle.approved_handoff, false));
    if (cycle.completion_receipt && !completionShown) block.append(milestone("Implementation accepted", cycle.completion_receipt, true));
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
}

function turnRow(turn) {
  const row = document.createElement("div");
  row.className = "timeline-row";
  row.dataset.phase = turn.phase;
  row.dataset.session = turn.session_label;
  const colorClass = sessionColorClass(turn.session_label);
  const preview = (turnPreview(turn) || "Open the stored artifact.").replace(/\s+/g, " ").slice(0, 280);
  row.innerHTML = `<button class="prompt-node" data-prompt-path="turns/${escapeHtml(turn.prompt_file)}" data-tooltip="Loading exact prompt…" aria-label="Open ${escapeHtml(turn.title)} prompt">${escapeHtml(promptShort(turn.prompt_kind))}</button><article class="turn-card ${escapeHtml(turn.provider)} ${colorClass}" tabindex="0" role="button" aria-label="Open ${escapeHtml(turn.title)} output"><div class="turn-card-head"><div class="actor"><span class="session-token">${escapeHtml(turn.session_label)}</span><div><h3>${escapeHtml(turn.title)}</h3><span class="route">${escapeHtml(turn.provider)} · ${escapeHtml(turn.role)}</span></div></div><span class="turn-number">${escapeHtml(turn.id)}</span></div><p class="turn-preview">${escapeHtml(preview)}</p><div class="chips"><span class="chip ${escapeHtml(turn.session_action)}">${escapeHtml(turn.session_action)} session</span><span class="chip">${escapeHtml(turn.profile_label || turn.profile)}</span><span class="chip">${escapeHtml(turn.permission)}</span><span class="chip">${Math.round((turn.elapsed_ms || 0)/1000)}s</span></div></article>`;
  const card = $(".turn-card", row);
  const prompt = $(".prompt-node", row);
  card.addEventListener("click", () => openTurn(turn, "output"));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openTurn(turn, "output");
    }
  });
  prompt.addEventListener("click", () => openTurn(turn, "prompt"));
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

async function hydratePromptPreviews() {
  for (const node of $$(".prompt-node[data-prompt-path]")) {
    try {
      const cacheKey = `${currentRunId}:${node.dataset.promptPath}`;
      let preview = promptPreviewCache.get(cacheKey);
      if (!preview) {
        const text = await artifactText(node.dataset.promptPath);
        preview = text.slice(0, 1000) + (text.length > 1000 ? "\n…" : "");
        promptPreviewCache.set(cacheKey, preview);
      }
      node.dataset.tooltip = preview;
    } catch { node.dataset.tooltip = "Prompt artifact unavailable"; }
  }
}

async function artifactText(path) {
  return api(`/api/runs/${encodeURIComponent(currentRunId)}/artifact?path=${encodeURIComponent(path)}`);
}

async function openTurn(turn, tab = "output") {
  const [prompt, output] = await Promise.all([
    artifactText(`turns/${turn.prompt_file}`),
    artifactText(`turns/${turn.output_file}`),
  ]);
  inspectorPayload = {prompt, output, metadata: JSON.stringify(turn, null, 2)};
  $("#inspector-kicker").textContent = `${turn.session_label} · ${turn.profile_label || turn.profile}`;
  $("#inspector-title").textContent = turn.title;
  const observed = turn.observed_model || turn.observed_reasoning
    ? `<span class="chip observed">observed ${escapeHtml(turn.observed_model || "model unknown")} · ${escapeHtml(turn.observed_reasoning || "effort unknown")}</span>`
    : `<span class="chip muted">observation unavailable${turn.observation_error ? ` · ${escapeHtml(turn.observation_error)}` : ""}</span>`;
  $("#inspector-meta").innerHTML = `<span class="chip ${escapeHtml(turn.session_action)}">${escapeHtml(turn.session_action)}</span><span class="chip">configured ${escapeHtml(turn.configured_model)} · ${escapeHtml(turn.configured_reasoning)}</span>${observed}<span class="chip">${escapeHtml(turn.permission)}</span>`;
  openInspector(tab);
}

async function openArtifact(title, path) {
  const content = await artifactText(path);
  inspectorPayload = {prompt:"", output:content, metadata:JSON.stringify({path}, null, 2)};
  $("#inspector-kicker").textContent = "SEALED ARTIFACT";
  $("#inspector-title").textContent = title;
  $("#inspector-meta").innerHTML = `<span class="chip">${escapeHtml(path)}</span>`;
  openInspector("output");
}

function openInspector(tab) {
  const inspector = $("#inspector");
  if (inspector.hidden) inspectorReturnFocus = document.activeElement;
  inspector.hidden = false;
  inspector.inert = false;
  document.querySelector(".app-shell").classList.add("inspector-open");
  inspector.setAttribute("aria-hidden", "false");
  selectInspectorTab(tab);
  $("#close-inspector").focus();
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
  $$('[data-choice]', gate).forEach((button) => button.addEventListener("click", async () => {
    const choice = button.dataset.choice;
    if (choice === "other" && !textarea.value.trim()) { textarea.focus(); return; }
    button.disabled = true;
    try {
      if (gate.dataset.reason === "operator_step") {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/continue`, {method:"POST", body:"{}"});
      } else {
        await api(`/api/runs/${encodeURIComponent(currentRunId)}/decision`, {method:"POST", body:JSON.stringify({choice, text:choice === "other" ? textarea.value : ""})});
      }
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
    operator_step: ["Ready for the next turn?", "Step mode paused before the next provider invocation. You can adjust the one-turn profile or session action above, then continue.", "Run next turn", "", "", ""],
    next_task_approval: ["Is this the right next task?", "The proposal is preserved exactly. Accept it, finish the loop, or redirect Claude session B.", "Yes — start planning", "No — finish here", "Other — revise proposal", "Tell session B what to change"],
    validation_execution_approval: ["Run the validation commands?", "These commands execute on the host against the isolated implementation worktree. Review the pending commands before approving.", "Yes — run validation", "No — cancel run", "Other — send to repair", "Explain what session C must change before validation"],
    validation_receipt_required: ["Validation receipt required", `Attach the patch-bound receipt from a terminal with: python -m toledo_orchestrator validate ${state.run_id} --receipt-file "C:\\path\\to\\receipt.json"`, "", "No — cancel run", "Other — add direction", "Add receipt or validation guidance"],
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
  if (reason === "validation_execution_approval") {
    const commands = (state.pending_validation?.commands || []).map((item) => `${item.id}: ${item.command}`);
    if (commands.length) description.textContent += `\n\nPending host commands:\n${commands.join("\n")}`;
  }
  gate.dataset.reason = reason;
  if (reason === "operator_step") {
    textarea.hidden = true;
    label.hidden = true;
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
}

function renderSettings() {
  const workflows = bootstrap.workflows;
  const workflow = workflows["continuous-development"] || Object.values(workflows)[0];
  const root = $("#profile-settings");
  root.innerHTML = "";
  for (const profile of Object.values(workflow.profiles)) {
    const card = document.createElement("article");
    card.className = "profile-editor";
    card.innerHTML = `<header><strong>${escapeHtml(profile.label)}</strong><span>${escapeHtml(profile.id)} · ${escapeHtml(profile.provider)}</span></header><div class="profile-grid"><input data-field="model" value="${escapeHtml(profile.model)}" aria-label="${escapeHtml(profile.label)} model"><input data-field="effort" value="${escapeHtml(profile.effort)}" aria-label="${escapeHtml(profile.label)} reasoning effort"><button type="button" class="quiet-button" aria-label="Save ${escapeHtml(profile.label)} profile">Save</button></div>`;
    $("button", card).addEventListener("click", async () => {
      const payload = {workflow:workflow.id, profile:profile.id, model:$('[data-field="model"]',card).value, effort:$('[data-field="effort"]',card).value};
      try { await api("/api/profile", {method:"POST",body:JSON.stringify(payload)}); await loadBootstrap(); }
      catch(error){ alert(error.message); }
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
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  dialog.innerHTML = `<form method="dialog"><div class="modal-head"><div><p class="eyebrow">ONE-TURN OVERRIDE</p><h2>${escapeHtml(stage.title)}</h2></div><button value="cancel" aria-label="Close override">×</button></div><label>Profile<select id="override-profile">${profiles.map((profile)=>`<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label)}</option>`).join("")}</select></label><label>Session action<select id="override-session"><option value="">Workflow default</option><option value="continue">Continue current session</option><option value="new">Start a new session</option></select></label><p class="override-note">The override is recorded in the run and applies only to the displayed next provider turn.</p><div class="modal-actions"><button value="cancel" class="ghost-button">Cancel</button><button type="button" class="primary-button" id="save-override">Apply override</button></div></form>`;
  document.body.append(dialog);
  $("#override-profile",dialog).value = currentState.next_turn_override?.profile || stage.profile;
  $("#override-session",dialog).value = currentState.next_turn_override?.session_action || "";
  $("#save-override",dialog).addEventListener("click", async () => {
    try {
      await api(`/api/runs/${encodeURIComponent(currentRunId)}/override`, {method:"POST",body:JSON.stringify({profile:$("#override-profile",dialog).value,session_action:$("#override-session",dialog).value || null})});
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
  $("#refresh-button").addEventListener("click", async () => { await loadBootstrap(); if(currentRunId) await refreshCurrent(); });
  $("#close-inspector").addEventListener("click", closeInspector);
  $$(".inspector-tabs button").forEach((button) => button.addEventListener("click", () => selectInspectorTab(button.dataset.tab)));
  $("#zoom-slider").addEventListener("input", (event) => document.documentElement.style.setProperty("--zoom", Number(event.target.value)/100));
  $("#phase-filter").addEventListener("change", () => currentState && renderTimeline(currentState));
  $("#session-filter").addEventListener("change", () => currentState && renderTimeline(currentState));
  $("#jump-active").addEventListener("click", () => ($("#active-node") || $("#active-gate"))?.scrollIntoView({behavior:"smooth",block:"center"}));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if ($("#inspector").getAttribute("aria-hidden") === "false") closeInspector();
    else if (document.body.classList.contains("mobile-rail-open")) closeRunRail();
  });
}

bindStaticEvents();
loadBootstrap().catch((error) => {
  $("#connection-label").textContent = error.message;
  console.error(error);
});
