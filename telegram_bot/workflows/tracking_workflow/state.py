from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

TRACKING_NEXT_ACTION_KEY = "tracking_next_action"
TRACKING_CANDIDATES_KEY = "tracking_candidates"
TRACKING_SELECTED_INDEX_KEY = "tracking_selected_candidate_index"
TRACKING_TARGET_KIND_KEY = "tracking_target_kind"

TRACKING_WORKFLOW_KEYS = (
    TRACKING_NEXT_ACTION_KEY,
    TRACKING_CANDIDATES_KEY,
    TRACKING_SELECTED_INDEX_KEY,
    TRACKING_TARGET_KIND_KEY,
)


def clear_tracking_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    for key in TRACKING_WORKFLOW_KEYS:
        user_data.pop(key, None)
