# Correct verified findings

Address the review findings against the actual code and sealed handoff. Fix everything that is valid, add regression coverage that exercises the real failing path, and push back with evidence on incorrect or out-of-scope claims. Preserve unrelated user changes and the approved scope.

Run the affected checks using the same command and material environment as the sealed evidence whenever possible. If results disagree, preserve and explain both rather than replacing one silently. Report the corrected evidence state and every remaining host/runtime obligation. Return a complete correction report, not a promise. Choose `continue` when the correction is ready for re-verification. Choose `human` only when a blocking decision or unavailable authority remains.

If a finding demands a git index operation or anything else outside your sandbox (network, files beyond the workspace), do not attempt or retry it: record the exact host-side command required, mark the finding as needing controller authority, and continue with the corrections you can make. Asking a human to "continue" does not expand your authority.
