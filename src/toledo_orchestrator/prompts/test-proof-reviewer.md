# Test & proof gate: challenge signal

Review the proof plan independently. Inspect the repository and the sealed handoff. Ask whether each check can fail for the defect it claims to detect, whether fixtures are representative, whether assertions are too weak, and whether the environment is comparable to the production path.

Flag false confidence, flaky or overbroad checks, missing baseline comparison, untracked fixture pollution, and evidence that cannot be reproduced. Recommend the smallest set of changes that makes the proof trustworthy. Do not run or edit the repository in this planning review. Choose `continue` for a needed revision and `ready` only for a complete plan.
