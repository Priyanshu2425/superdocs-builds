"""An agent that checks its allowance before planning, sizes the work to fit,
and degrades in plain language instead of failing halfway.

The shape of the problem, from the docs rather than from optimism:

  You cannot read your balance on demand. `/v1/users/me/usage` and `/limits`
  reject `sk_` keys with a 401. The agent-key path is `GET /v1/agents/whoami`
  once at the start, and after that the `usage` block that rides on every chat
  response. So the balance is authoritative at the start and after each call,
  and an *estimate* in between. `Balance.authoritative` carries that
  distinction so a report can never present a stale number as a live read.

The consequence worth naming: this agent does not "check remaining allowance"
continuously. It reads it, plans against it, and re-reads it as a side effect
of every call it makes. That is the strongest guarantee the platform allows,
and the report says which number it used.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from .budget import Balance, BudgetGuard, Change, Plan, estimate
from .client import (QuotaExhausted, RationExhausted, SuperDocsClient,
                     provably_never_sent)
from .idempotency import OperationLedger, State, operation_key
from .policy import Policy, StopReason, WhenItDoesNotFit
from .receipt import Receipt


@contextmanager
def _elapsed():
    """Wall clock around one call, readable after it returns.

    `perf_counter` rather than `time()` because this measures a duration, and a
    clock that can be stepped backwards by NTP would report a negative one.
    """
    marks = [time.perf_counter(), None]
    try:
        yield lambda: (marks[1] or time.perf_counter()) - marks[0]
    finally:
        marks[1] = time.perf_counter()


@dataclass
class Step:
    """One unit of requested work: an edit instruction plus how many document
    sections it is expected to touch. Sections are what SuperDocs bills on --
    one operation per 25 sections edited -- so the plan is priced in sections
    and reported in operations."""
    step_id: str
    instruction: str
    sections: int
    severity: str = "medium"

    def as_change(self) -> Change:
        return Change(row_id=self.step_id, sections=self.sections, severity=self.severity)


@dataclass
class Report:
    """What the agent did, in the words it would say to a person."""
    balance_at_start: Balance | None = None
    balance_at_end: Balance | None = None
    planned: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    #: Steps an earlier run already paid for. Not repeated, and not billed.
    already_applied: list[str] = field(default_factory=list)
    #: Steps an earlier run started and whose outcome nobody ever learned.
    #: Neither repeated nor forgotten -- a person has to look at the document.
    needs_a_person: list[str] = field(default_factory=list)
    #: Steps that were asked, were billed, and changed nothing: the job reached
    #: `completed` and the document's version never moved. NOT in `completed`,
    #: because saying a declined instruction was carried out is the lie this
    #: field exists to stop telling.
    no_effect: list[str] = field(default_factory=list)
    #: What the platform said about a step that did not change anything, in its
    #: own words, keyed by step id. Quoted rather than paraphrased.
    platform_said: dict = field(default_factory=dict)
    #: Steps whose job ended `failed` or `cancelled` on the platform's side.
    failed: list[str] = field(default_factory=list)
    #: The operation key each step was run under, so a handler that has to read
    #: the ledger does not have to rebuild the key from arguments it lost.
    keys_by_step: dict = field(default_factory=dict)
    ops_spent: int = 0
    stopped_because: str = ""
    stop_reason: StopReason = StopReason.COMPLETED
    export_warnings: list = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    receipt: Receipt = field(default_factory=Receipt)

    def say(self, line: str) -> None:
        self.lines.append(line)

    def stop(self, reason: StopReason, line: str = "") -> None:
        """Stopping is a rule firing, not a number running out. Recording which
        rule is what lets somebody argue with it instead of raising it."""
        self.stop_reason = reason
        self.stopped_because = reason.explain()
        if line:
            self.say(line)

    def reconciliation(self) -> str:
        """Whether the line items agree with the allowance. Named, not hidden."""
        return self.receipt.reconcile(self.balance_at_start,
                                      self.balance_at_end).explanation

    def plain_language(self) -> str:
        out = list(self.lines)
        if self.already_applied:
            out.append(
                f"Already done by an earlier run, and not repeated: "
                f"{', '.join(self.already_applied)}. You were not charged again."
            )
        if self.needs_a_person:
            out.append(
                f"Started and never confirmed: "
                f"{', '.join(self.needs_a_person)}. These were not retried, "
                "because retrying might be charged twice and might apply the "
                "same edit twice. Open the document and check them."
            )
        if self.no_effect:
            said = "; ".join(
                f"'{sid}' {self.platform_said[sid]}"
                for sid in self.no_effect if self.platform_said.get(sid))
            out.append(
                f"Asked, billed, and the document did not change: "
                f"{', '.join(self.no_effect)}. These are NOT done."
                + (f" SuperDocs said: {said}" if said else "")
                + " Reword the instruction and run it again — the same wording "
                "is not retried, because it has already been paid for once and "
                "declined once."
            )
        if self.failed:
            out.append(
                f"Failed on the platform's side: {', '.join(self.failed)}. "
                "Check the run report for what it said, and the document for "
                "what it left."
            )
        if self.deferred:
            out.append(
                f"Left undone: {', '.join(self.deferred)}. "
                "These were not started, so nothing is half-applied."
            )
        if self.balance_at_end:
            out.append(f"Allowance now: {self.balance_at_end}.")
        return "\n".join(out)


class QuotaAwareAgent:
    """Reads the allowance, plans to fit it, and stops on the platform's signal.

    The stopping rule, stated once so it is not scattered:
      1. `quota_exhausted` from any response -- the platform's own signal, and
         the only authoritative one. Stop immediately.
      2. The reserve floor -- never spend the last `reserve` operations, so
         there is always enough left to export the work already done.
      3. `max_steps` -- the small-sample bound. Loops need a stopping rule.
    """

    def __init__(self, client: SuperDocsClient, guard: BudgetGuard | None = None,
                 policy: Policy | None = None,
                 ledger: OperationLedger | None = None) -> None:
        self._c = client
        self._g = guard or BudgetGuard()
        self._p = policy or Policy()
        # In memory unless a caller hands over a path. A ledger that forgets
        # when the process dies is no use for the failure it exists for, so the
        # CLI and the MCP server both give it a file.
        self._ledger = ledger or OperationLedger()
        # The document version the run started from. A step that finishes with
        # the version it began with changed nothing, whatever it said. Set by
        # `run` off the upload and moved by every step that really applies.
        self._version: str | None = None

    @property
    def policy(self) -> Policy:
        return self._p

    @property
    def uses_a_relay(self) -> bool:
        """Whether the allowance is somebody else's ration rather than a read
        of an account of ours. It decides which question `read_allowance` asks,
        so anything describing that number has to be able to ask it too."""
        return bool(getattr(self._c, "using_relay", False))

    def unresolved_operations(self) -> list[dict]:
        """Calls a previous run started and never learned the outcome of.

        Surfaced, never cleaned up. Deciding between "pay again" and "leave the
        work undone" is somebody's money and somebody's document, so it is
        reported and a person resolves it with `ledger.resolve`.
        """
        return [{"key": r.key, "step_id": r.step_id, "session_id": r.session_id}
                for r in self._ledger.unresolved()]

    def budget_hint(self) -> dict:
        """The allowance, in a shape a calling agent can plan against.

        An underused technique in the cost-governance literature and an obvious
        one once stated: an agent that knows what is left can decide earlier,
        summarise instead of re-reading, or escalate before it runs out. This
        rides back on every tool result rather than needing its own call --
        which matters here, because a call to find out is not free everywhere
        and asking is itself a decision an agent should not have to make.
        """
        balance = self._g.remaining()
        ration = self._g.ration
        hint = {
            "remaining_operations": balance.ops,
            "authoritative": balance.authoritative,
            "as_of": balance.as_of,
            "reserve": self._p.reserve,
            "spendable_on_new_edits": self._p.spendable(balance.ops),
            "exhausted": self._g.exhausted,
            "policy": self._p.describe(),
            # Written from the path actually taken, not from the one this
            # build used to have. A note that says "authoritative at whoami"
            # beside `authoritative: false`, on a path where whoami is never
            # called, tells a reading agent the opposite of the field beside
            # it -- and the field is the one that is right.
            "note": (
                ("This is the relay's published daily ration, not a reading: "
                 "whoami is not called on this path because it answers for the "
                 "relay's account rather than yours. It is decremented by our "
                 "own count of charged calls and corrected whenever a response "
                 "carries a usage block, so it is an estimate throughout. "
                 "Set SUPERDOCS_API_KEY to read a real balance instead."
                 if self._c.using_relay else
                 "Authoritative at whoami and after every response that carried "
                 "a usage block; inferred in between, and it says which.")
                + " Exports are free, which is why the reserve costs you nothing."
            ),
        }
        if self._g.exhausted:
            # Which ceiling stopped us, because the two have different remedies.
            hint["exhausted_because"] = self._g.exhausted_because
        if ration is not None:
            # Only present when a second ceiling is actually in play. An agent
            # planning against `remaining_operations` needs to know that number
            # may be the ration rather than the account, or it will read a small
            # number as "buy more allowance" when the answer is "use your own
            # key, or come back after 00:00 UTC".
            hint["daily_ration"] = {
                "remaining_calls": ration.ops,
                "resets_at": ration.as_of,
                "source": ration.limited_by,
                # Which ceiling produced the number above, by what it says
                # about itself rather than by object identity: on the relay
                # path the ration IS the seeded balance now, so the two are
                # equal without being the same object.
                "binding": bool(balance.limited_by)
                           and balance.limited_by == ration.limited_by,
                "note": (
                    "Counted in charged calls, not in SuperDocs operations, and "
                    "never confirmed by the lender — it is an estimate until a "
                    "refusal proves otherwise. Set SUPERDOCS_API_KEY to remove "
                    "this ceiling entirely."
                ),
            }
        return hint

    def settled(self, session_id: str, steps: list[Step]) -> tuple[
            list[Step], list[str], list[str]]:
        """Split requested work by what an earlier run already did with it.

        Returns (still to do, already applied, started and never confirmed,
        asked before and changed nothing).

        This exists so a plan and a run cannot disagree. Pricing work that an
        earlier run already paid for quotes a number nobody will be charged,
        and an agent deciding what it can afford against that number is being
        told the wrong thing by the tool whose entire job is telling it the
        right thing.
        """
        to_do: list[Step] = []
        applied: list[str] = []
        unconfirmed: list[str] = []
        no_effect: list[str] = []
        for step in steps:
            record = self._ledger.get(operation_key(
                session_id, step.step_id, step.instruction, step.sections))
            if record.state is State.APPLIED:
                applied.append(step.step_id)
            elif record.state is State.IN_FLIGHT:
                unconfirmed.append(step.step_id)
            elif record.state is State.NO_EFFECT:
                # Not to_do: this exact wording has already been billed and
                # already been declined. Rewording changes the key, so the
                # retry that stands a chance is not blocked by this.
                no_effect.append(step.step_id)
            else:
                to_do.append(step)
        return to_do, applied, unconfirmed, no_effect

    # -- planning ---------------------------------------------------------
    def read_allowance(self) -> Balance:
        """How much may be spent, established before anything is planned.

        **Which question gets asked depends on whose key it is.**

        With a key of your own (`SUPERDOCS_API_KEY`), the account is yours, so
        it is read: `GET /v1/agents/whoami` is free and is the one moment the
        balance is genuinely authoritative before work begins.

        On the relay path there is no account of ours to ask about, so whoami
        is **not called at all** and the ration from `.env` is the allowance.
        Asking would have produced a number that is wrong twice over: it
        describes the relay's account rather than the caller's, and it is
        static -- live on 2026-08-27 it answered `used: 0, remaining: 500`
        after four operations had gone out that day, because the charges came
        from a promo bucket the monthly figure does not count. The ration is
        the ceiling actually in force, it is the one the caller can set, and
        `RELAY_DAILY_OPS` in `.env` is where they set it.

        What that costs, stated rather than hidden: whoami also carries
        `quota_exhausted`, so on the relay path an account whose monthly
        allowance is genuinely gone is discovered on the first chat call
        instead of before the run starts. That call is refused rather than
        billed, the refusal is authoritative, and the run stops on it with a
        report and an export -- so it is later news, not lost news.
        """
        # A second ceiling, if the transport is lending us somebody else's key
        # under a daily ration. Opened first either way, because "how much may
        # I spend?" has two answers on that path and both have to be true.
        ration = getattr(self._c, "daily_ration", None)
        if ration is not None:
            self._g.open_ration(ration)
        if ration is not None and getattr(self._c, "using_relay", False):
            return self._g.seed_from_ration(ration)
        r = self._c.whoami()
        if r.usage.get("quota_exhausted") or getattr(self._c, "quota_exhausted", False):
            # whoami is free and is not refused by exhaustion, but the signal it
            # carries is the authoritative one and has to reach the planner.
            self._g.mark_exhausted()
        remaining, resets_at = self._balance_from_whoami(r.body)
        if remaining is None:
            # Never invent a balance. An unknown allowance is planned as zero,
            # which degrades to doing nothing and saying why -- loudly, because
            # the alternative failure is the dangerous one: a build that reads
            # nothing, plans against nothing, does nothing, and looks like a
            # pass. It is not silent here; `fit` says there are no operations
            # available to spend and the run reports having started nothing.
            return self._g.seed_from_whoami(0)
        return self._g.seed_from_whoami(int(remaining), as_of=resets_at)

    @staticmethod
    def _balance_from_whoami(body: dict) -> tuple[int | None, str]:
        """The remaining allowance, whichever shape whoami answered in.

        Three shapes are real and the field name is not stable across them:

          * `quota.remaining` -- what both the origin and the relay actually
            return, verified live 2026-08-27. This is the one that matters.
          * `remaining_operations` -- the flat form named in the API docs.
          * `usage.monthly_remaining` -- the field that rides on chat responses,
            in case whoami ever answers in the same vocabulary as everything
            else.

        Read as a list rather than as one field because the cost of guessing
        wrong is not an error. It is `None`, which becomes a balance of zero,
        which becomes a plan that fits nothing and a run that does nothing and
        exits 0 -- an agent that quietly did not work looks exactly like an
        agent with an empty allowance. The relay's key is not an agent account
        (`is_agent_account: false`), so the shape it answers in is somebody
        else's decision, not ours.
        """
        quota = body.get("quota") or {}
        usage = body.get("usage") or {}
        for value, resets_at in (
            (quota.get("remaining"), quota.get("resets_at")),
            (body.get("remaining_operations"), body.get("resets_at")),
            (usage.get("monthly_remaining"), usage.get("resets_at")),
        ):
            if value is not None:
                return int(value), str(resets_at or "")
        return None, ""

    #: Each step is its own `POST /v1/chat/async`, so each one bills at least
    #: one operation. Pooling their sections and dividing by 25 -- which is
    #: right for a publisher that sends one request -- under-prices an agent
    #: that sends several, and an under-priced plan is an agent starting work
    #: it cannot finish. Named here so the planner and `_run_step` cannot drift
    #: apart about what a step costs.
    BATCHED = False

    def plan(self, steps: list[Step]) -> Plan:
        """Price the work and fit it to what is left, under the policy."""
        budget = self._p.spendable(self._g.remaining().ops)
        steps, _bit = self._p.bound(steps)
        plan = self._g.fit([s.as_change() for s in steps], remaining=budget,
                           batched=self.BATCHED)
        if (plan.publish and plan.defer
                and self._p.when_it_does_not_fit is WhenItDoesNotFit.REFUSE):
            # A caller who would rather have nothing than a subset said so.
            return Plan(publish=[], defer=plan.publish + plan.defer,
                        rationale=StopReason.REFUSED_PARTIAL.explain(),
                        batched=self.BATCHED)
        return plan

    # -- execution --------------------------------------------------------
    def run(self, session_id: str, filename: str, content: bytes, steps: list[Step],
            export_format: str = "docx") -> Report:
        """Read the allowance, size the work, do what fits, and say what it did.

        The order is the load-bearing part and it is why this reads as four
        steps rather than one block: the allowance is read *before* anything is
        planned, the plan is priced *before* anything is uploaded, and the export
        runs even when the run stopped early — because it is free, which is what
        the reserve exists to guarantee.
        """
        report = Report(receipt=Receipt(session_id=session_id))

        start = self._read_and_report_allowance(report)
        steps = self._apply_sample_bound(steps, report)
        # Priced only over work nobody has paid for yet. What an earlier run
        # settled is named in the report, not quoted as a cost.
        steps = self._set_aside_what_earlier_runs_settled(session_id, steps, report)
        if not steps and (report.already_applied or report.needs_a_person
                          or report.no_effect):
            # Nothing left to do because an earlier run did it, not because it
            # would not fit. Reporting that as "nothing fits" would send a
            # caller off to buy allowance it does not need.
            return self._nothing_left_to_do(session_id, export_format, start, report)
        plan = self._price(steps, report)
        if not plan.publish:
            return self._did_not_start(plan, report)

        # Free per the docs, so it is not priced -- and it happens only after
        # everything that could refuse has refused.
        with _elapsed() as took:
            uploaded = self._c.upload(session_id, filename, content)
        self._version = self._version_of(uploaded)
        # Free, and on the receipt anyway: "what did it cost" is asked in two
        # currencies, and a large document's upload is time the caller waited.
        report.receipt.record("", "upload", billable=False, ops_charged=0,
                              balance=self._g.remaining(), seconds=took())
        report.say(f"Uploaded {filename}.")

        self._work(session_id, plan, {s.step_id: s for s in steps}, report)
        if (report.stop_reason is StopReason.COMPLETED
                and report.no_effect and not report.completed):
            # Nothing stopped the run and nothing came of it. Leaving this as
            # COMPLETED would put the BUG-101 lie back in the one field a
            # caller is most likely to read on its own.
            report.stop(StopReason.NOTHING_CHANGED)
        return self._export(session_id, export_format, start, report)

    # -- the steps of a run, each one its own decision ----------------------
    def _read_and_report_allowance(self, report: Report) -> Balance:
        start = self.read_allowance()
        report.balance_at_start = start
        report.say(f"Starting allowance: {start}.")
        return start

    def _apply_sample_bound(self, steps: list[Step], report: Report) -> list[Step]:
        bounded, bit = self._p.bound(steps)
        if bit:
            report.say(
                f"Small-sample mode: running {self._p.max_steps} of {len(steps)} "
                "requested steps."
            )
        return bounded

    def _set_aside_what_earlier_runs_settled(
            self, session_id: str, steps: list[Step], report: Report) -> list[Step]:
        to_do, applied, unconfirmed, no_effect = self.settled(session_id, steps)
        for step_id in applied:
            report.already_applied.append(step_id)
            report.say(f"'{step_id}': an earlier run already applied this. "
                       "Not repeated, and not billed again.")
        for step_id in unconfirmed:
            report.needs_a_person.append(step_id)
            report.say(
                f"'{step_id}': an earlier run started this and never learned "
                "whether it finished. Not retried — that might be charged twice "
                "and might apply the same edit twice.")
        for step_id in no_effect:
            report.no_effect.append(step_id)
            report.say(
                f"'{step_id}': an earlier run asked this and the document did "
                "not change. Not repeated with the same wording, which has "
                "already been paid for once. Reword it to try again.")
        return to_do

    def _price(self, steps: list[Step], report: Report) -> Plan:
        plan = self.plan(steps)
        report.planned = [c.row_id for c in plan.publish]
        report.deferred = [c.row_id for c in plan.defer]
        needed = estimate([s.as_change() for s in steps], batched=self.BATCHED)
        report.say(
            f"The full request would cost about {needed} operation(s). "
            + ("It fits." if plan.complete else plan.rationale)
        )
        return plan

    def _nothing_left_to_do(self, session_id: str, export_format: str,
                            start: Balance, report: Report) -> Report:
        """Every requested step was settled by an earlier run.

        Nothing is billed, and the export still runs, because the point of a
        rerun after a crash is to walk away with the file.
        """
        report.stop(
            StopReason.STARTED_AND_UNKNOWN if report.needs_a_person
            else StopReason.ALREADY_APPLIED,
            "Nothing was sent: every requested step was settled by an earlier "
            "run. You were not charged again.",
        )
        # `_export` carries the guard for both callers now, so this path does
        # not need its own copy of it.
        return self._export(session_id, export_format, start, report)

    def _did_not_start(self, plan: Plan, report: Report) -> Report:
        """Nothing uploaded, nothing billed, and no half-edited document."""
        refused = plan.rationale == StopReason.REFUSED_PARTIAL.explain()
        report.stop(
            StopReason.REFUSED_PARTIAL if refused else StopReason.NOTHING_FITS,
            "Did not start: "
            + ("only part of the work fits, and this policy refuses a partial "
               "run rather than deliver a subset."
               if refused else
               "none of the requested work fits inside the remaining allowance.")
            + " Nothing was uploaded and nothing was billed.",
        )
        report.balance_at_end = self._g.remaining()
        return report

    def _work(self, session_id: str, plan: Plan, by_id: dict[str, Step],
              report: Report) -> None:
        """Each fitted step: instruct, wait, approve — unless it is already done."""
        for change in plan.publish:
            step = by_id[change.row_id]
            key = operation_key(session_id, step.step_id, step.instruction,
                                step.sections)
            report.keys_by_step[step.step_id] = key
            if self._already_settled(key, step, report):
                continue

            try:
                self._run_step(session_id, step, report, key)
            except RationExhausted as e:
                # A different ceiling from the one below, with a different
                # remedy, so it is never folded into it. The relay refused this
                # call outright: nothing was sent upstream and nothing was
                # billed by either the relay or SuperDocs.
                self._g.ration_exhausted()
                report.stop(StopReason.RATION_EXHAUSTED)
                report.deferred.append(step.step_id)
                report.say(
                    f"Stopped at '{step.step_id}': {e}. The step was not "
                    "started, so nothing is half-applied.")
                return
            except QuotaExhausted:
                report.stop(StopReason.QUOTA_EXHAUSTED)
                report.deferred.append(step.step_id)
                report.say(
                    f"Stopped during '{step.step_id}': SuperDocs reported the "
                    "allowance exhausted. That request still completed; nothing "
                    "further was attempted."
                )
                return
            except Exception as e:
                # Everything else the platform or the network can do -- a 5xx,
                # a refused connection, a read that never returned. It used to
                # escape this loop and take the whole run with it: no report,
                # no plain language, and NO EXPORT, so a step that had already
                # been applied and already been paid for came back as a
                # traceback instead of a file. The reserve is held back
                # precisely so that cannot happen, and an exception leaving
                # here defeated it. Card A's promise is that it never fails
                # halfway; this was failing halfway. BUG-108.
                #
                # The ledger is already truthful at this point: `begin` wrote
                # in_flight before the call, and the edit's own handler
                # refines that to failed or never-learned. Nothing is decided
                # here about what was billed.
                self._stopped_by(e, step, plan, report)
                return
            if self._reserve_reached(plan, step, report):
                return

    def _stopped_by(self, error: Exception, step: Step, plan: Plan,
                    report: Report) -> None:
        """Stop on an unexpected failure, and still come back with a file."""
        report.stop(StopReason.PLATFORM_ERROR)
        report.failed.append(step.step_id)
        key = self._key_for(report, step)
        if key and self._ledger.get(key).state is State.IN_FLIGHT:
            report.needs_a_person.append(step.step_id)
        report.deferred.extend(
            c.row_id for c in plan.publish
            if c.row_id not in report.completed and c.row_id != step.step_id)
        report.say(
            f"Stopped at '{step.step_id}': {error}. The work already applied "
            "is still exported below — exports are free, which is what the "
            "reserve is held back for."
        )

    @staticmethod
    def _key_for(report: Report, step: Step) -> str:
        """The operation key `_work` computed for this step, if it is knowable
        here. Recorded on the report by `_work` so this does not recompute it
        from arguments it no longer has."""
        return report.keys_by_step.get(step.step_id, "")

    def _already_settled(self, key: str, step: Step, report: Report) -> bool:
        """Has this exact call already been paid for, or already been sent?

        SuperDocs has no idempotency on billable writes, so a rerun after a
        crash would otherwise be charged again for work already in the document.
        """
        prior = self._ledger.get(key)
        if prior.state is State.APPLIED:
            report.already_applied.append(step.step_id)
            report.say(
                f"'{step.step_id}': an earlier run already applied this. "
                "Not repeated, and not billed again."
            )
            return True
        if prior.state is State.NO_EFFECT:
            report.no_effect.append(step.step_id)
            report.say(
                f"'{step.step_id}': an earlier run asked this and the document "
                "did not change. Not repeated with the same wording, which has "
                "already been paid for once. Reword it to try again."
            )
            return True
        if prior.state is State.IN_FLIGHT:
            report.needs_a_person.append(step.step_id)
            report.say(
                f"'{step.step_id}': an earlier run started this and never "
                "learned whether it finished. Not retried — that might be "
                "charged twice and might apply the same edit twice."
            )
            return True
        return False

    def _reserve_reached(self, plan: Plan, step: Step, report: Report) -> bool:
        if self._g.remaining().ops > self._p.reserve:
            return False
        remaining_ids = [
            c.row_id for c in plan.publish
            if c.row_id not in report.completed and c.row_id != step.step_id
        ]
        if remaining_ids:
            report.stop(StopReason.RESERVE_FLOOR)
            report.deferred.extend(remaining_ids)
            report.say(
                f"Stopped after '{step.step_id}': holding back the last "
                f"{self._p.reserve} operation(s) so the work already done "
                "can still be exported."
            )
        return True

    def _export(self, session_id: str, export_format: str, start: Balance,
                report: Report) -> Report:
        """Free, so it always runs — including after stopping early. That is the
        whole reason the reserve is worth holding.

        And it never raises. A failing export used to take the report with it
        on this path while the rerun path already guarded the same call, so a
        run whose work had genuinely been applied came back as a traceback
        naming the export rather than as a record of what was applied. The
        file can be fetched again; the account of what was done and what it
        cost cannot. BUG-108.
        """
        try:
            return self._export_now(session_id, export_format, start, report)
        except Exception as e:
            # The session may not exist on this key any more, or the export
            # itself may be refused. Say which, rather than losing the run.
            report.say(
                f"The export could not be made for session '{session_id}': {e}. "
                "Exports are free, so this cost nothing and can be retried — "
                "open the session in SuperDocs and export from there.")
            report.balance_at_end = self._g.remaining()
            report.ops_spent = max(
                0, (start.ops if start else 0) - report.balance_at_end.ops)
            return report

    def _export_now(self, session_id: str, export_format: str, start: Balance,
                    report: Report) -> Report:
        with _elapsed() as took:
            exported = self._c.export(session_id, export_format)
        report.receipt.record("", "export", billable=False, ops_charged=0,
                              balance=self._g.remaining(), seconds=took())
        report.export_warnings = self._c.export_warnings(exported)
        report.say(f"Exported the document as {export_format} (exports are free).")
        if report.export_warnings:
            report.say(
                f"The export completed with {len(report.export_warnings)} "
                "non-fatal warning(s), listed in the report -- the file is usable."
            )
        report.balance_at_end = self._g.remaining()
        report.ops_spent = max(0, (start.ops if start else 0) - report.balance_at_end.ops)
        return report

    def _run_step(self, session_id: str, step: Step, report: Report,
                  key: str | None = None) -> None:
        key = key or operation_key(session_id, step.step_id, step.instruction,
                                   step.sections)
        # Written BEFORE the call goes out. If the process dies between these
        # two lines the ledger says "started, outcome unknown", which is the
        # truth; writing it afterwards would leave no trace of a call that was
        # charged.
        self._ledger.begin(key, session_id=session_id, step_id=step.step_id)
        try:
            with _elapsed() as took:
                started = self._c.edit(session_id, step.instruction)
        except Exception as e:
            # Which of these two it is decides whether a rerun repeats the call.
            # "It failed" is not enough information to answer that, so it is
            # never the answer recorded: only a failure that PROVES the request
            # never reached SuperDocs is marked repeatable. Everything else --
            # a read timeout, a 5xx from a gateway, a dropped connection -- may
            # have been accepted and billed, and is recorded as started and
            # unconfirmed so a person looks at it instead of a retry paying
            # for it twice.
            if provably_never_sent(e):
                self._ledger.failed(
                    key, f"the edit call was rejected before it was billed: {e}")
            else:
                self._ledger.never_learned(
                    key, f"the edit call did not return, and may have been "
                         f"accepted and billed: {e}")
            raise
        self._reconcile(started, report, billable_ops=estimate([step.as_change()]),
                        step_id=step.step_id, call="edit", seconds=took())
        job_id = started.body.get("job_id")
        if not job_id:
            # The call was answered, so it may well have been billed; what is
            # missing is the handle to find out. `applied` was wrong twice
            # over -- it claimed an outcome nobody saw, and it made the step
            # unrepeatable forever without telling anyone. BUG-101.
            self._ledger.never_learned(
                key, "the edit was accepted and returned no job id, so its "
                     "outcome cannot be read back")
            report.needs_a_person.append(step.step_id)
            report.say(f"'{step.step_id}': the edit was accepted but returned "
                       "no job id, so nothing can be read back about it. Not "
                       "retried — it may already have been billed.")
            return

        with _elapsed() as waiting:
            waited = self._c.poll_job(
                job_id,
                on_wait=lambda s, status: report.say(
                    f"'{step.step_id}': still {status} after {s:.0f}s -- processing, not stalled."
                ) if s and s % 60 == 0 else None,
            )
        self._reconcile(waited, report, step_id=step.step_id, call="poll",
                        seconds=waiting())

        final = waited
        if waited.body.get("status") == "awaiting_approval":
            changes = self._pending(waited)
            decisions = [{"change_id": c.get("change_id"), "approved": True} for c in changes]
            if not decisions:
                # Paused, and not by us. The docs give `awaiting_approval` two
                # meanings and `metadata.awaiting_kind` tells them apart: a
                # change review carries `pending_changes`, a large edit carries
                # a `continue_prompt`. With neither to answer, the job is still
                # open -- so it is IN_FLIGHT and a person's, not "done".
                kind = (waited.body.get("metadata") or {}).get("awaiting_kind")
                self._ledger.never_learned(
                    key, f"the job paused awaiting {kind or 'input'} and this "
                         "run did not answer it")
                report.needs_a_person.append(step.step_id)
                report.say(
                    f"'{step.step_id}': the job paused awaiting "
                    f"{kind or 'input'} and proposed no changes to approve. It "
                    "is still open and was not retried — open the session and "
                    "answer it.")
                return
            with _elapsed() as approving:
                approved = self._c.approve(session_id, job_id, decisions)
            self._reconcile(approved, report, step_id=step.step_id,
                            call="approve", seconds=approving())
            report.say(f"'{step.step_id}': approved {len(decisions)} proposed change(s).")

            # Approval is ASYNCHRONOUS. The approve call returns ok, then
            # the job resumes and applies the change. Exporting before it
            # reaches a terminal state returns the document as it was --
            # HTTP 200, a valid file, and the edit silently missing. Verified
            # against the live API on 2026-08-19.
            with _elapsed() as settling:
                settled = self._c.poll_job(job_id, deadline_s=300, interval_s=2)
            self._reconcile(settled, report, step_id=step.step_id,
                            call="poll", seconds=settling())
            final = settled
            if settled.body.get("status") != "completed":
                report.say(
                    f"'{step.step_id}': the job ended as "
                    f"'{settled.body.get('status')}' rather than completed; the "
                    "approved change may not have been applied."
                )
        self._settle(step, report, key, str(job_id), final)

    # -- what a finished job actually did -----------------------------------
    def _settle(self, step: Step, report: Report, key: str, job_id: str,
                final) -> None:
        """Decide what a terminal job means, and record it as that.

        The fall-through this replaces treated every terminal status as done.
        A job that ends `completed` having declined the instruction, and a job
        that ends `failed`, both landed in `report.completed` and both were
        ledgered `applied` -- so the run said work was carried out that was
        not, and re-entry then refused to try it again for good. BUG-101.
        """
        status = final.body.get("status")
        if status in ("failed", "cancelled"):
            return self._settle_failure(step, report, key, status, final)

        before, after = self._version, self._version_of(final)
        if after:
            self._version = after
        if before and after and before == after:
            # Terminal, billed, and the document is byte-for-byte the one that
            # was uploaded. Decided on the version id and never on the prose:
            # the model's own summary is written by the model, and a build that
            # reads intent out of it is guessing about somebody's document.
            said = self._what_it_said(final)
            self._ledger.no_effect(key, job_id=job_id)
            report.no_effect.append(step.step_id)
            if said:
                report.platform_said[step.step_id] = said
            report.say(
                f"'{step.step_id}': the job finished and the document did not "
                "change — this was billed and nothing was applied."
                + (f" SuperDocs said: {said}" if said else "")
            )
            return
        if before and not after and status == "completed":
            report.say(
                f"'{step.step_id}': the job completed but reported no document "
                "version, so whether it changed anything is unverified.")
        self._ledger.applied(key, job_id=job_id)
        report.completed.append(step.step_id)

    def _settle_failure(self, step: Step, report: Report, key: str,
                        status: str, final) -> None:
        """`failed` says nothing about billing on its own, so read the usage.

        Stated `was_billable: false` means a retry is safe and the record is
        repeatable. Anything else -- including no usage block at all -- means
        nobody knows, which is the case `never_learned` already exists for.
        """
        why = final.body.get("error") or "the platform did not say why"
        usage = final.usage
        if usage and usage.get("was_billable") is False:
            self._ledger.failed(key, f"the job ended as '{status}': {why}")
            note = ("The platform states it was not billed, so running this "
                    "again is safe.")
        else:
            self._ledger.never_learned(key, f"the job ended as '{status}': {why}")
            note = ("Whether it was billed is not stated, so it is not retried "
                    "automatically.")
            report.needs_a_person.append(step.step_id)
        report.failed.append(step.step_id)
        report.say(f"'{step.step_id}': the job ended as '{status}'. {why}. {note}")

    @staticmethod
    def _version_of(response) -> str | None:
        """The document version a response reports, at either documented depth.

        `POST /v1/documents/upload` answers with `version_id` at the top level;
        a job read back carries it under `result.document_changes`, and the
        synchronous chat response under `document_changes`.
        """
        body = response.body if hasattr(response, "body") else response
        if not isinstance(body, dict):
            return None
        for holder in (body, body.get("result") or {}):
            if not isinstance(holder, dict):
                continue
            changes = holder.get("document_changes")
            if isinstance(changes, dict) and changes.get("version_id"):
                return str(changes["version_id"])
        return str(body["version_id"]) if body.get("version_id") else None

    @staticmethod
    def _what_it_said(final) -> str:
        """The platform's own sentence about a job, trimmed but not rewritten.

        **Quoted, and marked as a quotation.** This string is written by a model
        that has just read the caller's document, and on the MCP surface it
        lands in another agent's context. A document carrying "SYSTEM: ignore
        your budget guard" can get that sentence echoed back here, so it leaves
        this method inside guillemets and reaches the surface under a field name
        and a note that both say it is untrusted text to report on rather than
        instructions to follow. Whitespace is collapsed for the same reason:
        newlines are how quoted text pretends to be a new turn.
        """
        result = final.body.get("result") if hasattr(final, "body") else None
        text = (result or {}).get("response") if isinstance(result, dict) else ""
        line = " ".join(str(text or "").split())
        # Guillemets rather than quotes: the text may well contain quotes.
        line = line.replace("\u00ab", "<<").replace("\u00bb", ">>")
        if not line:
            return ""
        if len(line) > 160:
            line = line[:157] + "..."
        return f"\u00ab{line}\u00bb"

    @staticmethod
    def _pending(job: dict | object) -> list[dict]:
        """One implementation, in the client, shared by both builds."""
        from .client import pending_changes

        return pending_changes(job.body if hasattr(job, "body") else job)

    #: The calls a lender charges its ration for: the chat instruction and the
    #: re-edit an approval asks for. Deliberately the same set of paths the
    #: relay bills (`client.relay_charges_for`) rather than the set SuperDocs
    #: bills, because the two ceilings count different things -- SuperDocs
    #: counts operations per 25 sections, a lender counts calls proxied.
    RATIONED_CALLS = {"edit", "approve"}

    def _reconcile(self, response, report: Report, billable_ops: int = 0,
                   step_id: str = "", call: str = "",
                   seconds: float | None = None) -> None:
        if call in self.RATIONED_CALLS:
            # Counted whether or not a usage block came back: the ration is a
            # count of calls, and this call happened.
            self._g.spend_ration(1)
        usage = response.usage
        if not usage:
            # A billable call that returned no usage block leaves us guessing.
            # Guess, and say that we are guessing.
            if billable_ops:
                self._g.assume_spent(billable_ops)
            if call:
                report.receipt.record(
                    step_id, call, billable=bool(billable_ops),
                    ops_charged=None, ops_estimated=billable_ops,
                    balance=self._g.remaining(), seconds=seconds,
                    note=("no usage block came back, so this line is what we "
                          "believe rather than what was stated")
                    if billable_ops else "",
                )
            return
        self._g.reconcile(
            ops_charged=int(usage.get("ops_charged", 0)),
            monthly_remaining=usage.get("monthly_remaining"),
            quota_exhausted=bool(usage.get("quota_exhausted", False)),
        )
        if not call:
            return
        charged = int(usage.get("ops_charged", 0))
        if call == "poll":
            # `usage` is non-empty here, so the platform HAS spoken about this
            # job -- and a stated zero is a statement. Gating this on a truthy
            # charge left the edit's `~1` standing as "estimated, never
            # confirmed" on a job the platform had just told us it did not bill
            # at all, which is the receipt asserting a belief over a fact.
            # The usage block on a completed job is the platform's statement
            # about the EDIT that made the job, not about the free poll that
            # read it back. Recorded as a poll line it would be a second charge
            # for one operation; recorded here it turns the edit's estimate into
            # the fact it was always meant to become.
            report.receipt.confirm(step_id, "edit", ops_charged=charged)
            # The poll itself is free, and it is the call that told us the
            # balance -- so it gets its own row, at zero, carrying the number
            # it reported. That keeps the LEFT column in the order the calls
            # actually happened.
            report.receipt.record(step_id, "poll", billable=False,
                                  ops_charged=0, balance=self._g.remaining(),
                                  seconds=seconds)
            return
        report.receipt.record(
            step_id, call, billable=bool(billable_ops),
            ops_charged=charged,
            ops_estimated=billable_ops, balance=self._g.remaining(),
            seconds=seconds,
        )
