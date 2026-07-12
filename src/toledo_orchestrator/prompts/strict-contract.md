## Transport contract

Produce the substantive work product first. The work product must stand on its own and must not be replaced by process-control JSON.

Finish with exactly one final sentinel line in this form:

`ORCHESTRATOR_DIRECTIVE_V2: {"next":"continue"}`

Allowed `next` values are `continue`, `ready`, and `human`. Do not add fields. Do not include any other line beginning with `ORCHESTRATOR_DIRECTIVE_V2:`. Ordinary Markdown—including fenced examples about the orchestrator protocol—is substantive content and is preserved exactly. The controller ignores unsupported fields and pauses on missing or malformed control output.

Use `human` only when a concrete missing decision prevents responsible progress. The controller, not this response, owns route selection, context selection, project selection, permissions, retries, and file authority.
