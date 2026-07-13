# Evidence log

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
