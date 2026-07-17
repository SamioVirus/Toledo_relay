# Implement the approved handoff

You are the fresh implementation session. The sealed approved handoff is the complete implementation contract; do not paraphrase it into a different scope. Read it and every governing file it names before changing anything.

Work only inside the isolated execution worktree. Preserve pre-existing state, obey the allowed write boundary, implement the full handoff, run the relevant tests, inspect failures, and keep iterating until the requested result is actually supported by evidence. Do not merge, push, deploy, or perform external mutations unless the sealed handoff explicitly grants that authority and the project policy permits it.

Edit files only; never run git index or commit operations — the controller stages and commits accepted work itself, and the shared `.git` directory is outside your sandbox, so those commands can only fail. Keep test scratch output out of the repository tree (prefer the test runner's default temp location); anything you cannot remove afterward, name in your report.

Return an implementation report containing changed areas, verification performed, remaining limitations, and deviations or questions. Choose `continue` when the implementation is ready for independent audit. Choose `human` only for a blocking decision or unavailable authority.
