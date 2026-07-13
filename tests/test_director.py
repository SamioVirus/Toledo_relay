from __future__ import annotations

from toledo_orchestrator.director import DIRECTION_CONDITIONS, DirectionContext, select_conditions


def test_first_visit_first_cycle_emits_nothing():
    # The static stance prompt already carries that language; the director stays silent.
    assert select_conditions(DirectionContext(stage_visits=0, cycle=1)) == ()


def test_revisiting_a_stage_selects_revisit():
    assert select_conditions(DirectionContext(stage_visits=1, cycle=1)) == ("revisit",)
    assert select_conditions(DirectionContext(stage_visits=3, cycle=1)) == ("revisit",)


def test_later_cycle_selects_continuity():
    assert select_conditions(DirectionContext(stage_visits=0, cycle=2)) == ("later_cycle",)


def test_revisit_in_a_later_cycle_selects_both_in_stable_order():
    assert select_conditions(DirectionContext(stage_visits=2, cycle=4)) == ("revisit", "later_cycle")


def test_selector_only_returns_known_conditions():
    for visits in range(4):
        for cycle in range(1, 4):
            for condition in select_conditions(DirectionContext(stage_visits=visits, cycle=cycle)):
                assert condition in DIRECTION_CONDITIONS


def test_selection_is_deterministic():
    context = DirectionContext(stage_visits=1, cycle=2)
    assert select_conditions(context) == select_conditions(context)
