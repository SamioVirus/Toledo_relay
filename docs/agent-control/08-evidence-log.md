# Evidence log

## 2026-07-13 — Phase 4 experiment 1: provider-switchable stages

- Hypothesis: an explicitly declared stage can safely change providers only by beginning a new physical session, while prior session artifacts remain immutable.
- Cost cap: no provider invocation was made for this experiment; deterministic fixture only. Rollback: omit `provider_switchable` (the default is false) or remove the one-turn override before it runs.
- Acceptance proof: a cross-provider override is rejected by default; the test opt-in permits it only with `session_action=new`, records `provider_switch=true`, and invokes the selected provider with a new session. Existing snapshots without the flag retain the historic provider lock.

## 2026-07-13 — Phase 3 steer and caption sidecars

- Deterministic proof: Steer is accepted only for a paused run with no in-flight call, continues the active provider session, records the exact note in a new sidecar artifact, links the replacement turn to the replaced turn, and does not call transition/round accounting. The latest turn remains the downstream artifact by existing chronological selection.
- Captions: `director.state_caption` is deterministic and free; optional `self_caption` is bounded to 280 characters, read only from the final directive fence, persisted as turn metadata, and removed with that fence from work-product transport.
- Live Codex continuity proof (low effort): session `019f5e67-b9bc-75b0-a265-8b542654abab` returned `CONTINUITY-ONE` then resumed to `CONTINUITY-TWO`. The manual CLI resume defaulted to Terra despite the initial Sol selection, so this is logged as a CLI invocation caution; the adapter explicitly passes `-m` on resume.
- Live Claude continuity proof (low effort): `claude-fable-5` session `7be09f0f-9e4f-4356-aa34-7eadbf10735e` returned both scoped responses with the same session ID. Recorded provider costs were $0.359001 then $0.064427.

## 2026-07-13 — Phase 2 catalog-backed controls

- Re-checked locally immediately before implementation: `codex debug models` reported `gpt-5.6-sol`, distinct supported reasoning levels, and the separate `fast` speed tier; `claude --help` exposes model/effort controls but no Ultracode capability. The UI shows special modes only when the cached local catalog records one.
- Profiles and the existing next-turn override modal now use catalog selects. The only text entry is the explicit `Custom…` branch; custom selections persist as `profile.custom` or in the one-turn override evidence. API proof rejects an unknown ordinary selection and accepts an explicitly custom one.
- The local UI Python subprocess cannot discover CLI capabilities on this host (`WinError 5`), so browser QA correctly displayed the explicit Custom warning rather than inventing ordinary choices. Desktop and 390×844 snapshots confirmed the picker remains reachable and mobile controls stay compact.
- Verification: `python -m pytest -q`, `python -m compileall -q src`, `node --check src/toledo_orchestrator/ui/app.js`, and `git diff --check` passed.

## 2026-07-13 — Phase 0 correctness and capability contracts

- The stale profile-save reproduction retained the page's pre-restart nonce and received `403` with `PermissionError: missing or invalid launch nonce`; the regression is `test_profile_save_with_browser_nonce_from_before_server_restart_is_rejected`.
- Local fact check: `codex-cli 0.144.1`; `codex debug models` reported account-aware JSON including `gpt-5.6-sol`, its supported reasoning levels, and a separate `additional_speed_tiers` field. The current host prevented Python subprocess discovery with `WinError 5`, so the cache records a stale warning and never blocks a run.
- Local fact check: `claude 2.1.185`; `claude --help` reports `--model` and efforts `low`, `medium`, `high`, `xhigh`, and `max`, but does not report Ultracode. Ultracode remains hidden.
- Official sources checked 2026-07-13: OpenAI's models documentation identifies `gpt-5.6-sol` and supported reasoning levels; Anthropic's CLI documentation specifies model/effort flags; Anthropic model documentation identifies curated API model IDs. The catalog labels curated Claude values as CLI-compatible, not subscription entitlement.
- Verification: `python -m pytest -q` (82 passed), `python -m compileall -q src`, `node --check src/toledo_orchestrator/ui/app.js`, and `git diff --check` passed.

## 2026-07-13 — Phase 1 control/evidence slice

- Added a compact sticky status strip with run state, stage, selected provider/model/effort, observed cumulative cost, and worker state.
- Added deterministic Markdown/plain-text export from authoritative run state and turn artifacts. It deliberately excludes raw envelopes; the same export is available through the UI API and `orchestrator export`.
- Validation approval now hides the direction field and identifies the isolated worktree/revision boundary separately from the next-turn controls.
- Browser QA: local workspace server inspected at desktop and 390×844. Mobile exposes the compact Runs/Settings/New controls and filters without tooltip overlays; desktop loaded the complete surface. No run fixture was available for a gate/preview visual capture.
