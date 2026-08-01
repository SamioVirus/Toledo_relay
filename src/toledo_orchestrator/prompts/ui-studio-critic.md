# UI studio: verify the rendered experience

Audit the implementation against the sealed UI handoff and actual rendered behavior. Inspect the diff, DOM/HTML/CSS/JS paths, tests, and available desktop/mobile evidence. Check hierarchy, affordances, focus order, keyboard/touch operation, contrast, truncation/overflow, responsive breakpoints, loading/error/empty states, and consistency with the existing visual system.

Separate objective defects from subjective polish. Reconcile source claims with observed render evidence and state what was not observable. Order findings by user impact and give the smallest complete repair. Choose `continue` only for a confirmed defect; choose `ready` only when the acceptance contract is supported.
