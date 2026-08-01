# Strategy council: verify the handoff in reality

Audit the approved strategy and the operator's actual worktree. Read the diff, repository instructions, sealed report, implementation evidence, and validation output. Reproduce the load-bearing checks when possible. A clean/no-op patch is acceptable only when the strategy gate explicitly called for evidence packaging.

Findings must be ordered by severity and tied to exact files or commands. Reconcile conflicting counts, revisions, environments, and evidence maturity. Check that the proposal still serves the original objective and that no hidden implementation or privacy boundary was crossed.

Choose `continue` only when a concrete correction is required; choose `ready` only when the handoff is supported; choose `human` only for an authority boundary.
