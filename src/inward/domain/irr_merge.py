"""Overlap-safe merge of an IRR file into the accumulated IRR history (SoT 8.2).

Each (Type, TRX Number, TRX Date) group is compared as a multiset of rows:
identical -> skip; new superset (later rows of the same day) -> add only the extra rows;
subset -> skip; anything else -> REVIEW, not applied.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Hashable, Mapping


class GroupAction(str, Enum):
    NEW = "NEW"
    IDENTICAL = "IDENTICAL"
    EXTENDED = "EXTENDED"
    SUBSET = "SUBSET"
    REVIEW = "REVIEW"


@dataclass
class MergePlan:
    actions: dict[Hashable, GroupAction] = field(default_factory=dict)
    rows_to_add: list = field(default_factory=list)

    def count(self, action: GroupAction) -> int:
        return sum(1 for a in self.actions.values() if a is action)


def plan_merge(existing: Mapping[Hashable, Counter], incoming: Mapping[Hashable, Counter]) -> MergePlan:
    plan = MergePlan()
    for key, new_rows in incoming.items():
        old_rows = existing.get(key)
        if old_rows is None:
            plan.actions[key] = GroupAction.NEW
            plan.rows_to_add.extend(new_rows.elements())
        elif new_rows == old_rows:
            plan.actions[key] = GroupAction.IDENTICAL
        elif not (old_rows - new_rows):  # new contains every old row (with multiplicity)
            plan.actions[key] = GroupAction.EXTENDED
            plan.rows_to_add.extend((new_rows - old_rows).elements())
        elif not (new_rows - old_rows):
            plan.actions[key] = GroupAction.SUBSET
        else:
            plan.actions[key] = GroupAction.REVIEW
    return plan
