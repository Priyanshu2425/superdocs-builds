# Quota-aware agent

**Assigned build A · band S1 · MCP/API · SuperDocs**

An agent that reads its remaining allowance **before** it plans, sizes the work
to fit, and when the work does not fit it degrades and says so in a sentence a
person can read — instead of starting a job it cannot finish and dying halfway
through someone's document.

![The agent degrading, twice](screenshot.png)

## The problem, stated precisely

An agent that plans without checking its allowance starts work it cannot
finish. The obvious fix — "check the balance first" — runs into something the
SuperDocs documentation is explicit about:

> the `/v1/users/me/usage` and `/v1/users/me/limits` endpoints belong to the
> web-app account surface and accept web-app session tokens only — they reject
> `sk_` / `lce_` API keys with a `401`

**So an API-key agent cannot read its balance on demand.** There is no endpoint
to poll. What exists is `GET /v1/agents/whoami` once at the start, and the
`usage` block that rides on **every** chat response afterwards. You learn your
balance as a side effect of doing work, not in advance of it.

This build takes that seriously rather than papering over it. `Balance` carries
an `authoritative` flag, and every report says which kind of number it used:

```
Starting allowance: 3 operations (confirmed).
Allowance now: 1 operation (confirmed).
```

A number inferred between calls prints as `(estimated)` and can never be
presented as a live read. That distinction is a tested property, not a comment.

## What it does when the work does not fit

Three behaviours, each one tested:

**It refuses to start.** If nothing fits, nothing is uploaded and nothing is
billed. There is no partially-edited document to clean up.

```
Did not start: none of the requested work fits inside the remaining allowance.
Nothing was uploaded and nothing was billed.
```

**It degrades by severity.** If some of the work fits, the highest-severity
work goes first and the rest is named — not silently dropped.

```
The full request would cost about 4 operation(s). Sized to fit: 50 of 100
changed sections (2 of 4 operations). Deferred 2 lower-severity change(s) to
stay inside the remaining allowance.
Left undone: terms, footer. These were not started, so nothing is half-applied.
```

**It holds a reserve.** The last operation is never spent on new edits, so the
work already done can always be exported. Exports are free, so this costs the
user nothing and guarantees they end up with a file.

**It does not pay twice for the same edit.** SuperDocs has no idempotency on
billable writes — there is no idempotency key on `POST /v1/chat/async` — so a
rerun after an interrupted batch is charged again for work already in the
document. An operation ledger, keyed on what the call *is* rather than on an id
the caller assigned, is written **before** each billable call and consulted
before each one:

```
'terms': an earlier run already applied this. Not repeated, and not billed again.
```

The hard case is handled as a hard case. A call that was sent, and whose outcome
this process died before learning, is **neither repeated nor forgotten** —
retrying it might be charged twice and might apply the same edit twice, and
skipping it might leave the work undone. There is no safe automatic answer, so
it is reported:

```
'dates': an earlier run started this and never learned whether it finished.
Not retried — that might be charged twice and might apply the same edit twice.
```

**It says what it spent, and whether that adds up.** `--receipt` prints the line
items and reconciles them against the allowance — and when they disagree, it
says so rather than picking a side:

```
DOES IT ADD UP?
2 operation(s) charged, and the allowance moved by 2. These agree.
```

A `~` marks an operation we *believe* was charged on a call that returned no
usage block, and a `?` marks a balance we inferred. Confirmed and estimated
figures are never added together, because one number that is part measurement
and part belief tells the reader nothing about which part is which.

### The stopping rule, stated once

1. `quota_exhausted` on any response — the platform's own signal, and the only
   authoritative one. Stop immediately. Our arithmetic never overrules it.
2. The reserve floor — never spend the last operation.
3. `--sample N` — the small-sample bound, because anything that loops needs one.

All three live in `Policy`, which is frozen, so a run ends under the policy it
began with. And every one of them **explains what it was protecting** when it
fires. That is not decoration: a limit whose only feedback is *"I stopped"*
trains the person who set it to raise the number until it stops firing, which is
the same as not having it.

## Use it as an MCP server

The card is band S1 · MCP and the user is an agent, so the agent is reachable
as one. Three tools, in the order an agent actually needs them:

| Tool | Costs | What it answers |
|---|---|---|
| `check_allowance` | free | What have I got? The one authoritative read — plus anything an earlier run left unresolved. |
| `plan_work` | free | What fits inside it — without doing any of it. |
| `run_work` | billable | Do the part that fits; say what was left out, what was already paid for, and what it cost. |

**Every result carries a `budget` block**, so a calling agent never has to spend
a turn asking what is left before it decides what to do. It carries the
remaining operations, whether that number is authoritative or inferred, the
reserve, and what is therefore spendable on new edits. Surfacing the budget into
the caller's own context is the cheapest useful thing this build does: an agent
that knows what is left can decide earlier, do less, or escalate — and an agent
that has to ask cannot.

