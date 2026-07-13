# Operator Control UX and Capability Catalog Plan

Status: proposed implementation plan

Date: 2026-07-13

Related design: [Session-Aware Continuous Development Design](session-cycle-design.md)

## Outcome

Make the orchestrator's operator surface feel like a clear, inspectable relay:

- saved defaults are truthful and durable;
- every next turn is previewed before it runs;
- model, effort, session, and provider controls only expose supported choices;
- human approvals state exactly what they authorize;
- an operator can export a complete, local record and steer a paused session without losing the evidence chain.

This is additive. Existing run snapshots, sealed artifacts, host-command approval, provider-session continuity, and declarative workflow ownership remain intact.

## Product decisions

### Separate the four independent controls

The UI must not conflate these concepts:

| Control | Meaning | Initial policy |
| --- | --- | --- |
| Provider | CLI route: `OpenAI / Codex` or `Anthropic / Claude` | Display always; lock while continuing a session. |
| Model | Exact provider selection token | Catalog-backed picker plus a deliberate `Custom...` escape hatch. |
| Reasoning effort | Per-model level such as Low through Max | Show only catalog-supported values. |
| Multi-agent/speed mode | A distinct capability, for example Codex Ultra or Claude Ultracode | Separate rail; hide unless the installed CLI reports support. |

Use the supplied picker as interaction inspiration: provider chips, model rows, a horizontal effort rail, a separate vertical multi-agent rail, and a compact live preview. Do not copy its `Pro` label: use the provider's actual `Max` effort where applicable.

Stance is a fifth, separate thing. `Ideas`, `Skeptic`, `Judge`, and `Audit` are workflow prompt contracts, not models or profile labels. The first inline pencil changes execution settings only. Curated stance overrides are a later, explicit workflow feature.

### Capability catalog: deterministic first

Do not make a general AI researcher part of the launch path. The catalog is a deterministic runtime artifact with source metadata, discovery time, verification time, local CLI version, availability status, selection token, display name, aliases, supported efforts, and supported special modes.

Source precedence:

1. Codex: local `codex debug models` JSON, which is account-aware and includes per-model supported reasoning levels.
2. Claude: installed CLI capabilities, curated official model manifest, and models observed in successful Toledo turns.
3. Optional Anthropic Models API only when an API route is configured; label its result as API availability, not Claude Code subscription entitlement.
4. `Custom...` selection with a visible warning and evidence capture.

Refresh must be read-only, atomic, and retain the last known good catalog. A stale catalog is a warning, never a run blocker. A control is never shown solely because it appears in documentation: the installed CLI must report the capability or the catalog must mark it available for the selected route.

Codex Ultra and Claude Ultracode are not merely another reasoning-effort dot. They are special execution modes and must be rendered and validated separately. The installed Claude Code version must be capability-gated; documentation-only features remain hidden until the local binary supports them.

### Truthful persistence and profiles

Existing profile persistence is runtime-local by design. A successful change applies to new runs; an existing run retains its snapshotted workflow for reproducibility.

The observed failure is consistent with a stale browser nonce after the UI server restarts: `app.js` sends the nonce obtained at page load, while each server launch generates a new nonce. The exact browser request/response must still be captured before declaring this the user's incident root cause.

The plan fixes the user-visible problem regardless:

- On a write 403 caused by an invalid nonce, fetch bootstrap state, retry the identical request once, then surface a precise failure if it still fails.
- On every successful profile save, re-read the server's canonical profile and render that result.
- On failure, retain the unsaved draft only as an explicitly unsaved form, with a visible error and a reload action; never imply persistence.
- Validate provider, model, effort, and permission as a combination rather than accepting arbitrary nonempty model and effort text.
- Derive the UI label from the persisted profile fields or make any custom label clearly secondary, so labels cannot silently lie about model or effort.
- Show `Saved - applies to new runs` and, at a paused run, separately offer a one-turn override. Do not silently mutate a pinned run.

The feedback claim that the JSON-content-type check is misspelled is rejected: the current source checks `application/json` correctly. No task should be created for that alleged defect.

### One pause, two distinct cards

