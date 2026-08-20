"""BudgetGuard -- never start work you cannot finish.

SHARED CORE. This module is used unchanged by "Attest", the document-analysis
system built for Problem 01 of the same round, where it guards the publisher.
It is vendored here rather than imported so this build stands alone in the
builds repository. Reuse is only a shortcut when it is hidden, so it is stated
here, in the README, and in the write-up.

Grounded in the SuperDocs documentation rather than in the task brief
(TASK.md rule 1). Two facts from the docs shape this module:

  * Most requests bill one operation; very large ones bill one per 25 sections
    edited. So pricing is done in sections and reported in operations.
  * The usage endpoints reject API keys -- `/v1/users/me/usage` and `/limits`
    accept web-app session tokens only. From an API-key context the balance is
    read off the `usage` block that rides on every chat response. You therefore
    learn your balance as a side effect of doing work, not in advance of it.

That second point is why `Balance` carries `authoritative`. Between calls the
number is an estimate and says so. `quota_exhausted` on a response is the
authoritative stop signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

SECTIONS_PER_OP = 25


@dataclass(frozen=True)
class Balance:
    ops: int
    authoritative: bool
    as_of: str = ""

    def __str__(self) -> str:
        qualifier = "confirmed" if self.authoritative else "estimated"
        unit = "operation" if self.ops == 1 else "operations"
        return f"{self.ops} {unit} ({qualifier})"


@dataclass(frozen=True)
class Change:
    row_id: str
    sections: int
    severity: str = "medium"


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Plan:
    publish: list[Change] = field(default_factory=list)
    defer: list[Change] = field(default_factory=list)
    rationale: str = ""

    @property
    def ops_to_publish(self) -> int:
        return estimate(self.publish)

    @property
    def complete(self) -> bool:
        return not self.defer


def estimate(changes: list[Change]) -> int:
    """Operations a set of changes will bill.

    Zero changes cost nothing. Anything else costs at least one operation,
    then one more per 25 sections -- which is the documented accounting, not
    a division.
    """
    sections = sum(c.sections for c in changes)
    if sections <= 0:
        return 0
    return max(1, math.ceil(sections / SECTIONS_PER_OP))


class BudgetGuard:
    def __init__(self, seed: Balance | None = None) -> None:
        self._balance = seed or Balance(ops=0, authoritative=False)
        self._exhausted = False

    def seed_from_whoami(self, remaining_ops: int, as_of: str = "") -> Balance:
        """The agent whoami call does accept an agent key, so this is the one
        moment the balance is genuinely authoritative before work begins."""
        self._balance = Balance(remaining_ops, authoritative=True, as_of=as_of)
        return self._balance

    def assume_spent(self, ops: int) -> Balance:
        """No usage block came back on a call we believe was billable.

        The docs say usage rides on every chat response; the async endpoints
        empirically return none (verified 2026-08-19). So the balance is
        decremented by our own estimate and, crucially, **stops being
        authoritative** -- continuing to print "confirmed" over a number the
        platform never confirmed is the exact bluff `authoritative` exists to
        prevent.
        """
        self._balance = Balance(
            max(0, self._balance.ops - max(0, ops)), authoritative=False,
            as_of=self._balance.as_of,
        )
        return self._balance

    def reconcile(self, ops_charged: int, monthly_remaining: int | None, quota_exhausted: bool,
                  as_of: str = "") -> Balance:
        """Called with the `usage` block from every response."""
        self._exhausted = quota_exhausted
        if monthly_remaining is not None:
            self._balance = Balance(monthly_remaining, authoritative=True, as_of=as_of)
        else:
            self._balance = Balance(
                max(0, self._balance.ops - ops_charged), authoritative=False, as_of=as_of
            )
        return self._balance

    def remaining(self) -> Balance:
        return self._balance

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def fit(self, changes: list[Change], remaining: int | None = None) -> Plan:
        """Pure given a budget number. Publishes what fits, highest severity
        first, defers the rest, and says so in a sentence a person can read."""
        budget = self._balance.ops if remaining is None else remaining
        needed = estimate(changes)

        if self._exhausted or budget <= 0:
            # These are different situations and must not be reported as one.
            # "Exhausted" is the platform's own signal; a zero budget can also
            # mean a reserve is being held back so finished work stays usable.
            reason = (
                "the allowance is exhausted"
                if self._exhausted
                else "there are no operations available to spend"
            )
            return Plan(
                publish=[],
                defer=list(changes),
                rationale=(
                    f"Started nothing: {reason}. "
                    f"{len(changes)} change(s) are queued and named in the run report."
                ),
            )

        if needed <= budget:
            return Plan(publish=list(changes), defer=[], rationale="")

        ordered = sorted(
            changes, key=lambda c: (_SEVERITY_ORDER.get(c.severity, 99), -c.sections)
        )
        publish: list[Change] = []
        for change in ordered:
            if estimate(publish + [change]) <= budget:
                publish.append(change)
        deferred = [c for c in changes if c not in publish]

        pub_sections = sum(c.sections for c in publish)
        all_sections = sum(c.sections for c in changes)
        rationale = (
            f"Sized to fit: {pub_sections} of {all_sections} changed sections "
            f"({estimate(publish)} of {needed} operations). Deferred "
            f"{len(deferred)} lower-severity change(s) to stay inside the "
            "remaining allowance."
        )
        return Plan(publish=publish, defer=deferred, rationale=rationale)
