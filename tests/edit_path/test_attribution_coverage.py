# SPDX-FileCopyrightText: 2026 Edit Path contributors
# SPDX-License-Identifier: GPL-3.0-only

import pytest

from edit_path.errors import GateError
from edit_path.pipeline import attribution_coverage, validate_effect_keyframe_ids
from edit_path.state import operation_name


def _effect(value: str = "10") -> dict:
    return {
        "effect_id": "effect-1",
        "name": "transform",
        "parameters": [{"parameter_id": "parameter-1", "name": "rotation", "value": f"0=0;10={value}"}],
        "keyframes": [
            {
                "keyframe_id": "keyframe-1",
                "parameter_id": "parameter-1",
                "parameter": "rotation",
                "frame": "10",
                "value": value,
            }
        ],
    }


def _diff(sequence: int, before: dict, after: dict, *, interaction_id: str | None) -> dict:
    event = {
        "event_type": "state.diff",
        "sequence": sequence,
        "transaction_id": f"tx-{sequence}",
        "boundary": "commit",
        "diff": {
            "changes": [
                {
                    "entity": "clip",
                    "native_id": 7,
                    "change": "updated",
                    "before": before,
                    "after": after,
                }
            ]
        },
    }
    if interaction_id:
        event["interaction_id"] = interaction_id
        event["intent"] = {
            "kind": "keyframe.value.change",
            "interaction_id": interaction_id,
            "ambiguous": False,
            "interaction_scope": "property_editor",
        }
    else:
        event["intent"] = {
            "kind": "keyframe.value.change",
            "interaction_id": "synthetic",
            "ambiguous": True,
            "attribution": "synthetic_state_correlation",
        }
    return event


def test_structured_keyframe_ids_drive_canonical_operation() -> None:
    before = {"effects": [_effect("10")]}
    after = {"effects": [_effect("20")]}
    assert operation_name({"changes": [{"entity": "clip", "change": "updated", "before": before, "after": after}]}) == "keyframe.value.change"


def test_effect_classifier_covers_track_and_master_stacks() -> None:
    before = {"effects": []}
    after = {"effects": [_effect()]}
    for entity in ("track", "master_effect"):
        assert operation_name({"changes": [{"entity": entity, "change": "updated", "before": before, "after": after}]}) == "effect.add"


def test_coverage_separates_mapped_and_ambiguous_effect_diffs() -> None:
    mapped = _diff(3, {"effects": [_effect("10")]}, {"effects": [_effect("20")]}, interaction_id="interaction-1")
    ambiguous = _diff(4, {"effects": [_effect("20")]}, {"effects": [_effect("30")]}, interaction_id=None)
    action_reports = [
        {
            "sequence": 3,
            "transaction_id": "tx-3",
            "linked": True,
            "compatible": True,
            "attribution": "transaction",
        },
    ]
    events = [
        {"event_type": "ui.gesture", "interaction_id": "interaction-1", "interaction_scope": "property_editor"},
        {"event_type": "ui.command", "interaction_id": "interaction-1", "command_id": "set-property", "command_registered": True},
    ]
    report = attribution_coverage(events, [mapped, ambiguous], action_reports)
    assert report["state_diffs"]["effect_or_keyframe"] == 2
    assert report["state_diffs"]["mapped"] == 1
    assert report["state_diffs"]["ambiguous"] == 1
    assert report["state_diffs"]["mapped_percent"] == 50.0
    assert report["property_editor"]["interaction_ids"] == 1
    assert report["stable_effects"]["effect_ids"] == 1
    assert report["stable_effects"]["parameter_ids"] == 1
    assert report["stable_effects"]["keyframe_ids"] == 1


def test_stable_effect_id_gate_is_opt_in_for_legacy_sessions() -> None:
    events = [{"event_type": "state.checkpoint", "sequence": 1, "snapshot": {"clips": [{"effects": [{"id": "legacy"}]}]}}]
    counts = validate_effect_keyframe_ids(events, required=False)
    assert counts["missing_effect_ids"] == 1
    with pytest.raises(GateError):
        validate_effect_keyframe_ids(events, required=True)