The current next-turn override plumbing is already working. The redesign is a projection over it, not a new controller path.

At an operator pause, render a `Next turn` preview dock:

```text
Next turn:  Audit / B.2
OpenAI / Codex | Terra | High | Continue | read-only
Context: approved handoff + validation receipt
[ direction for this turn                                  ]
                                      [ Go ]
```

Fields are inline-editable only where the current stage/session contract allows them. Provider switching is deferred: current controller rules correctly reject a one-turn profile that changes the stage provider. A future provider switch requires an explicit `provider_switchable` workflow declaration, a new physical session, and a separate evidence contract.

Host-command validation is a different card, never mixed with next-turn direction:

```text
Validation approval
You are approving: run `python -m pytest -q` once in this isolated worktree.
Why: Audit requires the project's configured unit-test proof.
Where: <worktree path> at <base revision + patch identity>
[ Run once ] [ Send back ] [ Cancel ]
```

The command boundary stays human-approved. The improvement is clarity, not removal of the boundary.

### Export and local privacy

Provide deterministic local export as Markdown by default and plain text on request. The chronological export contains run identity, selected project/revision, pinned profiles, prompts, directive-free outputs, owner directions, decisions, observed model evidence, and validation summaries.

Because prompts and outputs can be sensitive, the export UI states that it writes local evidence. Raw stdout/stderr envelopes remain excluded by default; their inclusion is an explicit future privileged-export decision.

### Cost visibility

Every decision to spend on a turn should show what it costs. Runs already record `total_cost_usd` per Claude turn and plan-metered usage for Codex; surface that in the next-turn preview, the sticky status strip, and the export, so an operator sees the price of the pinned profile before pressing Go and the cumulative spend after. A promotional tier (for example a time-limited model inclusion) that lapses shows up first in the recorded per-turn cost, so treat that number as the earliest signal rather than assuming a profile still bills as it did when it was chosen.

### Steer a paused session

`Steer` is the label for a manual follow-up to the latest output of a logical session. It is available only while the run is paused and no next provider invocation has begun.

A steer:

- continues the same provider and physical session;
- asks for a complete replacement artifact that incorporates the operator's direction;
- preserves the current stage and returns to its next-turn preview;
- records the operator's exact note and the replacement output as linked evidence;
- inserts a `STEER` timeline node whose hover/inspector reveals the note;
- makes the replacement the latest artifact for downstream context; and
- does not increment workflow round caps or stage visit counters.

After another provider turn begins, changing prior evidence requires a future fork/rewind design; it is not an implicit consequence of Steer.

### TL;DR: free structure, optional semantic judgment

The director remains deterministic and makes no model call. It can always produce a concise state caption from existing facts, for example stage, logical session, round, decision, elapsed time, and selected configuration. This is free and always on.

Semantic commentary such as `the review found two contradictions` needs model judgment. Make it an optional, clearly labeled `self-caption` emitted as a bounded sidecar field by the existing provider turn. It must:

- be absent without penalty if the provider does not produce it;
- be parsed through the existing directive-fence mechanism;
- never enter later transport prompts or influence transitions;
- be labeled as the producing model's self-report, not an independent audit; and
- be capped by length and enabled per run/profile.

Historical semantic backfill is a separately opt-in, budgeted experiment using the cheapest catalog-supported model at low effort. No model name is hardcoded into the design.

## Delivery sequence

### Phase 0 - Correctness and contracts

1. Capture the profile-save request, response, browser state, server start time, and nonce state; add a regression test for the confirmed failure.
2. Add server revision/start-time visibility and nonce-refresh/retry behavior.
3. Make profile saves server-confirmed, profile labels truthful, and errors actionable.
4. Introduce versioned catalog contracts, local Codex discovery, curated Claude manifest, observed-model evidence, validation, cache, and staleness rules.
5. Add contract/API tests for invalid provider-model-effort combinations, custom selection, and restart persistence.

Exit criterion: edit a profile, restart the UI server, reload the browser, and observe the exact persisted values. Existing runs retain their original snapshot.

### Phase 1 - Clear controls and evidence access

