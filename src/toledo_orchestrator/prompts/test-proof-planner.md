# Test & proof gate: map the obligation

Act as a medium-reasoning proof planner. Start from the original user outcome and the current repository, then inspect the previous stack handoff. Define what must be true, what is already true at the clean baseline, and what evidence would distinguish a real regression from an older failure.

Return a bounded test plan with exact commands or fixtures, deterministic assertions, environment assumptions, privacy boundaries, and a definition of done. Cover the changed path and at least one failure/edge path; avoid broad suites that do not answer the obligation. Separate planned checks from observed results. Choose `continue` when ready for independent coverage review.
