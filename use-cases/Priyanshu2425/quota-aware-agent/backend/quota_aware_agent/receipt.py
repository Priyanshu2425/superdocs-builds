"""What a run actually spent, line by line, and whether that adds up.

From the research, an engineer describing what they did after a job died
halfway: *"I added it up from the responses I'd logged, and it didn't match the
number in the account, so I stopped looking."*

Two failures in one sentence. They had to do the arithmetic by hand, and when
the arithmetic disagreed with the platform there was nothing to tell them which
side was wrong — so the disagreement went uninvestigated. A receipt does the
adding up, and `reconcile` does the comparison and **names the disagreement
instead of hiding it**.

The one rule this module refuses to break: a number the platform did not state
is never presented as one it did. An operation charged is what a `usage` block
said; an operation *estimated* is what we believed. They are separate columns,
they are never summed together, and a receipt whose balances were not
authoritative says the reconciliation could not be done rather than doing it on
guesses.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .budget import Balance


@dataclass(frozen=True)
class Line:
    """One call, and what it cost."""

    step_id: str
    call: str
    billable: bool
    #: What the platform's `usage` block said it charged. None when no usage
    #: block came back, which is not the same as zero.
    ops_charged: int | None = None
    #: What we believed it would cost, before it happened.
    ops_estimated: int = 0
    balance_after: int | None = None
    balance_authoritative: bool = False
    #: Wall-clock seconds this call took, when it was measured. A quota-aware
    #: agent is asked what a run cost, and cost is two currencies: the docs warn
    #: that a single operation can take "thirty seconds to several minutes with
    #: no visible progress", so a receipt that priced only the money answered
    #: half the question and left the half the caller was waiting on.
    seconds: float | None = None
    note: str = ""

    @property
    def counted(self) -> int:
        """Only what the platform stated. Never the estimate."""
        return self.ops_charged or 0


@dataclass(frozen=True)
class Reconciliation:
    checkable: bool
    agrees: bool
    counted: int
    implied: int | None
    explanation: str


@dataclass
class Receipt:
    """The line items of one run, in the order they happened."""

    session_id: str = ""
    lines: list[Line] = field(default_factory=list)

    def record(self, step_id: str, call: str, *, billable: bool,
               ops_charged: int | None = None, ops_estimated: int = 0,
               balance: Balance | None = None, seconds: float | None = None,
               note: str = "") -> Line:
        line = Line(
            step_id=step_id, call=call, billable=billable,
            ops_charged=ops_charged, ops_estimated=ops_estimated,
            balance_after=balance.ops if balance else None,
            balance_authoritative=bool(balance and balance.authoritative),
            seconds=seconds,
            note=note,
        )
        self.lines.append(line)
        return line

    def confirm(self, step_id: str, call: str, *, ops_charged: int) -> "Line | None":
        """Replace an estimated line with what the platform later stated.

        The async edit call returns no usage block; the poll that completes the
        job returns one, and it is about the edit. Recording that as a second
        line would count the operation twice over -- once as a belief under
        "estimated, never confirmed" and once as a fact -- and a run that
        actually agreed would print a phantom estimate beside the agreement.
        So the belief is *replaced* by the statement, which is the only
        operation on a receipt that improves it: a number we guessed becomes a
        number we were told.

        Only the CHARGE is corrected. `balance_after` is deliberately left
        alone: the confirmation arrives on a later call, and writing its
        balance onto an earlier row makes the ledger read backwards -- live,
        the edit row showed the balance measured after the approve two rows
        below it (`edit 458?`, `poll 459?`, `approve 458?`). What we learned
        late is what the call *charged*; what the balance was at that instant
        is still what we believed at that instant.

        Returns None when there is nothing outstanding to confirm, and appends
        nothing. It used to append a line instead, which double-counted: a call
        whose usage arrives on BOTH the edit response and the job that completes
        it would have had one operation recorded twice, once per row, and the
        receipt would then have "disagreed" with an allowance that was right.
        Nothing is lost by staying quiet — the balance is updated by the guard
        either way, and the poll that carried the usage gets its own row.
        """
        for i in range(len(self.lines) - 1, -1, -1):
            l = self.lines[i]
            if l.step_id == step_id and l.call == call and l.ops_charged is None:
                self.lines[i] = Line(
                    step_id=l.step_id, call=l.call, billable=l.billable,
                    ops_charged=ops_charged, ops_estimated=l.ops_estimated,
                    balance_after=l.balance_after,
                    balance_authoritative=l.balance_authoritative,
                    seconds=l.seconds,
                    note="",
                )
                return self.lines[i]
        return None

    # -- what it adds up to ------------------------------------------------
    @property
    def counted(self) -> int:
        """Operations the platform said it charged."""
        return sum(l.counted for l in self.lines)

    @property
    def estimated_only(self) -> int:
        """Operations we believe were charged on calls that reported nothing.

        Kept apart from `counted` on purpose. Adding them would produce one
        number that is part measurement and part belief, and nobody downstream
        could tell which part.
        """
        return sum(l.ops_estimated for l in self.lines
                   if l.billable and l.ops_charged is None)

    @property
    def seconds(self) -> float:
        """Wall clock across every call that was measured."""
        return sum(l.seconds or 0.0 for l in self.lines)

    def slowest(self) -> Line | None:
        """The stage a caller was waiting on. Named, because an average hides
        exactly the call the docs warn can take minutes."""
        timed = [l for l in self.lines if l.seconds is not None]
        return max(timed, key=lambda l: l.seconds) if timed else None

    @property
    def billable_calls(self) -> int:
        return sum(1 for l in self.lines if l.billable)

    @property
    def unreported_billable_calls(self) -> int:
        return sum(1 for l in self.lines if l.billable and l.ops_charged is None)

    def reconcile(self, start: Balance | None, end: Balance | None) -> Reconciliation:
        """Does what the lines add up to match what the balance moved by?

        Only checkable when both ends of the run were *authoritative*. An
        estimate on either end makes the difference an estimate too, and a
        reconciliation between two guesses is theatre.
        """
        counted = self.counted
        if start is None or end is None:
            return Reconciliation(
                False, False, counted, None,
                "There is no starting or ending balance to compare against.")
        if not (start.authoritative and end.authoritative):
            which = []
            if not start.authoritative:
                which.append("the starting balance")
            if not end.authoritative:
                which.append("the ending balance")
            return Reconciliation(
                False, False, counted, None,
                f"{' and '.join(which).capitalize()} was inferred rather than "
                "stated by the platform, so this run cannot be reconciled. "
                f"{counted} operation(s) were confirmed charged"
                + (f", and {self.estimated_only} more were estimated on calls "
                   "that reported no usage." if self.estimated_only else "."),
            )

        implied = start.ops - end.ops
        if implied == counted:
            return Reconciliation(
                True, True, counted, implied,
                f"{counted} operation(s) charged, and the allowance moved by "
                f"{implied}. These agree.")
        gap = implied - counted
        return Reconciliation(
            True, False, counted, implied,
            f"The line items add up to {counted} operation(s), but the allowance "
            f"moved by {implied}. That is a difference of {gap}. It is reported "
            "rather than reconciled away: something was charged that this run "
            "did not record, or something else is spending against the same "
            "account.",
        )

    # -- handing it over ---------------------------------------------------
    def as_dict(self, start: Balance | None = None,
                end: Balance | None = None) -> dict:
        rec = self.reconcile(start, end)
        return {
            "session_id": self.session_id,
            "lines": [asdict(l) for l in self.lines],
            "billable_calls": self.billable_calls,
            "operations_charged": self.counted,
            "seconds": round(self.seconds, 3),
            "slowest_call": (
                {"step_id": self.slowest().step_id, "call": self.slowest().call,
                 "seconds": round(self.slowest().seconds, 3)}
                if self.slowest() is not None else None),
            "operations_estimated_only": self.estimated_only,
            "calls_that_reported_no_usage": self.unreported_billable_calls,
            "reconciliation": asdict(rec),
        }

    def as_json(self, start: Balance | None = None,
                end: Balance | None = None) -> str:
        return json.dumps(self.as_dict(start, end), indent=2)

    def render_text(self, start: Balance | None = None,
                    end: Balance | None = None) -> str:
        out = ["RUN RECEIPT" + (f" · {self.session_id}" if self.session_id else ""),
               "=" * 62]
        if start:
            out.append(f"Allowance at the start: {start}")
        out.append("")
        out.append(f"{'STEP':<18}{'CALL':<12}{'CHARGED':>9}{'LEFT':>9}{'TOOK':>9}")
        out.append("-" * 62)
        for l in self.lines:
            charged = "—" if l.ops_charged is None else str(l.ops_charged)
            if l.ops_charged is None and l.billable:
                charged = f"~{l.ops_estimated}"
            left = "—" if l.balance_after is None else (
                str(l.balance_after) + ("" if l.balance_authoritative else "?"))
            took = "—" if l.seconds is None else f"{l.seconds:.1f}s"
            out.append(f"{l.step_id[:17]:<18}{l.call[:11]:<12}"
                       f"{charged:>9}{left:>9}{took:>9}")
            if l.note:
                out.append(f"  {l.note}")
        out.append("-" * 62)
        out.append(f"{'confirmed charged':<30}{self.counted:>9}")
        if self.estimated_only:
            out.append(f"{'estimated, never confirmed':<30}{self.estimated_only:>9}")
        # A tenth of a second is the floor for saying anything about time. Below
        # it there is no story to tell -- the offline demo answers from a fake
        # in microseconds, and "longest single call: export, 0.0s" is noise
        # dressed as a finding.
        if self.seconds >= 0.1:
            out.append(f"{'wall clock':<30}{self.seconds:>8.1f}s")
            slowest = self.slowest()
            if slowest is not None and slowest.seconds >= 0.1:
                where = f" on '{slowest.step_id}'" if slowest.step_id else ""
                out.append(
                    f"  longest single call: {slowest.call}{where}, "
                    f"{slowest.seconds:.1f}s — the docs warn one operation can "
                    "take minutes, so this is the number to look at rather than "
                    "the average.")
        if end:
            out.append(f"Allowance at the end: {end}")
        out.append("")
        rec = self.reconcile(start, end)
        out.append("DOES IT ADD UP?")
        out.append("-" * 62)
        out.append(rec.explanation)
        out.append("")
        out.append("A '~' is an operation we believe was charged on a call that "
                   "returned no usage")
        out.append("block. A '?' is a balance we inferred rather than one the "
                   "platform stated.")
        return "\n".join(out)
