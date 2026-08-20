# Quota-aware agent

**Assigned build A · band S1 · MCP/API · SuperDocs**

An agent that reads its remaining allowance **before** it plans, sizes the work
to fit, and when the work does not fit it degrades and says so in a sentence a
person can read — instead of starting a job it cannot finish and dying halfway
through someone's document.

![The agent degrading, twice](screenshot.png)

## Setup and test

**Zero setup to see it work.** No key, no network, and nothing to install but a
test runner — the transport is injected and the tests answer with a documented
fake, so nothing here costs an operation:

```bash
git clone https://github.com/superdocsapp/superdocs-builds.git
cd superdocs-builds/use-cases/Priyanshu2425/quota-aware-agent

python3 backend/demo.py  # watch it plan, size the work, run it, and export

pip install pytest       # the one thing a bare clone does not already carry
python3 -m pytest        # 71 passed, 1 skipped — the one skip is the MCP protocol check
```

Python 3.10 or newer. There are **no runtime dependencies** — that is why the
demo runs on a clone with nothing installed at all, and why `pytest` is the only
thing the suite asks you to add.

<details>
<summary><b>The MCP surface</b> — one extra install</summary>

```bash
pip install -e ".[mcp]"
export SUPERDOCS_API_KEY=your-key-here    # placeholder; never commit a real key
python3 -m quota_aware_agent.mcp_server   # stdio
```

Or register it with a client:

```bash
claude mcp add quota-aware-agent \
  --env SUPERDOCS_API_KEY=your-key-here \
  -- python3 -m quota_aware_agent.mcp_server
```

With the SDK installed the suite runs **72 passed, nothing skipped** — the extra
test starts the real server over stdio and drives it with a real client, because
a server that imports cleanly and cannot start is worse than one that fails
loudly.
</details>

<details>
<summary><b>Against the real API</b> — this one spends operations</summary>

```bash
export SUPERDOCS_API_KEY=your-key-here
python3 backend/demo.py --live --sample 1   # one step, one operation
```

`--sample N` is the small-sample bound. Use it. Exports and `whoami` are free;
edits are not.
</details>

### Environment

| Variable | Required | What it is |
|---|---|---|
| `SUPERDOCS_API_KEY` | only for `--live` and the MCP server | Your SuperDocs API key. An agent can create its own account with `POST /v1/agents/signup`. |
| `QUOTA_AWARE_AGENT_LEDGER` | no | Where the operation ledger is kept. Defaults to `~/.quota-aware-agent/operations.jsonl`. **Set it per account** — two agents driving different accounts must not share one. |

### What to run to check each claim

| Claim | Command |
|---|---|
| It sizes work to fit and names what it dropped | `python3 backend/demo.py --scenario tight` |
| It refuses to start rather than half-finish | `python3 backend/demo.py --scenario broke` |
| It can refuse a partial run outright | `python3 backend/demo.py --scenario tight --refuse` |
| It bounds anything that loops | `python3 backend/demo.py --sample 2` |
| It says what it spent, and whether that adds up | `python3 backend/demo.py --receipt` |
| A rerun does not pay twice | `python3 backend/demo.py --scenario tight --ledger /tmp/ops.jsonl` — **twice** |

The second run of that last one declines to repeat the first run's work and says
so. That is graceful re-entry shown rather than asserted.

## What it uses from SuperDocs

| Surface | Used for |
|---|---|
| `GET /v1/agents/whoami` | The one authoritative allowance read available to an API key. Free. |
| `POST /v1/documents/upload` | The document. Multipart; the filename extension decides the parser. |
| `POST /v1/chat/async` | The edit instruction, with `approval_mode: ask_every_time`. **The only billable call.** |
| `GET /v1/jobs/{id}` | Polling. Silence is treated as processing, never as a crash. |
| `POST /v1/chat/{session_id}/approve` | Approving proposed changes, item by item. |
| `POST /v1/documents/export` | The finished file. Free, so it runs even when the run stopped early. |
| **MCP** | The delivery surface — four tools, of which one costs anything. |

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

A person opens the document, sees whether the edit is there, and says so through
`resolve_operation`. That exit has to exist: a step that is never retried
automatically and cannot be resolved through the surface is a step that can
never run again.

**It knows whether a failed call could have been billed.** A refused connection
proves the request never reached SuperDocs, so a rerun may safely repeat it. A
read timeout proves nothing — the request very possibly arrived, was charged and
applied an edit, and only the answer was lost. Collapsing those two into "it
failed" is how a retry pays twice, so they are recorded as different things and
the default answer is *we do not know*.

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

## How the work is priced, and why that is the whole ballgame

SuperDocs bills a **request**: most bill one operation, and very large ones bill
one per 25 sections edited. The floor of one is therefore *per request*, not per
plan — and this agent sends one request per step.

That distinction is not a rounding error. Four five-section edits pooled to
twenty sections price as **one** operation and bill as **four**. An agent that
prices them pooled reports "it fits", starts, and runs out partway through
somebody's document — which is the exact failure this build exists to prevent,
arriving through its own arithmetic. `estimate(changes, batched=False)` is what
the agent uses, `batched=True` is what a publisher sending one request uses, and
`QuotaAwareAgent.BATCHED` names which one this is so the planner and the
executor cannot drift apart about what a step costs.

## Use it as an MCP server

The card is band S1 · MCP and the user is an agent, so the agent is reachable
as one. Four tools, in the order an agent actually needs them:

| Tool | Costs | What it answers |
|---|---|---|
| `check_allowance` | free | What have I got? The one authoritative read — plus anything an earlier run left unresolved. |
| `plan_work` | free | What fits inside it — without doing any of it. Given a `session_id` it also prices against the ledger, so work an earlier run already paid for is named rather than quoted. |
| `run_work` | billable | Do the part that fits; say what was left out, what was already paid for, and what it cost. Takes HTML, or the bytes of a real `.docx` as `document_base64`. |
| `resolve_operation` | free | A person's answer about a call that was started and never confirmed. Without it that step could never run again — a state the surface could enter and not leave. |

Both `plan_work` and `run_work` take `when_it_does_not_fit`: `degrade` (do the
highest-severity part that fits, the default) or `refuse` (start nothing rather
than deliver a subset).

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

## Why the tests need no key

The transport is injected. `tests/fake.py` implements the documented response
shapes — including the double-parsed envelope, a `usage` block on billable
responses, and the `400` the live upload endpoint returns for a filename whose
extension disagrees with its bytes. The demo answers with the **same** fake the
suite uses, so what a reviewer watches is what the tests assert; a second fake
written only for the demo would be free to flatter the code that calls it.

See **Setup and test** at the top for every command.

## Shared core — stated plainly

`backend/quota_aware_agent/budget.py` is shared, unchanged, with another system by the
same author, where it guards the publisher that writes documents back to
SuperDocs. It is vendored here rather than imported so this build stands alone
in this repository.

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
- **The ledger is not locked against two processes running at once.** It is read
  at start-up and appended to, so two runs launched simultaneously against the
  same steps can both see "not yet attempted" and both pay. Sequential reruns —
  the crash-and-resume case it was built for — are safe. A file lock would close
  the concurrent case and is not built.
- **A failure whose outcome is unknown needs a person, by design.** The agent
  will not guess between paying twice and leaving work undone, so those steps
  stay blocked until someone answers with `resolve_operation`.
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
