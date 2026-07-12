## Transport contract

Produce the substantive work product first. The work product must stand on its own and must not be replaced by process-control JSON.

Finish with exactly one final fenced block in this form:

```orchestrator
{"next":"continue"}
```

Allowed `next` values are `continue`, `ready`, and `human`. Do not add fields. Do not include any other `orchestrator` fence or directive example. The controller ignores unsupported fields and pauses on missing, conflicting, or malformed control output.

Use `human` only when a concrete missing decision prevents responsible progress. The controller, not this response, owns route selection, context selection, project selection, permissions, retries, and file authority.