```
pip install -e ".[mcp]"
export SUPERDOCS_API_KEY=your-key-here
python3 -m quota_aware_agent.mcp_server        # stdio
```

Claude Code:

```
claude mcp add quota-aware-agent \
  --env SUPERDOCS_API_KEY=your-key-here \
  -- python3 -m quota_aware_agent.mcp_server
```

**`plan_work` exists as its own tool on purpose.** An agent that has to commit
to work before it can find out what fits is the exact failure this build is
about, so the plan is readable for free and changes nothing. A test asserts that
planning makes no billable call, and another asserts the plan and the run agree
about what fits — if they could disagree, only one of them would be tested.

Every tool is a thin wrapper over `QuotaAwareAgent`; no planning logic lives in
the MCP layer. `dispatch()` calls the whole surface without a protocol client,
which is what keeps it testable offline.

## The four-call contract

`upload` → `edit instruction` → `approve` → `export`, built first and built
completely, before any depth.

| Call | Endpoint | Billing |
|---|---|---|
| Upload | `POST /v1/documents/upload` | free |
| Edit instruction | `POST /v1/chat/async` (`approval_mode: ask_every_time`) | 1 op per 25 sections |
| Approve | `POST /v1/chat/{session_id}/approve` | denied changes are never billed |
| Export | `POST /v1/documents/export` | free |

### The three named traps, all handled

**Trap 1 · the double parse.** A `proposed_change_batch` envelope carries its
payload as a JSON-encoded *string* in `content`. It needs a second parse. Miss
it and you get diff cards where every field reads `undefined` — silently, with
no error. `parse_proposed_changes` does it, and the test asserts the change id
actually reached the approve call, because a test that only checks "it ran"
would pass while approving nothing.

**Trap 2 · silence is not a crash.** A job on a large document can run for
minutes with no output. `poll_job` treats quiet as processing and gives up only
at an explicit deadline — and when it does, it says the deadline was *ours*:

> job job-1 was still 'in_progress' after 4s. That is a deadline this client
> imposed, not a platform failure — the job may still be running.

**Trap 3 · the allowance.** The whole point of this build. Small-sample mode
and the stopping rule above.

## Run it

No key, no network, nothing to install:

```
python3 -m pytest                 # 50 tests, offline
python3 demo.py                   # allowance is plentiful — everything runs
python3 demo.py --scenario tight  # not enough — it degrades and explains
python3 demo.py --scenario broke  # nothing fits — it refuses to start
python3 demo.py --sample 2        # small-sample mode
python3 demo.py --receipt         # the line items, and whether they add up
```

Against the real API:

```
export SUPERDOCS_API_KEY=your-key-here
python3 demo.py --live
```

The transport is injected, which is why the tests need no key: the fake
implements the documented response shapes, including the double-parsed
envelope and a `usage` block on every billable response.

## Shared core — stated plainly

`quota_aware_agent/budget.py` is shared, unchanged, with **Attest**, the
document-analysis system I built for Problem 01 of the same round, where it
guards the publisher that writes back to SuperDocs. It is vendored here rather
than imported so this build stands alone in the builds repository.

Reuse is only a shortcut when it is hidden. The same module solving both
problems is the argument that the abstraction is right: "never start work you
cannot finish" is one idea, and an agent sizing a plan and a publisher sizing a
document write are the same shape of problem.

## Honest limitations

- **The allowance is not continuously known, and this build does not pretend it
  is.** It is authoritative at `whoami` and after each response, and an estimate
  in between. Every report labels which.
- **Section counts for planned edits are supplied by the caller, not measured.**
  SuperDocs bills per 25 sections *edited*, which is known only after the edit.
  A caller who badly underestimates can still overshoot — which is exactly why
  `quota_exhausted` is treated as authoritative and the reserve exists.
- **The operation ledger is this client's memory, not the platform's.** It
  prevents *this tool* from paying twice. It cannot prevent a different client,
  or a person in the web app, from making the same edit — nothing below us
  offers idempotency to build that on.
- **A reconciliation needs two authoritative balances.** If either end of the
  run was inferred, the receipt says the run cannot be reconciled rather than
  reconciling two guesses.
- **It approves every proposed change in the demo.** The approve call supports
  per-change denial with feedback and the client exposes it; deciding *which*
  changes are worth approving is a different problem from affording them.
- It does not implement the pre-signed upload flow, so it is limited to the
  ~20 MB direct-upload ceiling.

## Credit

Built by **Priyanshu Semwal** ([@Priyanshu2425](https://github.com/Priyanshu2425))
for the SuperDocs engineer round, 2026. MIT licensed — see [LICENSE](LICENSE).

Grounded throughout in the SuperDocs API documentation; where the task brief and
the documentation differed, the documentation won.
