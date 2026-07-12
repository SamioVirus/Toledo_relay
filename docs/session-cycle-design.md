# Session-Aware Continuous Development Design

## Product center

The orchestrator is a session-aware marshal. It moves sealed artifacts between collaborators with different roles, injects short stance-changing prompts, preserves useful disagreement, and makes phase transitions and evidence visible. Provider session history is useful continuity; stored files remain the authority.

## Default A/B/C cycle

1. Planning session A starts fresh and develops the ideas, why, and buildable plan.
2. Review session B starts fresh and independently challenges the plan.
3. A continues, accepts valid findings, rejects bad feedback with evidence, and returns the complete revised plan.
4. B continues until it can honestly approve the handoff.
5. The controller seals A's latest plan byte-for-byte as the approved handoff.
6. Implementation session C starts fresh from that handoff in an isolated write-enabled worktree.
7. B continues the same logical and physical session under the implementation-audit profile and reviews actual code, diff, and validation evidence.
8. C continues for corrections; B re-verifies the exact production path.
9. After acceptance, the controller commits the isolated change to a durable execution branch and seals a completion receipt.
10. B switches back to its planning-review profile, preserves the same physical session, and proposes the next smallest high-leverage task.
11. A human chooses `yes`, `no`, or `other`. An accepted proposal starts a fresh D/E/F triplet (then G/H/I, and so on) while continuing on the accepted execution branch.

## Interstitial prompt vocabulary

The short prompts are first-class workflow actions rather than paraphrased task restatements:

- `Ideas`: begin with the ideas and why.
- `Skeptic`: distinguish real failure modes from reflexive fear.
- `Judge`: adjudicate every finding; fix, reject, or defer with evidence.
- `Build`: bind a fresh implementer to the sealed handoff without paraphrase drift.
- `Audit`: inspect actual code and evidence rather than trusting the summary.
- `Correct`: address concrete findings and add regression proof.
- `Next`: return to project-level context and choose the next valid build.
- `Redirect`: incorporate exact owner feedback and reopen the gate.

## Stable orchestration law

- Stay objective.
- Reason from first principles, the project goal, and authoritative evidence.
- Prefer pragmatic forward motion without ignoring real failure modes.
- Reward concrete disagreement; never manufacture objections to extend the loop.
- Separate legitimate safety from fear-based stagnation.
- Permit bounded, observable, reversible experiments; reject uncontained or irreversible risk.
- Inspect code, diffs, tests, and artifacts instead of trusting summaries.
- Push each collaborator to improve the preceding artifact rather than merely agree.
- Ask a human only when a material decision cannot be resolved from evidence or existing authority.
- Never call work complete beyond what the evidence proves.

## Independent controls

The engine keeps these concepts separate so alternate workflows can be added without rewriting the state machine:

- Workflow stage and route.
- Logical session slot and visible label.
- Physical provider session ID and generation.
- `new` versus `continue` session action.
- Provider/model/reasoning/permission profile.
- Project root, committed starting revision, write allowlist, and validations.
- Sealed artifact type.
- Human gate policy.

Model display names are not CLI contracts. Runtime configuration maps a friendly label to the exact provider model and effort arguments. A one-turn override can select another same-provider profile or intentionally start a new physical session. Missing continuation state pauses instead of falling back.

## Authority and durability

- All prompts and results are byte-accounted.
- Planning and review remain read-only.
- Implementation writes only inside a dedicated Git worktree.
- Changed paths are checked against the project allowlist.
- Exact patches, file hashes, and validation output are sealed before audit.
- Reviewer `ready` cannot override failed required local validation.
- Accepted work is committed to `codex/orchestrator/<run-id>`; source checkout and source branch stay unchanged.
- No automatic merge, push, deployment, or external mutation is implied.
- Unknown or mismatched provider-session continuation pauses for a human.
- A failed attempt to start a new provider session never supersedes the last healthy session.
- A stale in-flight marker after a crash is explicitly abandoned before a fresh evidence-led session starts.
- Per-run OS locks prevent two CLI/UI processes from invoking the same stage concurrently.
- Validation commands execute on the host only after explicit approval; remote proof is bound to the exact patch and base revision.
- Dirty source checkouts are rejected so uncommitted input cannot disappear from an isolated worktree.

## UI projection

The local UI is a projection of the run files:

- Codex left, Claude right, human gates centered.
- Stable color and label per logical session.
- Profile badges remain distinct when B switches models.
- Compact interstitial prompts sit on the center spine; hover previews and click inspection expose exact prompt bytes.
- Sealed handoff and completion artifacts are visible milestones.
- Overview/detail zoom and filters support long multi-cycle histories.
- Yes/no/other records exact human decisions.
- Repository and profile changes write runtime-local configuration, not package code.
- A repository is immutable within one run; switching repositories starts a separate run and evidence chain.
- Automatic mode runs to a decision; step mode pauses before every next provider turn so an operator can change the next profile or session action.