1. Add the compact sticky run-status strip beneath the global top bar; it shows run status, current stage, session, provider/model/effort, observed spend, and worker state without pinning the oversized header.
2. Replace generic gate copy with separate next-turn and validation-approval cards.
3. Redesign the existing next-turn override modal into the preview dock while retaining its existing API contract.
4. Add Markdown/text export from the authoritative run files and CLI parity.
5. Fix prompt-tooltip/inspector interactions defensively: suppress hover popovers once the inspector is open, remove tooltip pointer capture, and define mobile behavior.

Exit criterion: desktop and 390px mobile QA show no sticky overlap, prompt overlay, ambiguous gate, or loss of the currently supported override controls.

### Phase 2 - Catalog-backed picker everywhere

1. Use the picker in Profiles, the next-turn dock, and launch preflight.
2. Filter model and effort choices by provider and capability.
3. Render special modes only when verified locally.
4. Show source, verification date, availability, and stale/custom warnings in compact detail, not as distracting main UI.
5. Keep a fallback text field only behind `Custom...` and record it as custom evidence.

Exit criterion: no ordinary picker path can submit an unsupported combination; custom selection remains possible and plainly marked.

### Phase 3 - Steer and caption sidecars

1. Add the paused-session Steer contract, artifacts, timeline node, and replacement-artifact rules.
2. Add deterministic state captions.
3. Add optional self-caption parsing and redaction/transport isolation tests.

Exit criterion: a Steer resumes the same session, creates a full evidence trail, keeps round accounting correct, and feeds only the replacement artifact to the next stage. Captions never appear in transport prompts.

### Phase 4 - Deliberate experiments

These are separate proposals, not hidden extensions of the earlier phases:

- provider-switchable stages with a forced new physical session;
- curated, evidence-tested stance overrides;
- catalog-research automation beyond local discovery and curated manifests;
- semantic-caption backfill;
- fork/rewind after a later provider invocation.

Each experiment needs a hypothesis, cost cap, rollback path, and separate acceptance proof.

## Verification matrix

| Area | Required proof |
| --- | --- |
| Profile persistence | API success/error tests; browser save/restart/reload proof; stale-nonce regression. |
| Catalog | Parser fixtures; cache fallback; staleness; supported/unsupported combinations; custom selection. |
| Overrides | Current snapshot remains unchanged; stage provider lock remains enforced; permitted one-turn override reaches the existing controller contract. |
| Gates | Exact approval statement, command/path/revision binding, and no direction field in validation approval. |
| UI | JavaScript/static checks plus desktop and 390px browser QA for sticky offsets, picker filtering, tooltip/inspector behavior, and keyboard focus. |
| Export | Stable chronological output; no raw stdout/stderr by default; export file reflects authoritative artifacts. |
| Steer | Same physical session, no in-flight override, replacement artifact linkage, round-count invariants, and transport isolation. |
| Captions | Deterministic caption correctness; optional self-caption absence/failure is harmless; no sidecar text reaches a later provider prompt. |
| Cost | Per-turn `total_cost_usd`/plan usage surfaced in preview, status strip, and export; a lapsed promotional tier is visible from the recorded cost. |

No paid provider call is needed for Phases 0-2 beyond a bounded local capability query. Before closing Phase 3, run one scoped live continuity proof for each affected provider only after deterministic tests pass.

## Evidence basis and current-source notes

- Local installed Codex CLI exposes `codex debug models` with raw JSON and supported reasoning-level metadata.
- Local installed Claude Code has no equivalent account-aware model-list command; its capabilities must remain locally version-gated.
- OpenAI model selection is documented at <https://developers.openai.com/api/docs/models>.
- Anthropic model availability and the Models API are documented at <https://platform.claude.com/docs/en/about-claude/models/overview> and <https://platform.claude.com/docs/en/api/models/list>.
- Anthropic effort and Claude Code CLI behavior are documented at <https://platform.claude.com/docs/en/build-with-claude/effort> and <https://code.claude.com/docs/en/cli-usage>.

Recheck these sources and the locally installed CLI versions immediately before implementing catalog logic; provider capability information is expected to change.
