# Audit the actual implementation

Review the implementation against the sealed handoff. Inspect actual files, diff, repository state, and validation evidence; do not trust the implementer's summary. Trace load-bearing paths where a fake, narrow test, or happy-path assertion could hide a production defect.

Lead with confirmed findings ordered by severity and give the smallest complete correction. Separate real defects from optional hardening, known limitations, and irrelevant churn. Recheck claims the implementer says are fixed against the exact production path.

Reconcile evidence before accepting: the report, sealed validation receipts, commands you reproduce, revision, and execution environment must describe one coherent result. If numbers differ, explain which run produced each result and whether the difference affects the changed system. Do not choose the nicer number.

Audit the consistency surfaces implied by the diff, not a generic maximal checklist. A new artifact family normally requires contract/schema, migration allowlist, inventory, privacy/retention classification, persistence, review/API exposure, runtime scheduling, failure isolation, and a test that follows the current source of truth. Prefer a mechanical invariant when drift is deterministic. State the highest proven evidence state: implemented, verified, operationally verified, or observed.

Choose `continue` only when at least one concrete unresolved defect warrants repair. Choose `ready` only when the implementation and required evidence satisfy the handoff. Choose `human` only for a material decision or authority boundary.
