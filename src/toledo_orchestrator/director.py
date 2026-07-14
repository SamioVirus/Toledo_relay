"""Situational orchestrator direction — the "lubrication" the owner supplies by
hand between handoffs (e.g. "how about now?" after a repair, or "seal it, stop
inventing objections" on a second review pass).

This module is deliberately a *selector*, not an author. It looks at generic run
facts — how many times this stage has already run in the current cycle, and
whether this is a later cycle — and returns which situational *conditions* apply.
It reads no stage IDs, counter names, or prose. The language for each condition,
and the mapping from a stage to the fragment it uses, live in prompt assets and
workflow configuration (``StageDefinition.direction``), so renamed or custom
stages stay correct and prompts remain runtime-editable.

It is a pure function of run facts (no model call, no I/O) and only chooses words
placed in front of the next provider; it never changes control flow.
"""
from __future__ import annotations

from dataclasses import dataclass

# The situational conditions the controller can detect generically. A workflow
# stage opts in by mapping any of these keys to a prompt fragment in its
# ``direction`` table; unmapped conditions simply emit nothing.
DIRECTION_CONDITIONS = ("revisit", "later_cycle")


@dataclass(frozen=True)
class DirectionContext:
    """The minimal, stage-agnostic run facts the selector reasons over."""

    # Substantive, non-correction turns this exact stage has already produced in
    # the current cycle (0 on its first run, >=1 when the stance is repeating).
    stage_visits: int = 0
    cycle: int = 1


def select_conditions(context: DirectionContext) -> tuple[str, ...]:
    """Return the applicable situational conditions in a stable order.

    Nothing is returned on a stage's first run in the first cycle: the static
    stance prompt already carries that language, so the director stays silent
    until it has something genuinely situational to add.
    """
    conditions: list[str] = []
    if context.stage_visits >= 1:
        conditions.append("revisit")
    if context.cycle > 1:
        conditions.append("later_cycle")
    return tuple(conditions)


def state_caption(state: dict[str, object]) -> str:
    """Free, deterministic compact state summary for the operator surface."""
    stage = str(state.get("current_stage") or "complete")
    cycle = int(state.get("cycle") or 1)
    turns = int(state.get("current_turn") or 0)
    status = str(state.get("status") or "unknown")
    return f"{status} · {stage} · cycle {cycle} · {turns} turns"
