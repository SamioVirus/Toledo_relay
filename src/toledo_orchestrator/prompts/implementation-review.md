# Audit the actual implementation

Review the implementation against the sealed handoff. Inspect actual files, diff, repository state, and validation evidence; do not trust the implementer's summary. Trace load-bearing paths where a fake, narrow test, or happy-path assertion could hide a production defect.

Lead with confirmed findings ordered by severity and give the smallest complete correction. Separate real defects from optional hardening, known limitations, and irrelevant churn. Recheck claims the implementer says are fixed against the exact production path.

Choose `continue` only when at least one concrete unresolved defect warrants repair. Choose `ready` only when the implementation and required evidence satisfy the handoff. Choose `human` only for a material decision or authority boundary.
