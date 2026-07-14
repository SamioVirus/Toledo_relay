"""Small, curated prompt-contract additions for explicit one-turn experiments."""
from __future__ import annotations

CURATED_STANCES = {
    "ideas": "Generate several concrete options, name their tradeoffs, and avoid treating a preference as proof.",
    "skeptic": "Challenge the strongest claim with concrete repository evidence; report only actionable objections.",
    "judge": "Compare the available evidence, state the deciding criterion, and make the smallest defensible recommendation.",
    "audit": "Trace the claimed behavior to observable evidence and distinguish verified facts from assumptions.",
}
